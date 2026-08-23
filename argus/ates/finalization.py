"""Compatibility layer applying hardened verification/derivation fixes to PR #22 finalization."""
from __future__ import annotations

import hashlib
import os
import stat
import sys
from dataclasses import replace
from pathlib import Path

from . import finalization_impl as _impl
from .core import EventType, to_json_compatible


_raw_derive = _impl._derive
_raw_artifacts = _impl._artifacts
_raw_verify_finalized_run = _impl.verify_finalized_run


def _derive(events, run_id):
    """Derive canonical state and include the closed runtime's terminal classification."""
    state = _raw_derive(events, run_id)
    inputs = state.status_inputs
    for event in events:
        if event.envelope.event_type is not EventType.RUN_MARKED_INCOMPLETE:
            continue
        if event.payload.get("reason") != "runtime.finalization_pending":
            continue
        execution_result = str(event.payload.get("execution_result") or "").strip().lower()
        if execution_result in {"error", "outcome_unknown"}:
            inputs = replace(inputs, execution_error=True)
        elif execution_result in {"fail", "failed"}:
            inputs = replace(inputs, deterministic_failure=True)
        elif execution_result in {"cancelled", "canceled"}:
            inputs = replace(inputs, cancelled=True)
    return replace(state, status_inputs=inputs)


def _artifacts(store, records):
    """Return ordinary canonical JSON values after re-verifying retained artifacts."""
    converted = to_json_compatible(_raw_artifacts(store, records))
    if not isinstance(converted, list):
        raise _impl.FinalizationError("artifact manifest projection is malformed")
    return converted


def _preflight_evidence_bytes(root: Path) -> None:
    """Reject raw evidence mutation before parser/reopen errors obscure the cause."""
    manifest = _impl._read(root / "manifests" / "manifest-0001.json")
    evidence = manifest.get("evidence")
    if not isinstance(evidence, dict):
        raise _impl.FinalizationError("evidence manifest metadata is malformed")
    expected_size = evidence.get("size_bytes")
    expected_digest = evidence.get("sha256")
    if isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size < 0:
        raise _impl.FinalizationError("evidence manifest size is invalid")
    if not isinstance(expected_digest, str) or not expected_digest.startswith("sha256:"):
        raise _impl.FinalizationError("evidence manifest digest is invalid")

    evidence_path = root / "evidence.jsonl"
    try:
        info = evidence_path.lstat()
    except OSError as exc:
        raise _impl.FinalizationError("evidence size cannot be verified") from exc
    if not stat.S_ISREG(info.st_mode) or evidence_path.is_symlink():
        raise _impl.FinalizationError("evidence size cannot be verified safely")
    try:
        raw = evidence_path.read_bytes()
    except OSError as exc:
        raise _impl.FinalizationError("evidence size cannot be verified") from exc
    if len(raw) != expected_size:
        raise _impl.FinalizationError("evidence size does not match manifest")
    actual_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    if actual_digest != expected_digest:
        raise _impl.FinalizationError("evidence digest does not match manifest")


def verify_finalized_run(run_dir):
    try:
        root = Path(run_dir).resolve(strict=True)
    except OSError as exc:
        raise _impl.FinalizationError(f"cannot resolve finalized run directory: {exc}") from exc
    _preflight_evidence_bytes(root)
    return _raw_verify_finalized_run(root)


_impl._derive = _derive
_impl._artifacts = _artifacts
_impl.verify_finalized_run = verify_finalized_run
sys.modules[__name__] = _impl
