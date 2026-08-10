"""Test runner — executes one TestSpec against a live target.

Hybrid-agentic execution:
  * **NL steps** → the agent loop (:mod:`argus.engine.agent`): the LLM
    observes and acts until it declares the step done (bounded per-step).
  * **assert steps** → executed deterministically against a fresh
    observation. The model is never consulted for assertions; user-defined
    assertions are always authoritative.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Optional

import uuid as _uuid

from argus.adapters.base import Adapter, AdapterError
from argus.engine.agent import extract_actions, run_turn
from argus.engine.results import RunResult, StepResult
from argus.engine.spec import AssertStep, NLStep, TestSpec
from argus.providers.base import LLMProvider, ProviderError
from argus.tokens import Budget

_MAX_TURNS_PER_STEP = 10

SYSTEM_PROMPT = """\
You are Argus, a precise GUI test agent driving a desktop application.
You complete ONE test step at a time by emitting JSON actions.
Rules:
- Work strictly toward the GOAL; do not explore beyond it.
- Prefer element_id references from the UI TREE over raw coordinates.
- When the goal is achieved, reply {"action":"done","success":true,"note":"..."}.
- If the goal is impossible (element missing, app crashed), reply
  {"action":"done","success":false,"note":"<what is wrong>"}.
- Never invent element ids. Re-read the UI TREE each turn."""

ProgressFn = Callable[[StepResult], None]


def check_assertion(
    step: AssertStep,
    adapter: Adapter,
    knowledge_store=None,
    state_id: str = "",
    target: str = "",
    session_id: str = "",
) -> StepResult:
    obs = adapter.observe(include_screenshot=True)
    result = StepResult(index=0, kind="assert", text=step.describe())
    result.expected = step.describe()

    ok = False
    actual = ""
    if step.assertion == "text_visible":
        ok = obs.find_text(str(step.expected))
        actual = "text found" if ok else f'text not found in window "{obs.window_title}"'
    elif step.assertion == "window_title_contains":
        ok = str(step.expected).lower() in obs.window_title.lower()
        actual = f'window title is "{obs.window_title}"'
    elif step.assertion == "element_exists":
        want = step.expected if isinstance(step.expected, dict) else {"name": step.expected}
        name = str(want.get("name", "")).lower()
        ctype = str(want.get("control_type", "")).lower()
        ok = any(
            (not name or name in el.name.lower())
            and (not ctype or ctype == el.control_type.lower())
            for el in obs.elements
        )
        actual = "element found" if ok else "no matching element in UI tree"
    elif step.assertion == "process_running":
        ok = obs.process_alive == bool(step.expected)
        actual = "process alive" if obs.process_alive else (obs.error or "process exited")
    elif step.assertion == "dialog_open":
        needle = str(step.expected).lower()
        ok = any(needle in d.lower() for d in obs.dialogs)
        actual = f"open dialogs: {', '.join(obs.dialogs) or 'none'}"
    elif step.assertion == "stdout_contains":
        needle = str(step.expected).lower()
        stdout = (obs.stdout or "").lower()
        ok = needle in stdout
        actual = f"stdout: {(obs.stdout or '').strip()[:120]!r}"
    elif step.assertion == "stderr_contains":
        needle = str(step.expected).lower()
        stderr = (obs.stderr or "").lower()
        ok = needle in stderr
        actual = f"stderr: {(obs.stderr or '').strip()[:120]!r}"
    elif step.assertion == "exit_code_is":
        expected_code = int(step.expected)
        ok = obs.exit_code == expected_code
        actual = f"exit code: {obs.exit_code}"
    elif step.assertion == "url_contains":
        needle = str(step.expected).lower()
        url = (obs.url or "").lower()
        ok = needle in url
        actual = f"url: {obs.url!r}"
    elif step.assertion == "page_title_contains":
        needle = str(step.expected).lower()
        ok = needle in obs.window_title.lower()
        actual = f'page title: "{obs.window_title}"'

    result.status = "pass" if ok else "fail"
    result.actual = actual

    if knowledge_store is not None and not ok and state_id:
        try:
            knowledge_store.record_assertion(
                step.assertion, step.expected, state_id, passed=False,
                target=target, session_id=session_id,
            )
        except Exception:
            pass

    return result


def _execution_fields(adapter: Adapter) -> dict:
    try:
        info = adapter.info()
    except Exception:
        return {
            "environment_type": "direct",
            "isolated": False,
            "location": "unknown",
        }
    return {
        "environment_type": str(info.environment_type),
        "isolated": bool(info.isolated),
        "location": str(info.location),
    }


def _record_environment_failure_reason(adapter: Adapter, reason: str) -> None:
    """Notify an execution environment before teardown can erase failure state."""
    hook = getattr(adapter, "record_failure", None)
    if not callable(hook):
        return
    try:
        hook(reason)
    except Exception:
        # Failure retention is diagnostic. A test result must never be converted
        # into a different outcome merely because the optional hook failed.
        pass


def _record_environment_failure(adapter: Adapter, step: StepResult) -> None:
    reason = f"step {step.index + 1} {step.status}: {step.text}"
    if step.note:
        reason += f" — {step.note}"
    elif step.actual:
        reason += f" — {step.actual}"
    _record_environment_failure_reason(adapter, reason)


def _retained_failure(adapter: Adapter):
    hook = getattr(adapter, "failure_capsule", None)
    if not callable(hook):
        return None
    try:
        value = hook()
    except Exception:
        return None
    return value if isinstance(value, dict) and value else None


def _retention_failure(adapter: Adapter):
    hook = getattr(adapter, "failure_capsule_error", None)
    if not callable(hook):
        return None
    try:
        value = hook()
    except Exception:
        return None
    return value if isinstance(value, dict) and value else None


def run_test(
    spec: TestSpec,
    provider: LLMProvider,
    adapter: Adapter,
    budget: Optional[Budget] = None,
    on_step: Optional[ProgressFn] = None,
    warn: Optional[Callable[[str], None]] = None,
    knowledge_store=None,
    shots_dir: Optional[Path] = None,
) -> RunResult:
    result = RunResult(
        test_name=spec.name,
        test_file=spec.file_name,
        adapter=spec.adapter,
        provider=provider.describe(),
        **_execution_fields(adapter),
    )
    started = time.monotonic()
    session_id = str(_uuid.uuid4())[:8]
    target = spec.launch or spec.name

    try:
        use_vision = provider.supports_vision()
    except ProviderError as exc:
        result.status = "error"
        result.error = f"provider check failed: {exc}"
        result.duration_s = time.monotonic() - started
        return result
    if not use_vision and warn:
        warn(
            f"model '{provider.model}' is not multimodal — vision-related testing is "
            "disabled; Argus will rely on the accessibility tree only."
        )

    try:
        adapter.launch(spec.launch)
    except AdapterError as exc:
        result.status = "error"
        result.error = f"launch failed: {exc}"
        # A prepared Capsule may have retained the failed guest launch before
        # re-raising. Capture both success and recovery metadata before this
        # early return; preparation failures simply leave both fields empty.
        result.failure_capsule = _retained_failure(adapter)
        result.failure_capsule_error = _retention_failure(adapter)
        result.duration_s = time.monotonic() - started
        result.tokens = provider.tracker.snapshot()
        return result

    failed = False
    budget_failure_recorded = False
    teardown_budget_reason: Optional[str] = None
    execution_error: Optional[str] = None
    cleanup_error: Optional[str] = None
    try:
        for index, step in enumerate(spec.steps):
            step_started = time.monotonic()

            if failed and not spec.continue_on_failure and step.kind != "teardown":
                sr = StepResult(index=index, kind=step.kind, text=_step_text(step), status="skipped",
                                note="skipped after failure (continue_on_failure: false)")
                result.steps.append(sr)
                if on_step:
                    on_step(sr)
                continue

            # Always check the budget at a step boundary. If it has expired when
            # entering teardown, record the run failure but still execute teardown
            # so retention/cleanup reaches a deterministic close point.
            budget_reason = budget.exhausted() if budget else None
            if budget_reason and step.kind == "teardown":
                if not budget_failure_recorded:
                    failed = True
                    budget_failure_recorded = True
                    teardown_budget_reason = budget_reason
                    _record_environment_failure_reason(
                        adapter,
                        f"run budget exhausted before teardown: {budget_reason}",
                    )
            elif budget_reason:
                sr = StepResult(index=index, kind=step.kind, text=_step_text(step),
                                status="skipped", note=f"skipped: {budget_reason}")
                result.steps.append(sr)
                _record_environment_failure(adapter, sr)
                if on_step:
                    on_step(sr)
                failed = True
                budget_failure_recorded = True
                continue

            if isinstance(step, AssertStep):
                sr = check_assertion(
                    step, adapter,
                    knowledge_store=knowledge_store,
                    state_id="",
                    target=target,
                    session_id=session_id,
                )
                sr.index = index
                _attach_screenshot(sr, adapter, index, shots_dir)
            elif step.text == "close" and step.kind == "teardown":
                try:
                    adapter.close()
                except Exception as exc:
                    retention_error = _retention_failure(adapter)
                    if retention_error is None:
                        raise
                    # If the run was healthy before teardown, this retention
                    # failure was armed by teardown itself and is a real run
                    # error. For an already-failed run, keep the original result
                    # semantics and surface retention as diagnostic metadata.
                    teardown_failed = not failed
                    sr = StepResult(
                        index=index,
                        kind="teardown",
                        text="close target",
                        status="error" if teardown_failed else "pass",
                        note=f"Failure Capsule retention warning: {exc}",
                    )
                    if teardown_failed:
                        result.error = f"teardown failed: {exc}"
                else:
                    sr = StepResult(index=index, kind="teardown", text="close target", status="pass")
            else:
                sr = _run_nl_step_with_retries(
                    step, index, provider, adapter, use_vision, spec.retries,
                    knowledge_store=knowledge_store, target=target, session_id=session_id,
                    shots_dir=shots_dir,
                )

            if step.kind == "teardown" and teardown_budget_reason:
                budget_note = f"run budget exhausted before teardown: {teardown_budget_reason}"
                sr.note = f"{sr.note}; {budget_note}" if sr.note else budget_note
                teardown_budget_reason = None

            sr.duration_s = time.monotonic() - step_started
            result.steps.append(sr)
            if sr.status in ("fail", "error"):
                failed = True
                _record_environment_failure(adapter, sr)
            if on_step:
                on_step(sr)
    except Exception as exc:
        # Unexpected observation/action/assertion/callback failures must be
        # recorded before final close; otherwise retain_on_failure would erase
        # the state that caused the exception. Keep the failure inside the
        # structured RunResult so callers can persist the retained/recovery
        # metadata instead of losing it across an exception boundary.
        execution_error = f"{type(exc).__name__}: {exc}"
        _record_environment_failure_reason(
            adapter,
            f"run execution error: {execution_error}",
        )
        failed = True
        result.error = f"execution failed: {execution_error}"
    finally:
        # If explicit teardown already failed to retain a Failure Capsule, do
        # not retry the retention operation or fall through to destructive
        # cleanup. The environment has preserved the handle for recovery.
        if _retention_failure(adapter) is None:
            try:
                adapter.close()
            except Exception as exc:
                # A cleanup failure on an otherwise healthy run is itself a run
                # error. Preserve existing fail/error semantics when the run was
                # already failing, but never silently report pass after teardown
                # failed or retained a Failure Capsule.
                if not failed and execution_error is None:
                    cleanup_error = f"{type(exc).__name__}: {exc}"
                    failed = True
                    result.error = f"cleanup failed: {cleanup_error}"

    result.failure_capsule = _retained_failure(adapter)
    result.failure_capsule_error = _retention_failure(adapter)
    result.duration_s = time.monotonic() - started
    if (
        execution_error is not None
        or cleanup_error is not None
        or any(s.status == "error" for s in result.steps)
    ):
        result.status = "error"
    elif failed:
        result.status = "fail"
    else:
        result.status = "pass"
    result.tokens = provider.tracker.snapshot()

    if knowledge_store is not None:
        try:
            knowledge_store.finalize_session(session_id, target)
        except Exception:
            pass

    return result


def _step_text(step) -> str:
    return step.describe() if isinstance(step, AssertStep) else step.text


def _attach_screenshot(
    sr: StepResult,
    adapter: Adapter,
    index: int,
    shots_dir: Optional[Path],
) -> None:
    if shots_dir is None:
        return
    try:
        obs = adapter.observe(include_screenshot=True)
        if obs.screenshot_png:
            shots_dir.mkdir(parents=True, exist_ok=True)
            name = f"step-{index + 1:02d}.png"
            (shots_dir / name).write_bytes(obs.screenshot_png)
            sr.screenshot_path = f"shots/{name}"
    except Exception:
        pass


def _run_nl_step_with_retries(
    step: NLStep,
    index: int,
    provider: LLMProvider,
    adapter: Adapter,
    use_vision: bool,
    retries: int,
    knowledge_store=None,
    target: str = "",
    session_id: str = "",
    shots_dir: Optional[Path] = None,
) -> StepResult:
    sr = _run_nl_step(
        step, index, provider, adapter, use_vision,
        knowledge_store=knowledge_store, target=target, session_id=session_id,
        shots_dir=shots_dir,
    )
    if sr.status == "pass" or retries == 0:
        return sr
    for attempt in range(retries):
        retry_sr = _run_nl_step(
            step, index, provider, adapter, use_vision,
            knowledge_store=knowledge_store, target=target, session_id=session_id,
            shots_dir=shots_dir,
        )
        if retry_sr.status == "pass":
            retry_sr.flaky = (attempt > 0 or sr.status != "pass")
            return retry_sr
    sr.flaky = True
    return sr


def _run_nl_step(
    step: NLStep,
    index: int,
    provider: LLMProvider,
    adapter: Adapter,
    use_vision: bool,
    knowledge_store=None,
    target: str = "",
    session_id: str = "",
    shots_dir: Optional[Path] = None,
) -> StepResult:
    sr = StepResult(index=index, kind=step.kind, text=step.text)
    history: list = []
    prev_state_id = ""
    prev_action: dict = {}
    for _ in range(_MAX_TURNS_PER_STEP):
        turn = run_turn(
            provider, adapter, SYSTEM_PROMPT, step.text, history, use_vision,
            knowledge_store=knowledge_store,
            target=target,
            session_id=session_id,
            prev_state_id=prev_state_id,
        )
        if knowledge_store is not None and prev_state_id and turn.state_id and prev_action:
            try:
                knowledge_store.record_transition(
                    prev_state_id, prev_action, turn.state_id,
                    target, session_id, success=not bool(turn.error),
                )
            except Exception:
                pass
        prev_state_id = turn.state_id
        prev_action = turn.action

        if turn.error and turn.action.get("action") == "done":
            sr.status = "error"
            sr.note = turn.error
            sr.actions = history
            return sr
        if turn.action.get("action") == "done":
            sr.status = "pass" if turn.action.get("success") else "fail"
            sr.note = str(turn.action.get("note", ""))
            sr.actions = history
            _attach_screenshot(sr, adapter, index, shots_dir)
            return sr
        if turn.error:
            history.append(f"{turn.action.get('action')} FAILED: {turn.error}")
        else:
            why = turn.action.get("why", "")
            history.append(turn.note + (f" — {why}" if why else ""))
        time.sleep(0.4)
    sr.status = "fail"
    sr.note = f"step not completed within {_MAX_TURNS_PER_STEP} actions"
    sr.actions = history
    _attach_screenshot(sr, adapter, index, shots_dir)
    return sr