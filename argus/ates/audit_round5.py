"""Round-5 approval lifecycle/idempotency hardening for PR #22.

Round 3 made approval retries durable by deriving ``request_id`` from semantic
content.  That is sufficient only while the original approval generation is
still live: after a verified supersession/revocation, the same semantic approval
is a *new* intentional operation rather than a retry of the historical one.

This layer keeps crash retries convergent within one live generation while
starting a new signed request generation after a later verified+audited record
supersedes the matching historical approval.
"""
from __future__ import annotations

import hashlib
import sys
from datetime import datetime
from pathlib import Path
from typing import Mapping, Optional

from . import audit as _api
from . import audit_impl as _impl
from . import audit_round2 as _round2
from . import audit_round3 as _round3
from .core import EvidenceValue, VerificationStatus

_REQUEST_GENERATION_FIELD = "request_generation_after_approval_id"


def _is_committed_verified_superseder(
    record: Mapping[str, object],
    *,
    target_approval_id: str,
    audits_by_approval: Mapping[str, list[Mapping[str, object]]],
) -> bool:
    """Return true only for a durable authenticated operation superseding target.

    Append-time code does not possess every historical verification key, so it
    cannot re-authorize unrelated actors here.  It can, however, require the
    record to declare the authenticated/verified form and require the exact
    approval.changed audit binding that the consumer protocol already uses.
    Normal read-time ``validate_approvals`` remains the authority for signature
    and role verification.
    """
    if record.get("supersedes_approval_id") != target_approval_id:
        return False
    auth = record.get("authentication")
    if not isinstance(auth, Mapping) or auth.get("status") != VerificationStatus.VERIFIED.value:
        return False
    approval_id = record.get("approval_id")
    if not isinstance(approval_id, str):
        return False
    return _round2._audit_binding_error(
        record, list(audits_by_approval.get(approval_id, ()))
    ) is None


def _later_generation_terminator(
    approvals: tuple[dict[str, object], ...],
    candidate_index: int,
    candidate: Mapping[str, object],
    audits_by_approval: Mapping[str, list[Mapping[str, object]]],
) -> Optional[Mapping[str, object]]:
    approval_id = candidate.get("approval_id")
    if not isinstance(approval_id, str):
        return None
    latest: Optional[Mapping[str, object]] = None
    for record in approvals[candidate_index + 1 :]:
        if _is_committed_verified_superseder(
            record,
            target_approval_id=approval_id,
            audits_by_approval=audits_by_approval,
        ):
            latest = record
    return latest


def _bind_generation_request_identity(
    template: dict[str, object],
    *,
    authentication_key: Optional[bytes],
    explicit_occurred_at: bool,
    generation_after_approval_id: Optional[str],
) -> str:
    payload = _round3._request_identity_payload(
        template, explicit_occurred_at=explicit_occurred_at
    )
    payload["protocol"] = "ates-approval-request-v2"
    payload[_REQUEST_GENERATION_FIELD] = generation_after_approval_id
    digest = hashlib.sha256(_impl._canonical(payload, newline=False)).hexdigest()
    request_id = _round3._APPROVAL_REQUEST_PREFIX + digest

    template["request_id"] = request_id
    template[_REQUEST_GENERATION_FIELD] = generation_after_approval_id
    auth = template.get("authentication")
    if authentication_key is not None:
        if not isinstance(auth, dict):
            raise _impl.ApprovalError("authenticated approval metadata is malformed")
        # Both generation identity and request_id are authenticated immutable bytes.
        auth["signature"] = None
        auth["signature"] = _impl._sign_record(template, authentication_key)
    return request_id


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
    """Append/recover one approval operation without conflating later generations.

    A matching live request is treated as a retry and converges to the same
    durable approval.  Once a later verified+audited operation supersedes that
    approval, the same semantic call starts a new request generation whose
    identity is anchored to the superseding record.
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

    with _round2._ledger_transaction(root) as (pin, lock):
        approvals = _impl._read_jsonl(root, "approvals.jsonl")
        audits = _impl._read_jsonl(root, "audit.jsonl")
        audits_by_approval = _round2._audit_records_by_approval(audits)

        # Search newest-first.  A semantic match is a retry only while that
        # approval generation has not subsequently been superseded/revoked by a
        # verified+audited approval operation.
        generation_anchor: Optional[str] = None
        for index in range(len(approvals) - 1, -1, -1):
            candidate = approvals[index]
            if not _round2._approval_request_matches(
                candidate, template, authentication_key
            ):
                continue
            state = _round3._candidate_state(
                candidate, template, authentication_key, audits_by_approval
            )
            if state == "conflict":
                continue

            terminator = _later_generation_terminator(
                approvals, index, candidate, audits_by_approval
            )
            if terminator is None:
                if state == "pending":
                    _round2._append_approval_audit(root, pin, lock, candidate, audits)
                return candidate

            terminator_id = terminator.get("approval_id")
            if isinstance(terminator_id, str) and generation_anchor is None:
                generation_anchor = terminator_id

        # No live matching request exists, so this is a new intentional approval
        # generation.  The anchor is durable history and therefore remains stable
        # across retries of this new generation.
        request_id = _bind_generation_request_identity(
            template,
            authentication_key=authentication_key,
            explicit_occurred_at=occurred_at is not None,
            generation_after_approval_id=generation_anchor,
        )
        exact = [item for item in approvals if item.get("request_id") == request_id]
        if len(exact) > 1:
            raise _impl.ApprovalError(
                f"approval request identity {request_id!r} is duplicated"
            )
        if exact:
            candidate = exact[0]
            state = _round3._candidate_state(
                candidate, template, authentication_key, audits_by_approval
            )
            if state == "conflict":
                raise _impl.ApprovalError(
                    f"approval request identity {request_id!r} is bound to different immutable approval bytes"
                )
            if state == "pending":
                _round2._append_approval_audit(root, pin, lock, candidate, audits)
            return candidate

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


def install() -> None:
    replacements = {
        "append_approval": append_approval,
        "revoke_approval": revoke_approval,
    }
    for name, value in replacements.items():
        setattr(_round2, name, value)
        setattr(_round3, name, value)
        setattr(_impl, name, value)
        setattr(_api, name, value)

    parent = sys.modules.get(__package__)
    if parent is not None:
        for name, value in replacements.items():
            setattr(parent, name, value)


__all__ = ["append_approval", "revoke_approval", "install"]
