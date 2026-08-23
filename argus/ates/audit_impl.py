"""Detached ATES approvals and append-oriented audit history.

Approval records intentionally live outside the immutable evidence-manifest bytes
that they approve.  A local approvals.jsonl file is only storage: a record is
considered authoritative only when its detached authentication can be verified
with independently supplied key material.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from .core import EvidenceValue, FinalizationId, RunId, VerificationStatus, to_json_compatible
from .store import AtesStoreError, _PinnedDirectory, _open_regular_file

APPROVAL_LEDGER_VERSION = "ates-approval-ledger-v1"
AUDIT_LEDGER_VERSION = "ates-audit-ledger-v1"
APPROVAL_AUTH_METHOD = "hmac-sha256"
_APPROVAL_ID_RE = re.compile(r"^APPROVAL-[0-9a-f]{32}$")
_AUDIT_ID_RE = re.compile(r"^AUDIT-[0-9a-f]{32}$")


class ApprovalError(RuntimeError):
    """Detached approval/audit state cannot be handled safely."""


class ApprovalAction(str, Enum):
    APPROVE = "approved"
    REJECT = "rejected"
    REVOKE = "revoked"


@dataclass(frozen=True)
class ApprovalValidation:
    record: Mapping[str, object]
    verification_status: VerificationStatus
    effective: bool
    reason: Optional[str] = None


@dataclass(frozen=True)
class ApprovalLedgerResult:
    records: tuple[ApprovalValidation, ...]
    effective_approval_ids: tuple[str, ...]

    @property
    def verified_approvals(self) -> tuple[ApprovalValidation, ...]:
        ids = set(self.effective_approval_ids)
        return tuple(
            item
            for item in self.records
            if item.verification_status is VerificationStatus.VERIFIED
            and item.effective
            and item.record.get("approval_id") in ids
            and item.record.get("action") == ApprovalAction.APPROVE.value
        )


KeyResolver = Callable[[str], Optional[bytes]]


def _canonical(value: object, *, newline: bool = True) -> bytes:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ApprovalError(f"detached audit value is not canonical JSON: {exc}") from exc
    return raw + (b"\n" if newline else b"")


def _strict_object(raw: bytes, label: str) -> dict[str, object]:
    def pairs(items):
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate key {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda item: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant {item}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ApprovalError(f"{label} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise ApprovalError(f"{label} must be a JSON object")
    if _canonical(value) != raw:
        raise ApprovalError(f"{label} is not in canonical persisted representation")
    return value


def _run_root(run_dir: Path | str) -> Path:
    try:
        root = Path(run_dir).resolve(strict=True)
    except OSError as exc:
        raise ApprovalError(f"cannot resolve ATES run directory: {exc}") from exc
    if root.parent.name != "runs" or root.parent.parent.name != ".argus":
        raise ApprovalError("approval ledger is not beneath a canonical .argus/runs directory")
    return root


def _pinned_bytes(root: Path, name: str, *, missing_ok: bool = False) -> bytes:
    from . import finalization_impl as finalization

    pin = None
    try:
        pin = _PinnedDirectory(root)
        try:
            return finalization._pinned_bytes(pin, name, f"detached ledger {name}")
        except finalization.FinalizationError:
            if missing_ok and not (root / name).exists():
                return b""
            raise
    except (OSError, AtesStoreError, finalization.FinalizationError) as exc:
        raise ApprovalError(f"cannot read detached ledger {name} safely") from exc
    finally:
        if pin is not None:
            try:
                pin.close()
            except BaseException:
                pass


def _ensure_file(root: Path, name: str) -> None:
    pin = None
    handle = None
    try:
        pin = _PinnedDirectory(root)
        handle, created = _open_regular_file(pin, name)
        if created:
            handle.flush()
            os.fsync(handle.fileno())
            pin.fsync()
        pin.assert_file_identity(name, handle.fileno(), f"detached ledger {name}")
    except (OSError, AtesStoreError) as exc:
        raise ApprovalError(f"cannot initialize detached ledger {name}") from exc
    finally:
        if handle is not None:
            try:
                handle.close()
            except BaseException:
                pass
        if pin is not None:
            try:
                pin.close()
            except BaseException:
                pass


def ensure_detached_ledgers(run_dir: Path | str) -> tuple[Path, Path]:
    """Create the detached ledgers without changing finalized evidence bytes."""
    root = _run_root(run_dir)
    _ensure_file(root, "approvals.jsonl")
    _ensure_file(root, "audit.jsonl")
    return root / "approvals.jsonl", root / "audit.jsonl"


def _append_line(root: Path, name: str, line: bytes) -> None:
    pin = None
    handle = None
    try:
        pin = _PinnedDirectory(root)
        handle, created = _open_regular_file(pin, name)
        handle.seek(0, os.SEEK_END)
        before = handle.tell()
        if before:
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) != b"\n":
                raise ApprovalError(f"{name} has an unterminated trailing record")
            handle.seek(0, os.SEEK_END)
        written = handle.write(line)
        if written != len(line):
            raise ApprovalError(f"short append to detached ledger {name}")
        handle.flush()
        os.fsync(handle.fileno())
        pin.assert_file_identity(name, handle.fileno(), f"detached ledger {name}")
        if os.fstat(handle.fileno()).st_size != before + len(line):
            raise ApprovalError(f"detached ledger {name} changed during append")
        if created:
            pin.fsync()
    except ApprovalError:
        raise
    except (OSError, AtesStoreError) as exc:
        raise ApprovalError(f"cannot append detached ledger {name} safely") from exc
    finally:
        if handle is not None:
            try:
                handle.close()
            except BaseException:
                pass
        if pin is not None:
            try:
                pin.close()
            except BaseException:
                pass


def _read_jsonl(root: Path, name: str) -> tuple[dict[str, object], ...]:
    raw = _pinned_bytes(root, name, missing_ok=True)
    if not raw:
        return ()
    if not raw.endswith(b"\n"):
        raise ApprovalError(f"{name} has an unterminated trailing record")
    records: list[dict[str, object]] = []
    for index, line in enumerate(raw.splitlines(keepends=True), 1):
        if line == b"\n":
            raise ApprovalError(f"{name} contains a blank record at line {index}")
        records.append(_strict_object(line, f"{name} line {index}"))
    return tuple(records)


def _manifest_identity(run_dir: Path | str):
    from .finalization import verify_finalized_run
    from . import finalization_impl as finalization

    root = _run_root(run_dir)
    result = verify_finalized_run(root)
    raw = _pinned_bytes(root / "manifests", "manifest-0001.json")
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    return root, result, digest


def _approval_unsigned(record: Mapping[str, object]) -> dict[str, object]:
    unsigned = dict(record)
    authentication = unsigned.get("authentication")
    if isinstance(authentication, Mapping):
        auth = dict(authentication)
        auth.pop("signature", None)
        unsigned["authentication"] = auth
    return unsigned


def _sign_record(record: Mapping[str, object], key: bytes) -> str:
    if not isinstance(key, (bytes, bytearray)) or len(key) < 16:
        raise ApprovalError("approval authentication key must contain at least 16 bytes")
    digest = hmac.new(bytes(key), _canonical(_approval_unsigned(record), newline=False), hashlib.sha256)
    return "hmac:" + digest.hexdigest()


def _new_approval_record(
    run_dir: Path | str,
    *,
    actor: str,
    role: str,
    action: ApprovalAction | str,
    reason: Optional[EvidenceValue] = None,
    supersedes_approval_id: Optional[str] = None,
    key_id: Optional[str] = None,
    authentication_key: Optional[bytes] = None,
    occurred_at: Optional[datetime] = None,
) -> dict[str, object]:
    root, result, manifest_digest = _manifest_identity(run_dir)
    del root
    try:
        normalized_action = action if isinstance(action, ApprovalAction) else ApprovalAction(action)
    except (TypeError, ValueError) as exc:
        raise ApprovalError("unsupported approval action") from exc
    if not isinstance(actor, str) or not actor.strip():
        raise ApprovalError("approval actor must be a non-empty string")
    if not isinstance(role, str) or not role.strip():
        raise ApprovalError("approval role must be a non-empty string")
    if reason is not None and not isinstance(reason, EvidenceValue):
        raise ApprovalError("approval reason must be an EvidenceValue")
    if supersedes_approval_id is not None and not _APPROVAL_ID_RE.fullmatch(supersedes_approval_id):
        raise ApprovalError("superseded approval_id is invalid")
    if normalized_action is ApprovalAction.REVOKE and supersedes_approval_id is None:
        raise ApprovalError("approval revocation must identify the record being revoked")
    if authentication_key is not None and (not isinstance(key_id, str) or not key_id.strip()):
        raise ApprovalError("authenticated approval requires a non-empty key_id")
    if authentication_key is None and key_id is not None:
        raise ApprovalError("key_id cannot be recorded without authentication key material")

    when = occurred_at or datetime.now(timezone.utc)
    if when.tzinfo is None:
        raise ApprovalError("approval timestamp must be timezone-aware")
    when = when.astimezone(timezone.utc)
    verification_status = (
        VerificationStatus.VERIFIED.value
        if authentication_key is not None
        else VerificationStatus.UNVERIFIED.value
    )
    authentication: dict[str, object] = {
        "status": verification_status,
        "method": APPROVAL_AUTH_METHOD if authentication_key is not None else None,
        "key_id": key_id,
        "signature": None,
    }
    record: dict[str, object] = {
        "ledger_version": APPROVAL_LEDGER_VERSION,
        "approval_id": "APPROVAL-" + uuid.uuid4().hex,
        "run_id": str(result.outcome.run_id),
        "finalization_id": str(result.outcome.finalization_id),
        "evidence_revision": result.outcome.evidence_revision,
        "manifest_revision": 1,
        "manifest_digest": manifest_digest,
        "role": actor if role is None else role,
        "actor": actor,
        "action": normalized_action.value,
        "occurred_at": when.isoformat(),
        "reason": None if reason is None else to_json_compatible(reason),
        "supersedes_approval_id": supersedes_approval_id,
        "authentication": authentication,
    }
    if authentication_key is not None:
        authentication["signature"] = _sign_record(record, authentication_key)
    return record


def append_approval(
    run_dir: Path | str,
    *,
    actor: str,
    role: str,
    action: ApprovalAction | str = ApprovalAction.APPROVE,
    reason: Optional[EvidenceValue] = None,
    supersedes_approval_id: Optional[str] = None,
    key_id: Optional[str] = None,
    authentication_key: Optional[bytes] = None,
    occurred_at: Optional[datetime] = None,
) -> Mapping[str, object]:
    """Append an approval/rejection/supersession bound to the current manifest.

    Passing ``authentication_key`` produces an HMAC-authenticated record.  The
    key is caller supplied and is never written into the run package.
    """
    root, _result, _manifest_digest = _manifest_identity(run_dir)
    ensure_detached_ledgers(root)
    existing = _read_jsonl(root, "approvals.jsonl")
    if supersedes_approval_id is not None and not any(
        item.get("approval_id") == supersedes_approval_id for item in existing
    ):
        raise ApprovalError("superseded approval does not exist in this ledger")
    record = _new_approval_record(
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
    _append_line(root, "approvals.jsonl", _canonical(record))
    append_audit_event(
        root,
        "approval.changed",
        actor=actor,
        details={
            "approval_id": record["approval_id"],
            "action": record["action"],
            "supersedes_approval_id": supersedes_approval_id,
            "verification_status": record["authentication"]["status"],  # type: ignore[index]
        },
    )
    return record


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
        action=ApprovalAction.REVOKE,
        reason=reason,
        supersedes_approval_id=approval_id,
        key_id=key_id,
        authentication_key=authentication_key,
    )


def _authentication_status(record: Mapping[str, object], resolver: Optional[KeyResolver]) -> tuple[VerificationStatus, Optional[str]]:
    auth = record.get("authentication")
    if not isinstance(auth, Mapping):
        return VerificationStatus.INVALID, "authentication metadata is malformed"
    declared = auth.get("status")
    method = auth.get("method")
    key_id = auth.get("key_id")
    signature = auth.get("signature")
    if declared == VerificationStatus.UNVERIFIED.value:
        if method is not None or key_id is not None or signature is not None:
            return VerificationStatus.INVALID, "unverified record carries authentication material"
        return VerificationStatus.UNVERIFIED, None
    if declared != VerificationStatus.VERIFIED.value or method != APPROVAL_AUTH_METHOD:
        return VerificationStatus.INVALID, "unsupported approval authentication state"
    if not isinstance(key_id, str) or not key_id or not isinstance(signature, str):
        return VerificationStatus.INVALID, "authenticated approval is missing key/signature metadata"
    if resolver is None:
        return VerificationStatus.UNVERIFIED, "verification key was not supplied"
    try:
        key = resolver(key_id)
    except BaseException:
        return VerificationStatus.UNVERIFIED, "verification key lookup failed"
    if key is None:
        return VerificationStatus.UNVERIFIED, "verification key is unavailable"
    try:
        expected = _sign_record(record, key)
    except ApprovalError as exc:
        return VerificationStatus.INVALID, str(exc)
    if not hmac.compare_digest(signature, expected):
        return VerificationStatus.INVALID, "approval authentication signature does not verify"
    return VerificationStatus.VERIFIED, None


def validate_approvals(
    run_dir: Path | str,
    *,
    key_resolver: Optional[KeyResolver] = None,
) -> ApprovalLedgerResult:
    root, result, manifest_digest = _manifest_identity(run_dir)
    ensure_detached_ledgers(root)
    raw_records = _read_jsonl(root, "approvals.jsonl")
    seen: dict[str, Mapping[str, object]] = {}
    validations: list[ApprovalValidation] = []
    authoritative: dict[str, bool] = {}

    for record in raw_records:
        approval_id = record.get("approval_id")
        structural_error: Optional[str] = None
        if record.get("ledger_version") != APPROVAL_LEDGER_VERSION:
            structural_error = "unsupported approval ledger version"
        elif not isinstance(approval_id, str) or not _APPROVAL_ID_RE.fullmatch(approval_id):
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
        elif record.get("action") not in {item.value for item in ApprovalAction}:
            structural_error = "approval action is invalid"
        elif not isinstance(record.get("actor"), str) or not str(record.get("actor")).strip():
            structural_error = "approval actor is invalid"
        elif not isinstance(record.get("role"), str) or not str(record.get("role")).strip():
            structural_error = "approval role is invalid"

        if structural_error is None:
            seen[approval_id] = record  # type: ignore[index]
            supersedes = record.get("supersedes_approval_id")
            if supersedes is not None and (
                not isinstance(supersedes, str) or supersedes not in seen or supersedes == approval_id
            ):
                structural_error = "approval supersession target is invalid or not historical"

        status, auth_reason = (
            (VerificationStatus.INVALID, structural_error)
            if structural_error is not None
            else _authentication_status(record, key_resolver)
        )
        effective = False
        if status is VerificationStatus.VERIFIED and isinstance(approval_id, str):
            supersedes = record.get("supersedes_approval_id")
            if isinstance(supersedes, str):
                authoritative.pop(supersedes, None)
            if record.get("action") != ApprovalAction.REVOKE.value:
                authoritative[approval_id] = True
                effective = True
        validations.append(
            ApprovalValidation(
                record=record,
                verification_status=status,
                effective=effective,
                reason=auth_reason,
            )
        )

    effective_ids = tuple(authoritative)
    # Recompute effective flags after later verified supersession/revocation.
    final_validations = tuple(
        ApprovalValidation(
            record=item.record,
            verification_status=item.verification_status,
            effective=item.record.get("approval_id") in authoritative,
            reason=item.reason,
        )
        for item in validations
    )
    return ApprovalLedgerResult(final_validations, effective_ids)


def _audit_digest(record: Mapping[str, object]) -> str:
    return "sha256:" + hashlib.sha256(_canonical(record, newline=False)).hexdigest()


def append_audit_event(
    run_dir: Path | str,
    event_type: str,
    *,
    actor: str,
    details: Mapping[str, object],
    occurred_at: Optional[datetime] = None,
    dedupe_key: Optional[str] = None,
) -> Mapping[str, object]:
    root = _run_root(run_dir)
    ensure_detached_ledgers(root)
    if not isinstance(event_type, str) or not event_type.strip():
        raise ApprovalError("audit event_type must be a non-empty string")
    if not isinstance(actor, str) or not actor.strip():
        raise ApprovalError("audit actor must be a non-empty string")
    when = occurred_at or datetime.now(timezone.utc)
    if when.tzinfo is None:
        raise ApprovalError("audit timestamp must be timezone-aware")
    records = _read_jsonl(root, "audit.jsonl")
    if dedupe_key is not None:
        for record in records:
            if record.get("dedupe_key") == dedupe_key:
                return record
    previous_digest = None if not records else _audit_digest(records[-1])
    record: dict[str, object] = {
        "ledger_version": AUDIT_LEDGER_VERSION,
        "audit_id": "AUDIT-" + uuid.uuid4().hex,
        "event_type": event_type,
        "actor": actor,
        "occurred_at": when.astimezone(timezone.utc).isoformat(),
        "previous_record_digest": previous_digest,
        "dedupe_key": dedupe_key,
        "details": to_json_compatible(dict(details)),
    }
    _append_line(root, "audit.jsonl", _canonical(record))
    return record


def record_finalization_audit(run_dir: Path | str) -> Mapping[str, object]:
    root, result, manifest_digest = _manifest_identity(run_dir)
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


def validate_audit_chain(run_dir: Path | str) -> tuple[Mapping[str, object], ...]:
    root = _run_root(run_dir)
    ensure_detached_ledgers(root)
    records = _read_jsonl(root, "audit.jsonl")
    previous = None
    seen: set[str] = set()
    for index, record in enumerate(records, 1):
        audit_id = record.get("audit_id")
        if record.get("ledger_version") != AUDIT_LEDGER_VERSION:
            raise ApprovalError(f"audit record {index} has unsupported ledger version")
        if not isinstance(audit_id, str) or not _AUDIT_ID_RE.fullmatch(audit_id) or audit_id in seen:
            raise ApprovalError(f"audit record {index} has invalid/duplicate audit_id")
        if record.get("previous_record_digest") != previous:
            raise ApprovalError(f"audit record {index} breaks the append hash chain")
        seen.add(audit_id)
        previous = _audit_digest(record)
    return records


__all__ = [
    "APPROVAL_AUTH_METHOD",
    "APPROVAL_LEDGER_VERSION",
    "AUDIT_LEDGER_VERSION",
    "ApprovalAction",
    "ApprovalError",
    "ApprovalLedgerResult",
    "ApprovalValidation",
    "append_approval",
    "append_audit_event",
    "ensure_detached_ledgers",
    "record_finalization_audit",
    "revoke_approval",
    "validate_approvals",
    "validate_audit_chain",
]
