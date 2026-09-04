"""Compatibility-preserving roam shim that finalizes closed ATES runs."""
from __future__ import annotations

import inspect
import sys
from functools import wraps

from argus.ates import RunStatus, recover_revision_one
from argus.engine import roam_ates_impl as _impl
from argus.engine.ates_runtime import resolve_runtime_project_dir
_original_roam = _impl.roam
_original_exit_code = _impl.roam_exit_code
_ROAM_SIGNATURE = inspect.signature(_original_roam)


def _public_status(status: RunStatus) -> str:
    return {
        RunStatus.PASSED: "pass",
        RunStatus.FAILED: "fail",
        RunStatus.ERROR: "error",
        RunStatus.CANCELLED: "cancelled",
    }[status]


def _bound_arguments(args, kwargs):
    try:
        return _ROAM_SIGNATURE.bind(*args, **kwargs).arguments
    except TypeError:
        return {}


@wraps(_original_roam)
def _finalizing_roam(*args, **kwargs):
    session = _original_roam(*args, **kwargs)
    run_id = getattr(session, "ates_run_id", None)
    if not run_id:
        return session

    original_status = str(getattr(session, "execution_status", "") or "")
    original_reason = str(getattr(session, "stopped_reason", "") or "")
    bound = _bound_arguments(args, kwargs)
    session_dir = bound.get("session_dir")
    project_dir = bound.get("project_dir")
    try:
        root = resolve_runtime_project_dir(
            project_dir,
            session_dir=session_dir,
        )
        finalized = recover_revision_one(root, run_id)
    except Exception as exc:
        session.execution_status = "error"
        finalization_reason = (
            f"ATES finalization failed: {type(exc).__name__}: {exc}"
        )
        if original_reason:
            session.stopped_reason = (
                f"{original_reason}; {finalization_reason}"
            )
        else:
            session.stopped_reason = finalization_reason
        return session

    session.ates_effective_status = finalized.outcome.effective_status.value
    session.ates_finalization_id = str(finalized.outcome.finalization_id)
    session.execution_status = _public_status(
        finalized.outcome.effective_status
    )

    if original_status == "error" and "ATES evidence failure" in original_reason:
        session.execution_status = "error"
        session.stopped_reason = original_reason
    return session


@wraps(_original_exit_code)
def _finalizing_exit_code(session):
    status = str(getattr(session, "execution_status", "") or "")
    if status in {"fail", "error", "outcome_unknown", "cancelled"}:
        return 1
    return _original_exit_code(session)


_finalizing_roam.__signature__ = _ROAM_SIGNATURE
_impl.roam = _finalizing_roam
_impl.roam_exit_code = _finalizing_exit_code
sys.modules[__name__] = _impl
