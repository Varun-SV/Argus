"""Round-3 detached-ledger idempotency hardening for PR #22.

Round 2 established one run-scoped transaction boundary and a recoverable
approval/audit pair.  This layer closes the two remaining ambiguity cases:

* an audit dedupe key is idempotent only when the stored record is the same
  semantic event, never merely because the key collides;
* approval changes carry a durable deterministic request identity so a retry
  after a committed-but-unacknowledged audit append converges on the original
  approval instead of creating a second effective approval.
"""
from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional

from . import audit as _api
from . import audit_impl as _impl
from . import audit_round2 as _round2
from .core import EvidenceValue, to_json_compatible

_APPROVAL_REQUEST_PREFIX = "APRREQ-"


def _audit_semantics_match(
    record: Mapping[str, object],
    *,
    event_type: str,
    actor: str,
    details: Mapping[str, object],
    occurred_at: Optional[datetime],
) -> bool:
    """Compare the semantic operation represented by one dedupe-key record."""
    if record.get("event_type") != event_type or record.get("actor") != actor:
        return False
    stored_details = record.get("details")
    if not isinstance(stored_details, Mapping) or dict(stored_details) != dict(details):
        return False
    # A caller-supplied timestamp is part of the requested operation.  When the
    # timestamp was omitted, occurred_at is generated metadata and therefore is
    # intentionally not part of idempotency comparison.
    if occurred_at is not None:
        if occurred_at.tzinfo is None:
            return False
        expected = occurred_at.astimezone(timezone.utc).isoformat()
        if record.get("occurred_at") != expected:
            return False
    return True


def append_audit_event(
    run_dir: Path | str,
    event_type: str,
    *,
    actor: str,
    details: Mapping[str, object],
    occurred_at: Optional[datetime] = None,
    dedupe_key: Optional[str] = None,
) -> Mapping[str, object]:
    """Append an audit event with semantic, fail-closed dedupe idempotency."""
    root = _impl._run_root(run_dir)
    _impl.ensure_detached_ledgers(root)
    when, normalized_details = _round2._normalize_audit_inputs(
        event_type, actor, details, occurred_at, dedupe_key
    )
    with _round2._ledger_transaction(root) as (pin, lock):
        records = _impl._read_jsonl(root, "audit.jsonl")
        if dedupe_key is not None:
            matches = [record for record in records if record.get("dedupe_key") == dedupe_key]
            if len(matches) > 1:
                raise _impl.ApprovalError(
                    f"audit dedupe conflict: key {dedupe_key!r} is duplicated"
                )
            if matches:
                existing = matches[0]
                if not _audit_semantics_match(
                    existing,
                    event_type=event_type,
                    actor=actor,
                    details=normalized_details,
                    occurred_at=occurred_at,
                ):
                    raise _impl.ApprovalError(
                        f"audit dedupe conflict: key {dedupe_key!r} is already bound "
                        "to a different semantic event"
                    )
                return existing
        record = _round2._build_audit_record(
            records,
            event_type=event_type,
            actor=actor,
            details=normalized_details,
            occurred_at=when,
            dedupe_key=dedupe_key,
        )
        _round2._append_line_held(pin, lock, "audit.jsonl", _impl._canonical(record))
        return record


def _request_identity_payload(
    template: Mapping[str, object],
    *,
    explicit_occurred_at: bool,
) -> dict[str, object]:
    auth = template.get("authentication")
    auth_identity = {
        "status": auth.get("status"),
        "method": auth.get("method"),
        "key_id": auth.get("key_id"),
    } if isinstance(auth, Mapping) else None
    payload: dict[str, object] = {
        "protocol": "ates-approval-request-v1",
        "ledger_version": template.get("ledger_version"),
        "run_id": template.get("run_id"),
        "finalization_id": template.get("finalization_id"),
        "evidence_revision": template.get("evidence_revision"),
        "manifest_revision": template.get("manifest_revision"),
        "manifest_digest": template.get("manifest_digest"),
        "role": template.get("role"),
        "actor": template.get("actor"),
        "action": template.get("action"),
        "reason": template.get("reason"),
        "supersedes_approval_id": template.get("supersedes_approval_id"),
        "authentication": auth_identity,
    }
    if explicit_occurred_at:
        payload["requested_occurred_at"] = template.get("occurred_at")
    return payload


def _approval_request_id(
    template: Mapping[str, object],
    *,
    explicit_occurred_at: bool,
) -> str:
    payload = _request_identity_payload(
        template, explicit_occurred_at=explicit_occurred_at
    )
    digest = hashlib.sha256(_impl._canonical(payload, newline=False)).hexdigest()
    return _APPROVAL_REQUEST_PREFIX + digest


def _bind_request_identity(
    template: dict[str, object],
    *,
    authentication_key: Optional[bytes],
    explicit_occurred_at: bool,
) -> str:
    request_id = _approval_request_id(
        template, explicit_occurred_at=explicit_occurred_at
    )
    template["request_id"] = request_id
    auth = template.get("authentication")
    if authentication_key is not None:
        if not isinstance(auth, dict):
            raise _impl.ApprovalError("authenticated approval metadata is malformed")
        # The request identity itself is part of authenticated immutable bytes.
        auth["signature"] = None
        auth["signature"] = _impl._sign_record(template, authentication_key)
    return request_id


def _candidate_state(
    candidate: Mapping[str, object],
    template: Mapping[str, object],
    authentication_key: Optional[bytes],
    audits_by_approval: Mapping[str, list[Mapping[str, object]]],
) -> str:
    if not _round2._approval_request_matches(candidate, template, authentication_key):
        return "conflict"
    approval_id = candidate.get("approval_id")
    if not isinstance(approval_id, str):
        return "conflict"
    audit_error = _round2._audit_binding_error(
        candidate, list(audits_by_approval.get(approval_id, ()))
    )
    if audit_error is None:
        return "committed"
    if audit_error.startswith("authenticated approval is pending"):
        return "pending"
    raise _impl.ApprovalError(
        f"approval request {candidate.get('request_id')!r} has invalid audit state: "
        f"{audit_error}"
    )


def append_approval(
    run_dir: Path | str,
    *,
    actor: str,
    role: str,
    action=None,
    reason: Optional[EvidenceValue] = None,
    supersedes_approval_id: Optional[str] = None,
    key_id: Optional[str] = None,
    authentication_key: Optional[bytes] = None,
    occurred_at: Optional[datetime] = None,
) -> Mapping[str, object]:
    """Append or recover one semantic approval request exactly once.

    ``request_id`` is deterministic from the immutable requested operation and
    finalization binding.  It is stored in, and authenticated as part of, the
    approval record.  This lets a retry recognize both a pending half-commit and
    a fully committed operation whose success acknowledgement was lost.
    """
    if action is None:
        action = _impl.ApprovalAction.APPROVE
    root, _result, _manifest_digest = _impl._manifest_identity(run_dir)
    _impl.ensure_detached_ledgers(root)
    template = _impl._new_approval_record(
        root,
        actor=actor,
        role=role,
        action=action,
        reason=reason,
        supersedes_approval_id=supersedes_approval_id,
        key_id=key_id,
        authentication_key=authentication_key,
        occurred_at=occurred_at,
    )
    request_id = _bind_request_identity(
        template,
        authentication_key=authentication_key,
        explicit_occurred_at=occurred_at is not None,
    )

    with _round2._ledger_transaction(root) as (pin, lock):
        approvals = _impl._read_jsonl(root, "approvals.jsonl")
        audits = _impl._read_jsonl(root, "audit.jsonl")
        audits_by_approval = _round2._audit_records_by_approval(audits)

        request_matches = [
            candidate
            for candidate in approvals
            if candidate.get("request_id") == request_id
        ]
        if len(request_matches) > 1:
            raise _impl.ApprovalError(
                f"approval request identity {request_id!r} is duplicated"
            )
        if request_matches:
            candidate = request_matches[0]
            state = _candidate_state(
                candidate, template, authentication_key, audits_by_approval
            )
            if state == "conflict":
                raise _impl.ApprovalError(
                    f"approval request identity {request_id!r} is bound to "
                    "different immutable approval bytes"
                )
            if state == "pending":
                _round2._append_approval_audit(root, pin, lock, candidate, audits)
            return candidate

        # Backward-compatible convergence for a round-2 record created before
        # request_id existed.  Exact semantic matching is still required.
        for candidate in reversed(approvals):
            if candidate.get("request_id") is not None:
                continue
            if not _round2._approval_request_matches(
                candidate, template, authentication_key
            ):
                continue
            approval_id = candidate.get("approval_id")
            if not isinstance(approval_id, str):
                continue
            audit_error = _round2._audit_binding_error(
                candidate, audits_by_approval.get(approval_id, [])
            )
            if audit_error is None:
                return candidate
            if audit_error.startswith("authenticated approval is pending"):
                _round2._append_approval_audit(root, pin, lock, candidate, audits)
                return candidate
            raise _impl.ApprovalError(
                f"matching legacy approval has invalid audit state: {audit_error}"
            )

        if supersedes_approval_id is not None and not any(
            item.get("approval_id") == supersedes_approval_id for item in approvals
        ):
            raise _impl.ApprovalError("superseded approval does not exist in this ledger")

        _round2._append_line_held(
            pin, lock, "approvals.jsonl", _impl._canonical(template)
        )
        _round2._append_approval_audit(root, pin, lock, template, audits)
        return template


def revoke_approval(
    run_dir: Path | str,
    approval_id: str,
    *,
    actor: str,
    role: str,
    reason: Optional[EvidenceValue] = None,
    key_id: Optional[str] = None,
    authentication_key: Optional[bytes] = None,
) -> Mapping[str, object]:
    return append_approval(
        run_dir,
        actor=actor,
        role=role,
        action=_impl.ApprovalAction.REVOKE,
        reason=reason,
        supersedes_approval_id=approval_id,
        key_id=key_id,
        authentication_key=authentication_key,
    )


def record_finalization_audit(run_dir: Path | str) -> Mapping[str, object]:
    root, result, manifest_digest = _impl._manifest_identity(run_dir)
    return append_audit_event(
        root,
        "finalization.bound",
        actor="argus.finalizer",
        dedupe_key=f"finalization:{result.outcome.finalization_id}",
        occurred_at=result.outcome.finalized_at,
        details={
            "run_id": str(result.outcome.run_id),
            "finalization_id": str(result.outcome.finalization_id),
            "revision": result.outcome.revision,
            "evidence_revision": result.outcome.evidence_revision,
            "effective_status": result.outcome.effective_status.value,
            "manifest_digest": manifest_digest,
        },
    )


def install() -> None:
    replacements = {
        "append_approval": append_approval,
        "append_audit_event": append_audit_event,
        "record_finalization_audit": record_finalization_audit,
        "revoke_approval": revoke_approval,
    }
    for name, value in replacements.items():
        setattr(_round2, name, value)
        setattr(_impl, name, value)
        setattr(_api, name, value)

    parent = sys.modules.get(__package__)
    if parent is not None:
        for name, value in replacements.items():
            setattr(parent, name, value)


__all__ = [
    "append_approval",
    "append_audit_event",
    "record_finalization_audit",
    "revoke_approval",
    "install",
]
