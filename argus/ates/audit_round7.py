"""Round-7 detached-ledger validation hardening for PR #22.

Read-only consumers must distinguish a genuinely absent detached ledger from an
unsafe directory entry, authenticated approvals need chronological provenance,
and a locally verified audit ledger must have unambiguous idempotency keys.
"""
from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import audit as _api
from . import audit_impl as _impl
from . import audit_round2 as _round2
from .core import VerificationStatus

_previous_pinned_bytes = _impl._pinned_bytes
_previous_validate_audit_chain = _impl.validate_audit_chain


def _pinned_bytes(root: Path, name: str, *, missing_ok: bool = False) -> bytes:
    """Treat only true no-entry absence as an optional empty ledger.

    ``Path.exists`` follows symlinks, so a dangling symlink previously looked
    absent after the pinned/no-follow read had correctly rejected it.  ``lexists``
    observes the directory entry itself and therefore preserves that rejection.
    """
    try:
        return _previous_pinned_bytes(root, name, missing_ok=False)
    except _impl.ApprovalError:
        if missing_ok and not os.path.lexists(os.fspath(Path(root) / name)):
            return b""
        raise


def _timestamp_error(record: Mapping[str, object]) -> Optional[str]:
    value = record.get("occurred_at")
    if not isinstance(value, str) or not value.strip():
        return "approval occurred_at is invalid"
    try:
        parsed = datetime.fromisoformat(value)
        offset = parsed.utcoffset() if parsed.tzinfo is not None else None
    except (TypeError, ValueError, OverflowError):
        return "approval occurred_at is invalid"
    if offset is None:
        return "approval occurred_at must be timezone-aware"
    return None


def _approval_structural_error(
    record: Mapping[str, object],
    *,
    result,
    manifest_digest: str,
    seen: dict[str, Mapping[str, object]],
) -> Optional[str]:
    """Validate one approval against the current immutable package.

    This is intentionally shared with approval-generation recovery.  A later
    signed/audited row must not terminate a live request generation unless the
    normal read-side validator would also accept its structure and package
    binding.
    """
    approval_id = record.get("approval_id")
    structural_error: Optional[str] = None
    if record.get("ledger_version") != _impl.APPROVAL_LEDGER_VERSION:
        structural_error = "unsupported approval ledger version"
    elif not isinstance(approval_id, str) or not _impl._APPROVAL_ID_RE.fullmatch(approval_id):
        structural_error = "approval_id is invalid"
    elif approval_id in seen:
        structural_error = "approval_id is duplicated"
    elif record.get("run_id") != str(result.outcome.run_id):
        structural_error = "approval is bound to another run"
    elif record.get("finalization_id") != str(result.outcome.finalization_id):
        structural_error = "approval is bound to another finalization"
    elif record.get("evidence_revision") != result.outcome.evidence_revision:
        structural_error = "approval evidence revision is stale"
    elif record.get("manifest_revision") != 1 or record.get("manifest_digest") != manifest_digest:
        structural_error = "approval manifest binding is stale or invalid"
    elif record.get("action") not in {item.value for item in _impl.ApprovalAction}:
        structural_error = "approval action is invalid"
    elif not isinstance(record.get("actor"), str) or not str(record.get("actor")).strip():
        structural_error = "approval actor is invalid"
    elif not isinstance(record.get("role"), str) or not str(record.get("role")).strip():
        structural_error = "approval role is invalid"
    else:
        structural_error = _timestamp_error(record)

    if structural_error is None and isinstance(approval_id, str):
        supersedes = record.get("supersedes_approval_id")
        if supersedes is not None and (
            not isinstance(supersedes, str)
            or supersedes not in seen
            or supersedes == approval_id
        ):
            structural_error = "approval supersession target is invalid or not historical"
        else:
            # Only fully valid structure can become historical evidence for a
            # later supersession or request-generation anchor.
            seen[approval_id] = record
    return structural_error


def validate_audit_chain(run_dir):
    records = tuple(_previous_validate_audit_chain(run_dir))
    seen_dedupe: set[str] = set()
    for index, record in enumerate(records, 1):
        dedupe_key = record.get("dedupe_key")
        if dedupe_key is None:
            continue
        # Round 6 already proves this is a non-empty string; keep a defensive
        # check so this wrapper remains fail-closed if installation order changes.
        if not isinstance(dedupe_key, str) or not dedupe_key.strip():
            raise _impl.ApprovalError(
                f"audit record {index} has invalid dedupe_key"
            )
        if dedupe_key in seen_dedupe:
            raise _impl.ApprovalError(
                f"audit record {index} has duplicate dedupe_key"
            )
        seen_dedupe.add(dedupe_key)
    return records


def validate_approvals(run_dir: Path | str, *, key_resolver=None):
    """Validate approval structure, timestamp, authentication, and audit bind."""
    root, result, manifest_digest = _impl._manifest_identity(run_dir)
    raw_records = _impl._read_jsonl(root, "approvals.jsonl")
    audit_records = tuple(validate_audit_chain(root))
    audit_by_approval = _round2._audit_records_by_approval(audit_records)
    seen: dict[str, Mapping[str, object]] = {}
    authoritative: dict[str, bool] = {}
    validations = []

    for record in raw_records:
        approval_id = record.get("approval_id")
        structural_error = _approval_structural_error(
            record,
            result=result,
            manifest_digest=manifest_digest,
            seen=seen,
        )

        status, reason_text = (
            (VerificationStatus.INVALID, structural_error)
            if structural_error is not None
            else _round2._base_authentication_status(record, key_resolver)
        )
        audited = False
        if status is VerificationStatus.VERIFIED and isinstance(approval_id, str):
            audit_error = _round2._audit_binding_error(
                record, audit_by_approval.get(approval_id, [])
            )
            if audit_error is None:
                audited = True
            elif audit_error.startswith("authenticated approval is pending"):
                reason_text = audit_error
            else:
                status = VerificationStatus.INVALID
                reason_text = audit_error

        if status is VerificationStatus.VERIFIED and audited and isinstance(approval_id, str):
            supersedes = record.get("supersedes_approval_id")
            if isinstance(supersedes, str):
                authoritative.pop(supersedes, None)
            if record.get("action") != _impl.ApprovalAction.REVOKE.value:
                authoritative[approval_id] = True

        validations.append(
            _impl.ApprovalValidation(record, status, False, reason_text)
        )

    final = tuple(
        _impl.ApprovalValidation(
            item.record,
            item.verification_status,
            item.record.get("approval_id") in authoritative,
            item.reason,
        )
        for item in validations
    )
    return _impl.ApprovalLedgerResult(final, tuple(authoritative))


def install() -> None:
    _impl._pinned_bytes = _pinned_bytes
    replacements = {
        "validate_audit_chain": validate_audit_chain,
        "validate_approvals": validate_approvals,
    }
    for name, value in replacements.items():
        setattr(_impl, name, value)
        setattr(_api, name, value)

    parent = sys.modules.get(__package__)
    if parent is not None:
        for name, value in replacements.items():
            setattr(parent, name, value)

    reports_runtime = sys.modules.get(f"{__package__}.reports_runtime")
    if reports_runtime is not None:
        reports_runtime.validate_audit_chain = validate_audit_chain
        reports_runtime.validate_approvals = validate_approvals


__all__ = [
    "_approval_structural_error",
    "install",
    "validate_audit_chain",
    "validate_approvals",
]
