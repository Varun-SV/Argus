"""Public detached ATES approval/audit API with policy-bound verification."""
from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Callable, Mapping, Optional

from . import audit_impl as _impl
from .core import VerificationStatus

APPROVAL_AUTH_METHOD = _impl.APPROVAL_AUTH_METHOD
APPROVAL_LEDGER_VERSION = _impl.APPROVAL_LEDGER_VERSION
AUDIT_LEDGER_VERSION = _impl.AUDIT_LEDGER_VERSION
ApprovalAction = _impl.ApprovalAction
ApprovalError = _impl.ApprovalError
ApprovalLedgerResult = _impl.ApprovalLedgerResult
ApprovalValidation = _impl.ApprovalValidation
append_approval = _impl.append_approval
append_audit_event = _impl.append_audit_event
ensure_detached_ledgers = _impl.ensure_detached_ledgers
record_finalization_audit = _impl.record_finalization_audit
revoke_approval = _impl.revoke_approval


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
        expected = _impl._sign_record(record, credential.key)
    except ApprovalError as exc:
        return VerificationStatus.INVALID, str(exc)
    if not hmac.compare_digest(signature, expected):
        return VerificationStatus.INVALID, "approval authentication signature does not verify"
    return VerificationStatus.VERIFIED, None


def validate_approvals(
    run_dir,
    *,
    key_resolver: Optional[KeyResolver] = None,
) -> ApprovalLedgerResult:
    """Validate approvals without creating/changing detached ledger files."""
    root, result, manifest_digest = _impl._manifest_identity(run_dir)
    raw_records = _impl._read_jsonl(root, "approvals.jsonl")
    seen: dict[str, Mapping[str, object]] = {}
    authoritative: dict[str, bool] = {}
    validations: list[ApprovalValidation] = []

    for record in raw_records:
        approval_id = record.get("approval_id")
        structural_error: Optional[str] = None
        if record.get("ledger_version") != APPROVAL_LEDGER_VERSION:
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
        elif record.get("action") not in {item.value for item in ApprovalAction}:
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

        status, reason = (
            (VerificationStatus.INVALID, structural_error)
            if structural_error is not None
            else _authentication_status(record, key_resolver)
        )
        if status is VerificationStatus.VERIFIED and isinstance(approval_id, str):
            supersedes = record.get("supersedes_approval_id")
            if isinstance(supersedes, str):
                authoritative.pop(supersedes, None)
            if record.get("action") != ApprovalAction.REVOKE.value:
                authoritative[approval_id] = True
        validations.append(ApprovalValidation(record, status, False, reason))

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


def validate_audit_chain(run_dir):
    """Validate the append hash chain without creating an absent audit ledger."""
    root = _impl._run_root(run_dir)
    records = _impl._read_jsonl(root, "audit.jsonl")
    previous = None
    seen: set[str] = set()
    for index, record in enumerate(records, 1):
        audit_id = record.get("audit_id")
        if record.get("ledger_version") != AUDIT_LEDGER_VERSION:
            raise ApprovalError(f"audit record {index} has unsupported ledger version")
        if not isinstance(audit_id, str) or not _impl._AUDIT_ID_RE.fullmatch(audit_id) or audit_id in seen:
            raise ApprovalError(f"audit record {index} has invalid/duplicate audit_id")
        if record.get("previous_record_digest") != previous:
            raise ApprovalError(f"audit record {index} breaks the append hash chain")
        seen.add(audit_id)
        previous = _impl._audit_digest(record)
    return records


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
