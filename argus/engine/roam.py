"""ATES-aware public free-roam entry point.

The pre-ATES roam engine is retained verbatim in :mod:`roam_impl`.  This thin
wrapper adds mandatory structural ATES lifecycle evidence without changing the
exploration algorithm, model prompts, legacy report format, or screenshots.
"""

from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path
from typing import Callable, Optional

from argus.adapters.base import Adapter
from argus.engine import roam_impl as _impl
from argus.engine.ates_runtime import (
    AtesAdapterProxy,
    AtesRuntimeRecorder,
    resolve_runtime_project_dir,
)
from argus.providers.base import LLMProvider
from argus.tokens import Budget

Finding = _impl.Finding
RoamSession = _impl.RoamSession
ROAM_SYSTEM_PROMPT = _impl.ROAM_SYSTEM_PROMPT

_active_recorder: ContextVar[Optional[AtesRuntimeRecorder]] = ContextVar(
    "argus_roam_ates_recorder",
    default=None,
)
_original_add_finding = _impl._add_finding


def _recording_add_finding(session, finding, screenshot_png, shots_dir, emit) -> None:
    """Preserve legacy finding handling and append its privacy-classified ATES record."""
    _original_add_finding(session, finding, screenshot_png, shots_dir, emit)
    recorder = _active_recorder.get()
    if recorder is not None and not recorder.failed:
        recorder.record_finding(
            source=finding.source,
            classification=finding.severity,
            title=finding.title,
            description={
                "expected": finding.expected,
                "actual": finding.actual,
                "detail": finding.detail,
            },
        )


# roam_impl is private and invoked only through this public wrapper.  A
# ContextVar keeps the installed hook safe when independent roam sessions run
# on different threads/tasks.
_impl._add_finding = _recording_add_finding


def _attempt_status(stopped_reason: str) -> str:
    reason = (stopped_reason or "").lower()
    if "ates evidence failure" in reason or "ates finalization failed" in reason:
        return "error"
    if "action outcome unresolved" in reason:
        return "outcome_unknown"
    if "stopped by user" in reason:
        return "cancelled"
    if "provider" in reason:
        return "error"
    if "crash" in reason or "unresponsive" in reason or "action failure" in reason:
        return "fail"
    return "pass"


def roam_exit_code(session: RoamSession) -> int:
    """Return a shell-safe result code for a completed roam session."""
    status = str(
        getattr(session, "execution_status", "")
        or _attempt_status(getattr(session, "stopped_reason", ""))
    )
    if status in {"fail", "error", "outcome_unknown"}:
        return 1
    return 1 if session.findings else 0


def _finish(
    recorder: AtesRuntimeRecorder,
    execution_result: str,
    *,
    reason: str,
) -> Optional[str]:
    """Close the structural stream without claiming canonical completion."""
    try:
        if recorder.failed:
            failure = recorder.failure
            return (
                "ATES evidence failure"
                + (f": {type(failure).__name__}: {failure}" if failure else "")
            )
        if recorder.current_attempt_id is not None:
            recorder.complete_current(execution_result)
        recorder.environment_released()
        recorder.mark_incomplete(reason, execution_result=execution_result)
        return None
    except Exception as exc:
        return f"ATES finalization failed: {type(exc).__name__}: {exc}"
    finally:
        try:
            recorder.close()
        except Exception:
            pass


def roam(
    target: str,
    provider: LLMProvider,
    adapter: Adapter,
    budget: Budget,
    session_dir: Path,
    on_event: Optional[Callable[[str], None]] = None,
    stop_flag: Optional[Callable[[], bool]] = None,
    generate_regressions: bool = True,
    memory_dir: Optional[Path] = None,
    knowledge_store=None,
    project_dir: Optional[Path] = None,
) -> RoamSession:
    """Run the existing explorer while recording canonical structural ATES."""
    root = resolve_runtime_project_dir(project_dir, session_dir=session_dir)
    recorder = AtesRuntimeRecorder.for_roam(
        root,
        provider,
        adapter,
        target=target,
    )
    run_id = str(recorder.run_id)
    wrapped = AtesAdapterProxy(adapter, recorder)
    token = _active_recorder.set(recorder)
    recorder.begin_roam()

    try:
        session = _impl.roam(
            target=target,
            provider=provider,
            adapter=wrapped,
            budget=budget,
            session_dir=session_dir,
            on_event=on_event,
            stop_flag=stop_flag,
            generate_regressions=generate_regressions,
            memory_dir=memory_dir,
            knowledge_store=knowledge_store,
        )
    except BaseException:
        _finish(recorder, "error", reason="runtime.execution_interrupted")
        raise
    else:
        status = _attempt_status(session.stopped_reason)
        evidence_error = _finish(
            recorder,
            status,
            reason="runtime.finalization_pending",
        )
        session.ates_run_id = run_id
        if evidence_error:
            session.stopped_reason = evidence_error
            status = "error"
        session.execution_status = status
        return session
    finally:
        _active_recorder.reset(token)


__all__ = [
    "Finding",
    "RoamSession",
    "ROAM_SYSTEM_PROMPT",
    "roam",
    "roam_exit_code",
]
