"""Compatibility-preserving runner shim that finalizes closed ATES runs."""
from __future__ import annotations

import sys
from argus.ates import RunStatus, recover_revision_one
from argus.engine import runner_impl as _impl
from argus.engine.ates_runtime import resolve_runtime_project_dir

_original_run_test = _impl.run_test


def _public_status(status: RunStatus) -> str:
    return {RunStatus.PASSED: "pass", RunStatus.FAILED: "fail", RunStatus.ERROR: "error"}.get(status, "fail")


def _finalizing_run_test(*args, **kwargs):
    result = _original_run_test(*args, **kwargs)
    run_id = getattr(result, "ates_run_id", None)
    if not run_id:
        return result
    spec = kwargs.get("spec") if "spec" in kwargs else (args[0] if args else None)
    try:
        root = resolve_runtime_project_dir(kwargs.get("project_dir"), spec_path=getattr(spec, "path", None))
        finalized = recover_revision_one(root, run_id)
    except Exception as exc:
        result.status = "error"
        message = f"ATES finalization failed: {type(exc).__name__}: {exc}"
        result.error = f"{result.error}; {message}" if result.error and message not in result.error else (result.error or message)
        return result
    result.ates_effective_status = finalized.outcome.effective_status.value
    result.ates_finalization_id = str(finalized.outcome.finalization_id)
    result.status = _public_status(finalized.outcome.effective_status)
    return result


_impl.run_test = _finalizing_run_test
sys.modules[__name__] = _impl
