"""Detached approvals and audit history with one authoritative implementation.

Ledger writes hold run-scoped authority across read, dedupe, repair, and append.
An approval is effective only after independent authentication and an exact
audit binding; reader-side validation never repairs persisted bytes.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
import uuid
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Optional, Tuple

from .core import (
    EvidenceValue,
    FinalizationId,
    RunId,
    VerificationStatus,
    to_json_compatible,
)
from .store import (
    AtesStoreBusy,
    AtesStoreError,
    _PinnedDirectory,
    _RunDirectoryChain,
    _WriterLock,
    _open_regular_file,
)

APPROVAL_LEDGER_VERSION = "ates-approval-ledger-v1"
AUDIT_LEDGER_VERSION = "ates-audit-ledger-v1"
APPROVAL_AUTH_METHOD = "hmac-sha256"
_APPROVAL_ID_RE = re.compile(r"^APPROVAL-[0-9a-f]{32}$")
_AUDIT_ID_RE = re.compile(r"^AUDIT-[0-9a-f]{32}$")
_AUDIT_FIELDS = frozenset(
    {
        "ledger_version",
        "audit_id",
        "event_type",
        "actor",
        "occurred_at",
        "previous_record_digest",
        "dedupe_key",
        "details",
    }
)
_APPROVAL_REQUEST_PREFIX = "APRREQ-"
_REQUEST_GENERATION_FIELD = "request_generation_after_approval_id"
_LOCK_WAIT_SECONDS = 5.0
_LOCK_RETRY_SECONDS = 0.01
_ACTIVE_AUTHORITY: ContextVar[
    Optional[Tuple[Path, _RunDirectoryChain, _PinnedDirectory, _WriterLock]]
] = ContextVar(
    "ates_detached_ledger_authority", default=None
)



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


@dataclass(frozen=True)
class ApprovalCredential:
    """Independently trusted reviewer credential supplied by the consumer.

    The credential is never persisted in the ATES run.  Returning it from a
    resolver asserts that ``key_id`` authenticates exactly ``actor`` and may
    exercise one of ``roles``.  Merely possessing bytes with a matching HMAC is
    therefore insufficient to turn a claimed actor/role into an approval.
    """

    key_id: str
    key: bytes
    actor: str
    roles: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.key_id, str) or not self.key_id.strip():
            raise ValueError("approval credential key_id must be non-empty")
        if not isinstance(self.key, bytes) or len(self.key) < 16:
            raise ValueError("approval credential key must contain at least 16 bytes")
        if not isinstance(self.actor, str) or not self.actor.strip():
            raise ValueError("approval credential actor must be non-empty")
        if not isinstance(self.roles, tuple) or not self.roles:
            raise ValueError("approval credential requires at least one role")
        if any(not isinstance(role, str) or not role.strip() for role in self.roles):
            raise ValueError("approval credential roles must be non-empty strings")
        if len(set(self.roles)) != len(self.roles):
            raise ValueError("approval credential roles must be unique")


KeyResolver = Callable[[str], Optional[ApprovalCredential]]


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
    from . import finalization

    pin = None
    try:
        pin = _PinnedDirectory(root)
        try:
            return finalization._pinned_bytes(pin, name, f"detached ledger {name}")
        except finalization.FinalizationError:
            if missing_ok and not os.path.lexists(root / name):
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


def _repair_trailing_partial_held(
    pin: _PinnedDirectory,
    lock: _WriterLock,
    name: str,
) -> None:
    """Truncate only an unterminated final JSONL record under writer authority."""
    if name not in {"approvals.jsonl", "audit.jsonl"}:
        raise ApprovalError("detached-ledger tail repair was requested for an unsupported file")

    handle = None
    try:
        lock.assert_authoritative()
        handle, created = _open_regular_file(pin, name)
        pin.assert_file_identity(name, handle.fileno(), f"detached ledger {name}")
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        if size == 0:
            if created:
                pin.fsync()
            return

        handle.seek(-1, os.SEEK_END)
        if handle.read(1) == b"\n":
            return

        # The only repair contract is the final unterminated record.  Earlier
        # newline-terminated bytes are preserved byte-for-byte.
        handle.seek(0)
        raw = handle.read()
        truncate_to = raw.rfind(b"\n") + 1
        handle.truncate(truncate_to)
        handle.flush()
        os.fsync(handle.fileno())

        lock.assert_authoritative()
        pin.assert_file_identity(name, handle.fileno(), f"detached ledger {name}")
        if os.fstat(handle.fileno()).st_size != truncate_to:
            raise ApprovalError(
                f"detached ledger {name} tail repair did not reach the expected boundary"
            )
        # Directory durability is cheap here and also covers the edge case where
        # the ledger file itself was created by the interrupted operation.
        pin.fsync()
        lock.assert_authoritative()
    except ApprovalError:
        raise
    except (OSError, AtesStoreError) as exc:
        raise ApprovalError(
            f"cannot reconcile unterminated detached ledger {name} safely"
        ) from exc
    finally:
        if handle is not None:
            try:
                handle.close()
            except BaseException:
                pass


def _directory_identity(path: Path) -> tuple[int, int]:
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise ApprovalError("cannot establish detached-ledger directory identity") from exc
    return info.st_dev, info.st_ino


@contextmanager
def _ledger_transaction(
    root: Path,
    *,
    run_id: RunId,
    expected_identity: tuple[int, int],
):
    """Hold canonical run-namespace authority for a complete ledger transaction."""
    deadline = time.monotonic() + _LOCK_WAIT_SECONDS
    chain = None
    pin = None
    lock = None
    while True:
        try:
            chain = _RunDirectoryChain(root.parent.parent.parent, run_id)
            pin = chain.run
            if pin.path != root:
                raise ApprovalError(
                    "detached-ledger authority resolved to another run directory"
                )
            if os.name == "nt":
                pinned_identity = _directory_identity(pin.path)
            else:
                assert pin._fd is not None
                pinned = os.fstat(pin._fd)
                pinned_identity = (pinned.st_dev, pinned.st_ino)
            if pinned_identity != expected_identity:
                raise ApprovalError(
                    "detached-ledger run directory changed before authority was acquired"
                )
            lock = _WriterLock(pin)
            chain.assert_authoritative()
            lock.assert_authoritative()
            break
        except AtesStoreBusy as exc:
            if chain is not None:
                try:
                    chain.close()
                except BaseException:
                    pass
            chain = None
            pin = None
            lock = None
            if time.monotonic() >= deadline:
                raise ApprovalError(
                    "timed out waiting for detached-ledger writer authority"
                ) from exc
            time.sleep(_LOCK_RETRY_SECONDS)
        except BaseException:
            if lock is not None:
                try:
                    lock.close()
                except BaseException:
                    pass
            if chain is not None:
                try:
                    chain.close()
                except BaseException:
                    pass
            raise
    assert chain is not None and pin is not None and lock is not None
    token = _ACTIVE_AUTHORITY.set((root, chain, pin, lock))
    try:
        yield pin, lock
        chain.assert_authoritative()
        lock.assert_authoritative()
    except (OSError, AtesStoreError) as exc:
        raise ApprovalError(
            "detached-ledger run namespace changed during the transaction"
        ) from exc
    finally:
        _ACTIVE_AUTHORITY.reset(token)
        try:
            lock.close()
        finally:
            chain.close()


def _assert_active_authority(
    chain: _RunDirectoryChain,
    pin: _PinnedDirectory,
    lock: _WriterLock,
) -> None:
    try:
        chain.assert_authoritative()
        lock.assert_authoritative()
        if chain.run is not pin:
            raise ApprovalError("detached-ledger pin is not the canonical run authority")
    except (OSError, AtesStoreError) as exc:
        raise ApprovalError(
            "detached-ledger run namespace is no longer authoritative"
        ) from exc


def _read_jsonl_held(
    chain: _RunDirectoryChain,
    pin: _PinnedDirectory,
    lock: _WriterLock,
    name: str,
) -> bytes:
    from . import finalization

    _assert_active_authority(chain, pin, lock)
    _repair_trailing_partial_held(pin, lock, name)
    try:
        raw = finalization._pinned_bytes(pin, name, f"detached ledger {name}")
    except finalization.FinalizationError as exc:
        raise ApprovalError(f"cannot read detached ledger {name} safely") from exc
    _assert_active_authority(chain, pin, lock)
    return raw


def _read_jsonl(root: Path, name: str) -> tuple[dict[str, object], ...]:
    authority = _ACTIVE_AUTHORITY.get()
    if authority is not None and name in {"approvals.jsonl", "audit.jsonl"}:
        authority_root, chain, pin, lock = authority
        if Path(root) != authority_root:
            raise ApprovalError(
                "detached-ledger transaction cannot read from another run directory"
            )
        raw = _read_jsonl_held(chain, pin, lock, name)
    else:
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


def _append_line_held(
    pin: _PinnedDirectory,
    lock: _WriterLock,
    name: str,
    line: bytes,
) -> None:
    """Append while the caller retains run-scoped authority across the transaction."""
    handle = None
    try:
        authority = _ACTIVE_AUTHORITY.get()
        if authority is None:
            raise ApprovalError("detached-ledger append lacks transaction authority")
        _root, chain, active_pin, active_lock = authority
        if active_pin is not pin or active_lock is not lock:
            raise ApprovalError("detached-ledger append uses mismatched authority")
        _assert_active_authority(chain, pin, lock)
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
        _assert_active_authority(chain, pin, lock)
        pin.assert_file_identity(name, handle.fileno(), f"detached ledger {name}")
        if os.fstat(handle.fileno()).st_size != before + len(line):
            raise ApprovalError(f"detached ledger {name} changed during append")
        if created:
            pin.fsync()
        _assert_active_authority(chain, pin, lock)
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


def _manifest_identity(run_dir: Path | str):
    from .finalization import FinalizationError, verify_finalized_run

    root = _run_root(run_dir)
    deadline = time.monotonic() + _LOCK_WAIT_SECONDS
    while True:
        identity = _directory_identity(root)
        try:
            result = verify_finalized_run(root)
        except FinalizationError as exc:
            if (
                str(exc) != "cannot acquire authoritative run state for verification"
                or time.monotonic() >= deadline
            ):
                raise
            time.sleep(_LOCK_RETRY_SECONDS)
            continue
        raw = _pinned_bytes(root / "manifests", "manifest-0001.json")
        if _directory_identity(root) != identity:
            raise ApprovalError(
                "ATES run directory changed while its finalization was verified"
            )
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        return root, result, digest, identity


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
    root, result, manifest_digest, _identity = _manifest_identity(run_dir)
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


def _authentication_status(
    record: Mapping[str, object], resolver: Optional[KeyResolver]
) -> tuple[VerificationStatus, Optional[str]]:
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
        return VerificationStatus.UNVERIFIED, "trusted reviewer credential was not supplied"
    try:
        credential = resolver(key_id)
    except BaseException:
        return VerificationStatus.UNVERIFIED, "trusted reviewer credential lookup failed"
    if credential is None:
        return VerificationStatus.UNVERIFIED, "trusted reviewer credential is unavailable"
    if not isinstance(credential, ApprovalCredential):
        return VerificationStatus.UNVERIFIED, "resolver did not bind key material to an actor/role policy"
    if credential.key_id != key_id:
        return VerificationStatus.INVALID, "resolved reviewer credential has another key_id"
    if record.get("actor") != credential.actor:
        return VerificationStatus.INVALID, "approval actor is not authenticated by this credential"
    role = record.get("role")
    if not isinstance(role, str) or role not in credential.roles:
        return VerificationStatus.INVALID, "approval role is not authorized by this credential"
    try:
        expected = _sign_record(record, credential.key)
    except ApprovalError as exc:
        return VerificationStatus.INVALID, str(exc)
    if not hmac.compare_digest(signature, expected):
        return VerificationStatus.INVALID, "approval authentication signature does not verify"
    return VerificationStatus.VERIFIED, None


def _audit_digest(record: Mapping[str, object]) -> str:
    return "sha256:" + hashlib.sha256(_canonical(record, newline=False)).hexdigest()


def _approval_digest(record: Mapping[str, object]) -> str:
    return "sha256:" + hashlib.sha256(
        _canonical(record, newline=False)
    ).hexdigest()


def _normalize_audit_inputs(
    event_type: str,
    actor: str,
    details: Mapping[str, object],
    occurred_at: Optional[datetime],
    dedupe_key: Optional[str],
):
    if not isinstance(event_type, str) or not event_type.strip():
        raise ApprovalError("audit event_type must be a non-empty string")
    if not isinstance(actor, str) or not actor.strip():
        raise ApprovalError("audit actor must be a non-empty string")
    if not isinstance(details, Mapping):
        raise ApprovalError("audit details must be a mapping")
    if dedupe_key is not None and (
        not isinstance(dedupe_key, str) or not dedupe_key.strip()
    ):
        raise ApprovalError("audit dedupe_key must be a non-empty string when supplied")
    when = occurred_at or datetime.now(timezone.utc)
    if when.tzinfo is None:
        raise ApprovalError("audit timestamp must be timezone-aware")
    converted = to_json_compatible(dict(details))
    if not isinstance(converted, dict):
        raise ApprovalError("audit details did not normalize to an object")
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
    previous_digest = None if not records else _audit_digest(records[-1])
    return {
        "ledger_version": AUDIT_LEDGER_VERSION,
        "audit_id": "AUDIT-" + uuid.uuid4().hex,
        "event_type": event_type,
        "actor": actor,
        "occurred_at": occurred_at.isoformat(),
        "previous_record_digest": previous_digest,
        "dedupe_key": dedupe_key,
        "details": dict(details),
    }


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
    immutable_fields = (
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
    )
    try:
        persisted = {key: record[key] for key in immutable_fields}
        requested = {key: template[key] for key in immutable_fields}
    except KeyError:
        return False
    # Python container equality conflates JSON booleans and numbers. Retry
    # identity is instead defined by the canonical immutable request bytes.
    if _canonical(persisted, newline=False) != _canonical(
        requested, newline=False
    ):
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
        expected = _sign_record(record, authentication_key)
    except ApprovalError:
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
        raise ApprovalError("cannot audit an approval without a valid approval_id")
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
    _append_line_held(pin, lock, "audit.jsonl", _canonical(record))
    return record


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
    if not isinstance(stored_details, Mapping):
        return False
    # Python equality conflates JSON booleans and numbers, even inside nested
    # containers. Compare the same canonical representation used by the ledger.
    if _canonical(dict(stored_details), newline=False) != _canonical(
        dict(details), newline=False
    ):
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
    root, result, _manifest_digest, run_identity = _manifest_identity(run_dir)
    ensure_detached_ledgers(root)
    when, normalized_details = _normalize_audit_inputs(
        event_type, actor, details, occurred_at, dedupe_key
    )
    with _ledger_transaction(
        root,
        run_id=result.outcome.run_id,
        expected_identity=run_identity,
    ) as (pin, lock):
        records = _validate_audit_records(_read_jsonl(root, "audit.jsonl"))
        if dedupe_key is not None:
            matches = [record for record in records if record.get("dedupe_key") == dedupe_key]
            if len(matches) > 1:
                raise ApprovalError(
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
                    raise ApprovalError(
                        f"audit dedupe conflict: key {dedupe_key!r} is already bound "
                        "to a different semantic event"
                    )
                return existing
        record = _build_audit_record(
            records,
            event_type=event_type,
            actor=actor,
            details=normalized_details,
            occurred_at=when,
            dedupe_key=dedupe_key,
        )
        _append_line_held(pin, lock, "audit.jsonl", _canonical(record))
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


def _candidate_state(
    candidate: Mapping[str, object],
    template: Mapping[str, object],
    authentication_key: Optional[bytes],
    audits_by_approval: Mapping[str, list[Mapping[str, object]]],
) -> str:
    if not _approval_request_matches(candidate, template, authentication_key):
        return "conflict"
    approval_id = candidate.get("approval_id")
    if not isinstance(approval_id, str):
        return "conflict"
    audit_error = _audit_binding_error(
        candidate, list(audits_by_approval.get(approval_id, ()))
    )
    if audit_error is None:
        return "committed"
    if audit_error.startswith("authenticated approval is pending"):
        return "pending"
    raise ApprovalError(
        f"approval request {candidate.get('request_id')!r} has invalid audit state: "
        f"{audit_error}"
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
        if not isinstance(request_id, str) or not request_id.startswith(_APPROVAL_REQUEST_PREFIX):
            return "approval request_id is invalid"
        suffix = request_id[len(_APPROVAL_REQUEST_PREFIX):]
        if len(suffix) != 64:
            return "approval request_id is invalid"
        try:
            int(suffix, 16)
        except ValueError:
            return "approval request_id is invalid"
    if generation is not None:
        if not isinstance(generation, str) or not _APPROVAL_ID_RE.fullmatch(generation):
            return "approval request generation anchor is invalid"
        if generation not in seen:
            return "approval request generation anchor is not historical"
    return None


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
    if action == ApprovalAction.REVOKE.value and supersedes is None:
        return "approval revocation must identify the record being revoked"

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
    elif (
        isinstance(record.get("evidence_revision"), bool)
        or not isinstance(record.get("evidence_revision"), int)
        or record.get("evidence_revision") != result.outcome.evidence_revision
    ):
        structural_error = "approval evidence revision is stale"
    elif (
        isinstance(record.get("manifest_revision"), bool)
        or not isinstance(record.get("manifest_revision"), int)
        or record.get("manifest_revision") != 1
        or record.get("manifest_digest") != manifest_digest
    ):
        structural_error = "approval manifest binding is stale or invalid"
    elif record.get("action") not in {item.value for item in ApprovalAction}:
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


def _aware_timestamp(value: object, index: int) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ApprovalError(
            f"audit record {index} has invalid occurred_at"
        )
    try:
        parsed = datetime.fromisoformat(value)
        offset = parsed.utcoffset() if parsed.tzinfo is not None else None
    except (TypeError, ValueError, OverflowError) as exc:
        raise ApprovalError(
            f"audit record {index} has invalid occurred_at"
        ) from exc
    if offset is None:
        raise ApprovalError(
            f"audit record {index} occurred_at must be timezone-aware"
        )


def _validate_audit_records(
    records: tuple[dict[str, object], ...],
) -> tuple[dict[str, object], ...]:
    """Validate already-read audit rows while preserving transaction authority."""
    previous = None
    seen: set[str] = set()
    seen_dedupe: set[str] = set()

    for index, record in enumerate(records, 1):
        if not isinstance(record, Mapping):
            raise ApprovalError(f"audit record {index} is malformed")
        fields = set(record)
        if fields != _AUDIT_FIELDS:
            missing = sorted(_AUDIT_FIELDS - fields)
            unexpected = sorted(str(item) for item in fields - _AUDIT_FIELDS)
            details = []
            if missing:
                details.append("missing=" + ",".join(missing))
            if unexpected:
                details.append("unexpected=" + ",".join(unexpected))
            raise ApprovalError(
                f"audit record {index} does not match the canonical field set"
                + (f" ({'; '.join(details)})" if details else "")
            )
        if record.get("ledger_version") != AUDIT_LEDGER_VERSION:
            raise ApprovalError(
                f"audit record {index} has unsupported ledger version"
            )

        audit_id = record.get("audit_id")
        if (
            not isinstance(audit_id, str)
            or not _AUDIT_ID_RE.fullmatch(audit_id)
            or audit_id in seen
        ):
            raise ApprovalError(
                f"audit record {index} has invalid/duplicate audit_id"
            )

        for field in ("event_type", "actor"):
            value = record.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ApprovalError(
                    f"audit record {index} has invalid {field}"
                )

        _aware_timestamp(record.get("occurred_at"), index)

        if "details" not in record or not isinstance(record.get("details"), Mapping):
            raise ApprovalError(
                f"audit record {index} has invalid details"
            )

        if "dedupe_key" not in record:
            raise ApprovalError(
                f"audit record {index} is missing dedupe_key"
            )
        dedupe_key = record.get("dedupe_key")
        if dedupe_key is not None and (
            not isinstance(dedupe_key, str) or not dedupe_key.strip()
        ):
            raise ApprovalError(
                f"audit record {index} has invalid dedupe_key"
            )

        if "previous_record_digest" not in record:
            raise ApprovalError(
                f"audit record {index} is missing previous_record_digest"
            )
        if record.get("previous_record_digest") != previous:
            raise ApprovalError(
                f"audit record {index} breaks the append hash chain"
            )

        if dedupe_key is not None:
            if dedupe_key in seen_dedupe:
                raise ApprovalError(f"audit record {index} has duplicate dedupe_key")
            seen_dedupe.add(dedupe_key)
        seen.add(audit_id)
        previous = _audit_digest(record)

    return records


def validate_audit_chain(run_dir):
    """Validate both the hash chain and the complete canonical audit-row shape."""
    root = _run_root(run_dir)
    return _validate_audit_records(_read_jsonl(root, "audit.jsonl"))


def validate_approvals(run_dir: Path | str, *, key_resolver=None):
    """Validate approval structure, timestamp, authentication, and audit bind."""
    root, result, manifest_digest, _identity = _manifest_identity(run_dir)
    raw_records = _read_jsonl(root, "approvals.jsonl")
    audit_records = tuple(validate_audit_chain(root))
    audit_by_approval = _audit_records_by_approval(audit_records)
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
            else _authentication_status(record, key_resolver)
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
            if record.get("action") != ApprovalAction.REVOKE.value:
                authoritative[approval_id] = True

        validations.append(
            ApprovalValidation(record, status, False, reason_text)
        )

    final = tuple(
        ApprovalValidation(
            item.record,
            item.verification_status,
            item.record.get("approval_id") in authoritative,
            item.reason,
        )
        for item in validations
    )
    return ApprovalLedgerResult(final, tuple(authoritative))


def _approval_matches_live_request(
    record: Mapping[str, object],
    template: Mapping[str, object],
    authentication_key: Optional[bytes],
    *,
    explicit_occurred_at: bool,
) -> bool:
    """Match retry semantics, including a caller-specified timestamp when present."""
    if not _approval_request_matches(record, template, authentication_key):
        return False
    if explicit_occurred_at and record.get("occurred_at") != template.get("occurred_at"):
        return False
    return True


def _generation_key_resolver(
    *,
    actor: str,
    role: str,
    key_id: Optional[str],
    authentication_key: Optional[bytes],
    key_resolver,
):
    """Build the independent credential boundary used for historical superseders.

    The current caller can authenticate historical records issued by the same
    credential.  Callers that need cross-reviewer supersession semantics may
    additionally supply the same independently trusted resolver used by
    ``validate_approvals``.  A ledger record's self-declared ``verified`` state
    is never sufficient to advance a request generation.
    """
    current_credential = None
    if authentication_key is not None and isinstance(key_id, str) and key_id.strip():
        current_credential = ApprovalCredential(
            key_id=key_id,
            key=bytes(authentication_key),
            actor=actor,
            roles=(role,),
        )

    def resolve(candidate_key_id: str):
        if current_credential is not None and candidate_key_id == current_credential.key_id:
            return current_credential
        if key_resolver is None:
            return None
        return key_resolver(candidate_key_id)

    return resolve


def _is_committed_verified_superseder(
    record: Mapping[str, object],
    *,
    target_approval_id: str,
    audits_by_approval: Mapping[str, list[Mapping[str, object]]],
    key_resolver,
    result,
    manifest_digest: str,
    seen: dict[str, Mapping[str, object]],
) -> bool:
    """Return true only for an authenticated, authorized, audited superseder."""

    if _approval_structural_error(
        record,
        result=result,
        manifest_digest=manifest_digest,
        seen=seen,
    ) is not None:
        return False
    if record.get("supersedes_approval_id") != target_approval_id:
        return False
    status, _reason = _authentication_status(record, key_resolver)
    if status is not VerificationStatus.VERIFIED:
        return False
    approval_id = record.get("approval_id")
    if not isinstance(approval_id, str):
        return False
    return _audit_binding_error(
        record, list(audits_by_approval.get(approval_id, ()))
    ) is None


def _later_generation_terminator(
    approvals: tuple[dict[str, object], ...],
    candidate_index: int,
    candidate: Mapping[str, object],
    audits_by_approval: Mapping[str, list[Mapping[str, object]]],
    *,
    key_resolver,
    result,
    manifest_digest: str,
) -> Optional[Mapping[str, object]]:
    approval_id = candidate.get("approval_id")
    if not isinstance(approval_id, str):
        return None
    latest: Optional[Mapping[str, object]] = None
    # Reconstruct the same historical structural view used by
    # validate_approvals(). Invalid rows never gain authority merely because
    # their authentication and audit digest happen to be valid.

    seen: dict[str, Mapping[str, object]] = {}
    for record in approvals[: candidate_index + 1]:
        _approval_structural_error(
            record,
            result=result,
            manifest_digest=manifest_digest,
            seen=seen,
        )
    for record in approvals[candidate_index + 1 :]:
        if _is_committed_verified_superseder(
            record,
            target_approval_id=approval_id,
            audits_by_approval=audits_by_approval,
            key_resolver=key_resolver,
            result=result,
            manifest_digest=manifest_digest,
            seen=seen,
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
    payload = _request_identity_payload(
        template, explicit_occurred_at=explicit_occurred_at
    )
    payload["protocol"] = "ates-approval-request-v2"
    payload[_REQUEST_GENERATION_FIELD] = generation_after_approval_id
    digest = hashlib.sha256(_canonical(payload, newline=False)).hexdigest()
    request_id = _APPROVAL_REQUEST_PREFIX + digest

    template["request_id"] = request_id
    template[_REQUEST_GENERATION_FIELD] = generation_after_approval_id
    auth = template.get("authentication")
    if authentication_key is not None:
        if not isinstance(auth, dict):
            raise ApprovalError("authenticated approval metadata is malformed")
        # Both generation identity and request_id are authenticated immutable bytes.
        auth["signature"] = None
        auth["signature"] = _sign_record(template, authentication_key)
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
    key_resolver=None,
) -> Mapping[str, object]:
    """Append/recover one approval operation without conflating later generations.

    A matching live request is treated as a retry and converges to the same
    durable approval.  Once a later authenticated+authorized+audited operation
    supersedes that approval, the same semantic call starts a new request
    generation whose identity is anchored to the superseding record.

    ``key_resolver`` is optional for the common same-reviewer case because the
    current caller's key/actor/role form an independent credential.  Supplying a
    resolver enables generation changes caused by a different trusted reviewer.
    """
    if action is None:
        action = ApprovalAction.APPROVE
    root, result, manifest_digest, run_identity = _manifest_identity(run_dir)
    ensure_detached_ledgers(root)
    template = _new_approval_record(
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
    expected_binding = {
        "run_id": str(result.outcome.run_id),
        "finalization_id": str(result.outcome.finalization_id),
        "evidence_revision": result.outcome.evidence_revision,
        "manifest_revision": 1,
        "manifest_digest": manifest_digest,
    }
    if _canonical(
        {key: template.get(key) for key in expected_binding}, newline=False
    ) != _canonical(expected_binding, newline=False):
        raise ApprovalError(
            "ATES finalization changed while the approval request was prepared"
        )
    explicit_occurred_at = occurred_at is not None
    generation_resolver = _generation_key_resolver(
        actor=actor,
        role=role,
        key_id=key_id,
        authentication_key=authentication_key,
        key_resolver=key_resolver,
    )

    with _ledger_transaction(
        root,
        run_id=result.outcome.run_id,
        expected_identity=run_identity,
    ) as (pin, lock):
        approvals = _read_jsonl(root, "approvals.jsonl")
        audits = _validate_audit_records(_read_jsonl(root, "audit.jsonl"))
        audits_by_approval = _audit_records_by_approval(audits)

        # Search newest-first. A semantic match is a retry only while that
        # approval generation has not subsequently been terminated by a
        # cryptographically verified + policy-authorized + audited operation.
        generation_anchor: Optional[str] = None
        for index in range(len(approvals) - 1, -1, -1):
            candidate = approvals[index]
            if not _approval_matches_live_request(
                candidate,
                template,
                authentication_key,
                explicit_occurred_at=explicit_occurred_at,
            ):
                continue
            state = _candidate_state(
                candidate, template, authentication_key, audits_by_approval
            )
            if state == "conflict":
                continue

            terminator = _later_generation_terminator(
                approvals,
                index,
                candidate,
                audits_by_approval,
                key_resolver=generation_resolver,
                result=result,
                manifest_digest=manifest_digest,
            )
            if terminator is None:
                if state == "pending":
                    _append_approval_audit(root, pin, lock, candidate, audits)
                return candidate

            terminator_id = terminator.get("approval_id")
            if isinstance(terminator_id, str) and generation_anchor is None:
                generation_anchor = terminator_id

        # No live matching request exists, so this is a new intentional approval
        # generation. The anchor is authenticated durable history and therefore
        # remains stable across retries of this new generation.
        request_id = _bind_generation_request_identity(
            template,
            authentication_key=authentication_key,
            explicit_occurred_at=explicit_occurred_at,
            generation_after_approval_id=generation_anchor,
        )
        exact = [item for item in approvals if item.get("request_id") == request_id]
        if len(exact) > 1:
            raise ApprovalError(
                f"approval request identity {request_id!r} is duplicated"
            )
        if exact:
            candidate = exact[0]
            if not _approval_matches_live_request(
                candidate,
                template,
                authentication_key,
                explicit_occurred_at=explicit_occurred_at,
            ):
                raise ApprovalError(
                    f"approval request identity {request_id!r} is bound to a different explicit timestamp"
                )
            state = _candidate_state(
                candidate, template, authentication_key, audits_by_approval
            )
            if state == "conflict":
                raise ApprovalError(
                    f"approval request identity {request_id!r} is bound to different immutable approval bytes"
                )
            if state == "pending":
                _append_approval_audit(root, pin, lock, candidate, audits)
            return candidate

        if supersedes_approval_id is not None and not any(
            item.get("approval_id") == supersedes_approval_id for item in approvals
        ):
            raise ApprovalError("superseded approval does not exist in this ledger")

        _append_line_held(
            pin, lock, "approvals.jsonl", _canonical(template)
        )
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
    key_resolver=None,
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
        key_resolver=key_resolver,
    )


def record_finalization_audit(run_dir: Path | str) -> Mapping[str, object]:
    root, result, manifest_digest, _identity = _manifest_identity(run_dir)
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


__all__ = [
    "APPROVAL_AUTH_METHOD",
    "APPROVAL_LEDGER_VERSION",
    "AUDIT_LEDGER_VERSION",
    "ApprovalAction",
    "ApprovalCredential",
    "ApprovalError",
    "ApprovalLedgerResult",
    "ApprovalValidation",
    "KeyResolver",
    "append_approval",
    "append_audit_event",
    "ensure_detached_ledgers",
    "record_finalization_audit",
    "revoke_approval",
    "validate_approvals",
    "validate_audit_chain",
]
