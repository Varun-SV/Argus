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
from typing import Callable, Optional

from argus.adapters.base import Adapter, AdapterError
from argus.engine.agent import run_turn
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


def check_assertion(step: AssertStep, adapter: Adapter) -> StepResult:
    obs = adapter.observe(include_screenshot=False)
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
    return result


def run_test(
    spec: TestSpec,
    provider: LLMProvider,
    adapter: Adapter,
    budget: Optional[Budget] = None,
    on_step: Optional[ProgressFn] = None,
    warn: Optional[Callable[[str], None]] = None,
) -> RunResult:
    result = RunResult(
        test_name=spec.name,
        test_file=spec.file_name,
        adapter=spec.adapter,
        provider=provider.describe(),
    )
    started = time.monotonic()

    # Vision capability: ask once; degrade gracefully.
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
        result.duration_s = time.monotonic() - started
        return result

    failed = False
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

            budget_reason = budget.exhausted() if budget else None
            if budget_reason:
                sr = StepResult(index=index, kind=step.kind, text=_step_text(step),
                                status="skipped", note=f"skipped: {budget_reason}")
                result.steps.append(sr)
                if on_step:
                    on_step(sr)
                failed = True
                continue

            if isinstance(step, AssertStep):
                sr = check_assertion(step, adapter)
                sr.index = index
            elif step.text == "close" and step.kind == "teardown":
                adapter.close()
                sr = StepResult(index=index, kind="teardown", text="close target", status="pass")
            else:
                sr = _run_nl_step_with_retries(
                    step, index, provider, adapter, use_vision, spec.retries
                )

            sr.duration_s = time.monotonic() - step_started
            result.steps.append(sr)
            if sr.status in ("fail", "error"):
                failed = True
            if on_step:
                on_step(sr)
    finally:
        try:
            adapter.close()
        except Exception:
            pass

    result.duration_s = time.monotonic() - started
    if any(s.status == "error" for s in result.steps):
        result.status = "error"
    elif failed:
        result.status = "fail"
    else:
        result.status = "pass"
    result.tokens = provider.tracker.snapshot()
    return result


def _step_text(step) -> str:
    return step.describe() if isinstance(step, AssertStep) else step.text


def _run_nl_step_with_retries(
    step: NLStep,
    index: int,
    provider: LLMProvider,
    adapter: Adapter,
    use_vision: bool,
    retries: int,
) -> StepResult:
    sr = _run_nl_step(step, index, provider, adapter, use_vision)
    if sr.status == "pass" or retries == 0:
        return sr
    for attempt in range(retries):
        retry_sr = _run_nl_step(step, index, provider, adapter, use_vision)
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
) -> StepResult:
    sr = StepResult(index=index, kind=step.kind, text=step.text)
    history: list = []
    for _ in range(_MAX_TURNS_PER_STEP):
        turn = run_turn(provider, adapter, SYSTEM_PROMPT, step.text, history, use_vision)
        if turn.error and turn.action.get("action") == "done":
            sr.status = "error"
            sr.note = turn.error
            sr.actions = history
            return sr
        if turn.action.get("action") == "done":
            sr.status = "pass" if turn.action.get("success") else "fail"
            sr.note = str(turn.action.get("note", ""))
            sr.actions = history
            return sr
        if turn.error:
            history.append(f"{turn.action.get('action')} FAILED: {turn.error}")
        else:
            why = turn.action.get("why", "")
            history.append(turn.note + (f" — {why}" if why else ""))
        time.sleep(0.4)  # let the UI settle between synthesized inputs
    sr.status = "fail"
    sr.note = f"step not completed within {_MAX_TURNS_PER_STEP} actions"
    sr.actions = history
    return sr
