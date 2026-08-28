"""Round-8 complete approval-record structural validation for PR #22."""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from . import audit_impl as _impl
from . import audit_round3 as _round3
from . import audit_round7 as _round7
from .core import EvidenceValue

_APPROVAL_FIELDS = frozenset(
    {
        "ledger_version", "approval_id", "run_id", "finalization_id",
        "evidence_revision", "manifest_revision", "manifest_digest", "role",
        "actor", "action", "occurred_at", "reason", "supersedes_approval_id",
        "authentication", "request_id", "request_generation_after_approval_id",
    }
)
_AUTH_FIELDS = frozenset({"status", "method", "key_id", "signature"})
_EVIDENCE_FIELDS = frozenset(
    {"disposition", "value", "reason", "secret_refs", "protected_ref"}
)


def _reason_error(value: object):
    if value is None:
        return None
    if not isinstance(value, Mapping):
        return "approval reason must be privacy-classified evidence"
    extra = set(value) - _EVIDENCE_FIELDS
    if extra:
        return "approval reason contains unexpected fields"
    refs = value.get("secret_refs", ())
    if isinstance(refs, (str, bytes, bytearray, Mapping)) or not isinstance(refs, Sequence):
        return "approval reason secret_refs are malformed"
    try:
        EvidenceValue(
            disposition=value.get("disposition"),
            value=value.get("value"),
            reason=value.get("reason"),
            secret_refs=tuple(refs),
            protected_ref=value.get("protected_ref"),
        )
    except (TypeError, ValueError):
        return "approval reason is invalid"
    return None


def _request_shape_error(record: Mapping[str, object], seen):
    request_id = record.get("request_id")
    generation = record.get("request_generation_after_approval_id")
    if request_id is not None:
        if not isinstance(request_id, str) or not request_id.startswith(_round3._APPROVAL_REQUEST_PREFIX):
            return "approval request_id is invalid"
        suffix = request_id[len(_round3._APPROVAL_REQUEST_PREFIX):]
        if len(suffix) != 64:
            return "approval request_id is invalid"
        try:
            int(suffix, 16)
        except ValueError:
            return "approval request_id is invalid"
    if generation is not None:
        if not isinstance(generation, str) or not _impl._APPROVAL_ID_RE.fullmatch(generation):
            return "approval request generation anchor is invalid"
        if generation not in seen:
            return "approval request generation anchor is not historical"
    return None


def _approval_structural_error(
    record: Mapping[str, object],
    *,
    result,
    manifest_digest: str,
    seen: dict[str, Mapping[str, object]],
):
    if not isinstance(record, Mapping):
        return "approval record is malformed"
    extra = set(record) - _APPROVAL_FIELDS
    if extra:
        return "approval record contains unexpected fields"

    auth = record.get("authentication")
    if not isinstance(auth, Mapping):
        return "authentication metadata is malformed"
    if set(auth) - _AUTH_FIELDS:
        return "authentication metadata contains unexpected fields"

    reason_error = _reason_error(record.get("reason"))
    if reason_error is not None:
        return reason_error

    request_error = _request_shape_error(record, seen)
    if request_error is not None:
        return request_error

    action = record.get("action")
    supersedes = record.get("supersedes_approval_id")
    if action == _impl.ApprovalAction.REVOKE.value and supersedes is None:
        return "approval revocation must identify the record being revoked"

    # Run the previous current-package/history validator only after extension,
    # privacy, authentication-container, and request-generation shape checks.
    return _previous_structural_error(
        record,
        result=result,
        manifest_digest=manifest_digest,
        seen=seen,
    )


_previous_structural_error = _round7._approval_structural_error


def install() -> None:
    # round7.validate_approvals resolves this name from its module globals and
    # round5 request-generation recovery imports the same helper dynamically.
    _round7._approval_structural_error = _approval_structural_error


__all__ = ["install"]
