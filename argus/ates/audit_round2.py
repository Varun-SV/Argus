"""Round-2 detached-ledger transaction hardening for PR #22.

The audit and approval ledgers are separate append-only files, so atomicity is
expressed as a consumer-visible commit protocol rather than pretending two file
appends can be one filesystem operation.  A verified approval is effective only
when an exact ``approval.changed`` audit record binds its immutable bytes.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional

from . import audit as _api
from . import audit_impl as _impl
from .core import EvidenceValue, VerificationStatus, to_json_compatible
from .store import AtesStoreBusy, AtesStoreError, _PinnedDirectory, _WriterLock, _open_regular_file

_LOCK_WAIT_SECONDS = 5.0
_LOCK_RETRY_SECONDS = 0.01

_base_authentication_status = _api._authentication_status
_base_validate_audit_chain = _api.validate_audit_chain


def _approval_digest(record: Mapping[str, object]) -> str:
    return "sha256:" + hashlib.sha256(
        _impl._canonical(record, newline=False)
    ).hexdigest()


@contextmanager
def _ledger_transaction(root: Path):
    """Hold the canonical run-scoped writer authority for a full ledger transaction."""
    deadline = time.monotonic() + _LOCK_WAIT_SECONDS
    pin = None
    lock = None
    while True:
        pin = _PinnedDirectory(root)
        try:
            lock = _WriterLock(pin)
            lock.assert_authoritative()
            break
        except AtesStoreBusy as exc:
            try:
                pin.close()
            except BaseException:
                pass
            pin = None
            if time.monotonic() >= deadline:
                raise _impl.ApprovalError(
                    "timed out waiting for detached-ledger writer authority"
                ) from exc
            time.sleep(_LOCK_RETRY_SECONDS)
        except BaseException:
            try:
                pin.close()
            except BaseException:
                pass
            raise
    try:
        yield pin, lock
    finally:
        if lock is not None:
            try:
                lock.close()
            except BaseException:
                pass
        if pin is not None:
            try:
                pin.close()
            except BaseException:
                pass


def _append_line_held(
    pin: _PinnedDirectory,
    lock: _WriterLock,
    name: str,
    line: bytes,
) -> None:
    """Append while the caller retains run-scoped authority across the transaction."""
    handle = None
    try:
        lock.assert_authoritative()
        handle, created = _open_regular_file(pin, name)
        handle.seek(0, os.SEEK_END)
        before = handle.tell()
        if before:
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) != b"\n":
                raise _impl.ApprovalError(f"{name} has an unterminated trailing record")
            handle.seek(0, os.SEEK_END)
        written = handle.write(line)
        if written != len(line):
            raise _impl.ApprovalError(f"short append to detached ledger {name}")
        handle.flush()
        os.fsync(handle.fileno())
        lock.assert_authoritative()
        pin.assert_file_identity(name, handle.fileno(), f"detached ledger {name}")
        if os.fstat(handle.fileno()).st_size != before + len(line):
            raise _impl.ApprovalError(f"detached ledger {name} changed during append")
        if created:
            pin.fsync()
        lock.assert_authoritative()
    except _impl.ApprovalError:
        raise
    except (OSError, AtesStoreError) as exc:
        raise _impl.ApprovalError(f"cannot append detached ledger {name} safely") from exc
    finally:
        if handle is not None:
            try:
                handle.close()
            except BaseException:
                pass


def _normalize_audit_inputs(
    event_type: str,
    actor: str,
    details: Mapping[str, object],
    occurred_at: Optional[datetime],
    dedupe_key: Optional[str],
):
    if not isinstance(event_type, str) or not event_type.strip():
        raise _impl.ApprovalError("audit event_type must be a non-empty string")
    if not isinstance(actor, str) or not actor.strip():
        raise _impl.ApprovalError("audit actor must be a non-empty string")
    if not isinstance(details, Mapping):
        raise _impl.ApprovalError("audit details must be a mapping")
    if dedupe_key is not None and (
        not isinstance(dedupe_key, str) or not dedupe_key.strip()
    ):
        raise _impl.ApprovalError("audit dedupe_key must be a non-empty string when supplied")
    when = occurred_at or datetime.now(timezone.utc)
    if when.tzinfo is None:
        raise _impl.ApprovalError("audit timestamp must be timezone-aware")
    converted = to_json_compatible(dict(details))
    if not isinstance(converted, dict):
        raise _impl.ApprovalError("audit details did not normalize to an object")
    return when.astimezone(timezone.utc), converted


def _build_audit_record(
    records: tuple[dict[str, object], ...],
    *,
    event_type: str,
    actor: str,
    details: Mapping[str, object],
    occurred_at: datetime,
    dedupe_key: Optional[str],
) -> dict[str, object]:
    previous_digest = None if not records else _impl._audit_digest(records[-1])
    return {
        "ledger_version": _impl.AUDIT_LEDGER_VERSION,
        "audit_id": "AUDIT-" + uuid.uuid4().hex,
        "event_type": event_type,
        "actor": actor,
        "occurred_at": occurred_at.isoformat(),
        "previous_record_digest": previous_digest,
        "dedupe_key": dedupe_key,
        "details": dict(details),
    }


def append_audit_event(
    run_dir: Path | str,
    event_type: str,
    *,
    actor: str,
    details: Mapping[str, object],
    occurred_at: Optional[datetime] = None,
    dedupe_key: Optional[str] = None,
) -> Mapping[str, object]:
    """Append one audit event with read/dedupe/hash/append under one authority lock."""
    root = _impl._run_root(run_dir)
    _impl.ensure_detached_ledgers(root)
    when, normalized_details = _normalize_audit_inputs(
        event_type, actor, details, occurred_at, dedupe_key
    )
    with _ledger_transaction(root) as (pin, lock):
        records = _impl._read_jsonl(root, "audit.jsonl")
        if dedupe_key is not None:
            for record in records:
                if record.get("dedupe_key") == dedupe_key:
                    return record
        record = _build_audit_record(
            records,
            event_type=event_type,
            actor=actor,
            details=normalized_details,
            occurred_at=when,
            dedupe_key=dedupe_key,
        )
        _append_line_held(pin, lock, "audit.jsonl", _impl._canonical(record))
        return record


def _audit_records_by_approval(
    records: tuple[Mapping[str, object], ...],
) -> dict[str, list[Mapping[str, object]]]:
    result: dict[str, list[Mapping[str, object]]] = {}
    for record in records:
        if record.get("event_type") != "approval.changed":
            continue
        details = record.get("details")
        if not isinstance(details, Mapping):
            continue
        approval_id = details.get("approval_id")
        if isinstance(approval_id, str):
            result.setdefault(approval_id, []).append(record)
    return result


def _audit_binding_error(
    approval: Mapping[str, object],
    candidates: list[Mapping[str, object]],
) -> Optional[str]:
    """Return None only for one exact audit binding of this approval record."""
    if not candidates:
        return "authenticated approval is pending its required audit record"
    if len(candidates) != 1:
        return "approval has ambiguous duplicate audit bindings"
    audit = candidates[0]
    details = audit.get("details")
    if not isinstance(details, Mapping):
        return "approval audit binding details are malformed"
    approval_id = approval.get("approval_id")
    expected = {
        "approval_id": approval_id,
        "approval_record_digest": _approval_digest(approval),
        "action": approval.get("action"),
        "supersedes_approval_id": approval.get("supersedes_approval_id"),
        "verification_status": (
            approval.get("authentication", {}).get("status")
            if isinstance(approval.get("authentication"), Mapping)
            else None
        ),
    }
    for key, value in expected.items():
        if details.get(key) != value:
            return f"approval audit binding disagrees on {key}"
    if audit.get("actor") != approval.get("actor"):
        return "approval audit actor does not match the approval actor"
    if audit.get("dedupe_key") != f"approval:{approval_id}":
        return "approval audit binding has the wrong dedupe key"
    return None


def _approval_request_matches(
    record: Mapping[str, object],
    template: Mapping[str, object],
    authentication_key: Optional[bytes],
) -> bool:
    """Recognize the durable approval half of a previously interrupted request."""
    for key in (
        "ledger_version",
        "run_id",
        "finalization_id",
        "evidence_revision",
        "manifest_revision",
        "manifest_digest",
        "role",
        "actor",
        "action",
        "reason",
        "supersedes_approval_id",
    ):
        if record.get(key) != template.get(key):
            return False
    auth = record.get("authentication")
    template_auth = template.get("authentication")
    if not isinstance(auth, Mapping) or not isinstance(template_auth, Mapping):
        return False
    if auth.get("status") != template_auth.get("status"):
        return False
    if auth.get("method") != template_auth.get("method"):
        return False
    if auth.get("key_id") != template_auth.get("key_id"):
        return False
    if authentication_key is None:
        return auth.get("signature") is None
    signature = auth.get("signature")
    if not isinstance(signature, str):
        return False
    try:
        expected = _impl._sign_record(record, authentication_key)
    except _impl.ApprovalError:
        return False
    return hmac.compare_digest(signature, expected)


def _append_approval_audit(
    root: Path,
    pin: _PinnedDirectory,
    lock: _WriterLock,
    approval: Mapping[str, object],
    audit_records: tuple[dict[str, object], ...],
) -> Mapping[str, object]:
    approval_id = approval.get("approval_id")
    if not isinstance(approval_id, str):
        raise _impl.ApprovalError("cannot audit an approval without a valid approval_id")
    when = datetime.now(timezone.utc)
    details = {
        "approval_id": approval_id,
        "approval_record_digest": _approval_digest(approval),
        "action": approval.get("action"),
        "supersedes_approval_id": approval.get("supersedes_approval_id"),
        "verification_status": (
            approval.get("authentication", {}).get("status")
            if isinstance(approval.get("authentication"), Mapping)
            else None
        ),
    }
    record = _build_audit_record(
        audit_records,
        event_type="approval.changed",
        actor=str(approval.get("actor")),
        details=details,
        occurred_at=when,
        dedupe_key=f"approval:{approval_id}",
    )
    _append_line_held(pin, lock, "audit.jsonl", _impl._canonical(record))
    return record


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
    """Append an approval with a recoverable audit-pair commit protocol.

    The approval bytes are written first, but consumers will not treat them as
    effective until the exact matching audit binding is present.  If the audit
    append fails, retrying the same semantic request repairs the pending record
    rather than appending another approval.
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

    with _ledger_transaction(root) as (pin, lock):
        approvals = _impl._read_jsonl(root, "approvals.jsonl")
        audits = _impl._read_jsonl(root, "audit.jsonl")
        audits_by_approval = _audit_records_by_approval(audits)

        # Repair a durable approval whose required audit half was interrupted.
        for candidate in reversed(approvals):
            approval_id = candidate.get("approval_id")
            if not isinstance(approval_id, str) or approval_id in audits_by_approval:
                continue
            if _approval_request_matches(candidate, template, authentication_key):
                _append_approval_audit(root, pin, lock, candidate, audits)
                return candidate

        if supersedes_approval_id is not None and not any(
            item.get("approval_id") == supersedes_approval_id for item in approvals
        ):
            raise _impl.ApprovalError("superseded approval does not exist in this ledger")

        _append_line_held(pin, lock, "approvals.jsonl", _impl._canonical(template))
        # If this second append fails, the approval remains durable but pending;
        # validate_approvals() will never expose it as effective without this bind.
        _append_approval_audit(root, pin, lock, template, audits)
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


def validate_approvals(
    run_dir: Path | str,
    *,
    key_resolver=None,
):
    """Apply authenticated approval history only when its audit binding is exact."""
    root, result, manifest_digest = _impl._manifest_identity(run_dir)
    raw_records = _impl._read_jsonl(root, "approvals.jsonl")
    audit_records = tuple(_base_validate_audit_chain(root))
    audit_by_approval = _audit_records_by_approval(audit_records)
    seen: dict[str, Mapping[str, object]] = {}
    authoritative: dict[str, bool] = {}
    validations = []

    for record in raw_records:
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

        if structural_error is None and isinstance(approval_id, str):
            seen[approval_id] = record
            supersedes = record.get("supersedes_approval_id")
            if supersedes is not None and (
                not isinstance(supersedes, str)
                or supersedes not in seen
                or supersedes == approval_id
            ):
                structural_error = "approval supersession target is invalid or not historical"

        status, reason_text = (
            (VerificationStatus.INVALID, structural_error)
            if structural_error is not None
            else _base_authentication_status(record, key_resolver)
        )
        audited = False
        if status is VerificationStatus.VERIFIED and isinstance(approval_id, str):
            audit_error = _audit_binding_error(
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
    """Install transaction-safe behavior into implementation, public API, and parent package."""
    replacements = {
        "append_approval": append_approval,
        "append_audit_event": append_audit_event,
        "record_finalization_audit": record_finalization_audit,
        "revoke_approval": revoke_approval,
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
        reports_runtime.validate_approvals = validate_approvals
        reports_runtime.validate_audit_chain = _base_validate_audit_chain


__all__ = [
    "append_approval",
    "append_audit_event",
    "record_finalization_audit",
    "revoke_approval",
    "validate_approvals",
    "install",
]
