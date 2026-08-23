"""Compatibility-preserving roam shim that finalizes closed ATES runs."""
from __future__ import annotations

import sys
from argus.ates import RunStatus, recover_revision_one
from argus.engine import roam_ates_impl as _impl
from argus.engine.ates_runtime import resolve_runtime_project_dir

_original_roam = _impl.roam
_original_exit_code = _impl.roam_exit_code


def _public_status(status: RunStatus) -> str:
    return {RunStatus.PASSED: "pass", RunStatus.FAILED: "fail", RunStatus.ERROR: "error", RunStatus.CANCELLED: "cancelled"}[status]


def _finalizing_roam(*args, **kwargs):
    session = _original_roam(*args, **kwargs)
    run_id = getattr(session, "ates_run_id", None)
    if not run_id:
        return session
    session_dir = kwargs.get("session_dir")
    if session_dir is None and len(args) >= 5:
        session_dir = args[4]
    try:
        root = resolve_runtime_project_dir(kwargs.get("project_dir"), session_dir=session_dir)
        finalized = recover_revision_one(root, run_id)
    except Exception as exc:
        session.execution_status = "error"
        session.stopped_reason = f"ATES finalization failed: {type(exc).__name__}: {exc}"
        return session
    session.ates_effective_status = finalized.outcome.effective_status.value
    session.ates_finalization_id = str(finalized.outcome.finalization_id)
    session.execution_status = _public_status(finalized.outcome.effective_status)
    return session


def _finalizing_exit_code(session):
    status = str(getattr(session, "execution_status", "") or "")
    if status in {"fail", "error", "outcome_unknown", "cancelled"}:
        return 1
    return _original_exit_code(session)


_impl.roam = _finalizing_roam
_impl.roam_exit_code = _finalizing_exit_code
sys.modules[__name__] = _impl
