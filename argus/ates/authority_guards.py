"""Authority-bearing identifier and retry-transition guards for ATES.

This module centralizes the small set of validation rules that must agree across
canonical evidence retries and detached approval authority.  It deliberately
patches internal validation hooks only; public ATES call signatures remain
unchanged.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from types import SimpleNamespace

from .core import EventType, StepAttemptStatus, VerificationStatus
from .finalization_types import FinalizationError
from .ids import RunId


_MACHINE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@:/+#~-]{0,127}$")
_RETRYABLE_PREDECESSORS = frozenset(
    {StepAttemptStatus.FAILED, StepAttemptStatus.ERROR}
)


def _validate_machine_identifier(value: object, label: str) -> None:
    if not isinstance(value, str) or not _MACHINE_IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} must be a bounded machine-safe identifier")


def _validate_retry_predecessors(events, ev) -> None:
    """Allow retries only after terminal states Argus actually treats as retryable."""
    completed: dict[str, object] = {}
    for event in tuple(events):
        kind = event.envelope.event_type
        if kind is EventType.STEP_ATTEMPT_COMPLETED:
            rec = ev._attempt(event.payload.get("attempt"), running=False)
            completed[str(rec.step_attempt_id)] = rec
            continue
        if kind is not EventType.STEP_RETRY_SCHEDULED:
            continue
        previous = event.payload.get("previous_step_attempt_id")
        prior = completed.get(previous) if isinstance(previous, str) else None
        if prior is None:
            continue
        if prior.status not in _RETRYABLE_PREDECESSORS:
            raise FinalizationError(
                f"step attempt status {prior.status.value} is not retryable by an ordinary retry"
            )


def _candidate_current_package_error(candidate, template, audit):
    """Run candidate reuse through the same structural/current-package contract."""
    run_id = template.get("run_id")
    manifest_digest = template.get("manifest_digest")
    evidence_revision = template.get("evidence_revision")
    finalization_id = template.get("finalization_id")
    try:
        rid = RunId(run_id)
    except (TypeError, ValueError):
        return "approval candidate is bound to an invalid run"
    if not isinstance(manifest_digest, str):
        return "approval candidate manifest binding is invalid"

    # Candidate-state matching does not receive the historical ledger view.  Seed
    # only the candidate's explicit relationship anchors so the shared validator
    # can check their shape while the surrounding append protocol remains
    # responsible for proving which generation is actually historical.
    seen: dict[str, Mapping[str, object]] = {}
    approval_id = candidate.get("approval_id") if isinstance(candidate, Mapping) else None
    if isinstance(candidate, Mapping):
        for field in (
            "supersedes_approval_id",
            "request_generation_after_approval_id",
        ):
            ref = candidate.get(field)
            if isinstance(ref, str) and ref and ref != approval_id:
                seen[ref] = {"approval_id": ref}

    result = SimpleNamespace(
        outcome=SimpleNamespace(
            run_id=rid,
            finalization_id=finalization_id,
            evidence_revision=evidence_revision,
        )
    )
    return audit._approval_structural_error(
        candidate,
        result=result,
        manifest_digest=manifest_digest,
        seen=seen,
    )


def install() -> None:
    """Install idempotent authority guards after the base trust guards."""
    from . import audit as audit
    from . import evidence_validation as ev
    from . import trust_guards as trust_guards

    if getattr(audit, "_ates_authority_guards_installed", False):
        return

    base_retry_semantics = trust_guards._validate_retry_semantics
    base_incomplete_prefix = trust_guards._validate_incomplete_prefix
    base_credential_post_init = audit.ApprovalCredential.__post_init__
    base_new_approval_record = audit._new_approval_record
    base_approval_structural_error = audit._approval_structural_error
    base_authentication_status = audit._authentication_status
    base_candidate_state = audit._candidate_state

    def guarded_retry_semantics(events, validator):
        base_retry_semantics(events, validator)
        _validate_retry_predecessors(events, validator)

    def guarded_incomplete_prefix(events, run_id, validator):
        base_incomplete_prefix(events, run_id, validator)
        _validate_retry_predecessors(events, validator)

    def guarded_credential_post_init(self):
        base_credential_post_init(self)
        try:
            trust_guards._validate_audit_actor(self.actor)
            _validate_machine_identifier(self.key_id, "approval credential key_id")
            for role in self.roles:
                _validate_machine_identifier(role, "approval credential role")
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

    def guarded_new_approval_record(*args, **kwargs):
        try:
            _validate_machine_identifier(kwargs.get("role"), "approval role")
            key_id = kwargs.get("key_id")
            if key_id is not None:
                _validate_machine_identifier(key_id, "approval key_id")
        except ValueError as exc:
            raise audit.ApprovalError(str(exc)) from exc
        return base_new_approval_record(*args, **kwargs)

    def guarded_approval_structural_error(record, *args, **kwargs):
        if isinstance(record, Mapping):
            try:
                _validate_machine_identifier(record.get("role"), "approval role")
                auth = record.get("authentication")
                if isinstance(auth, Mapping):
                    key_id = auth.get("key_id")
                    if key_id is not None:
                        _validate_machine_identifier(key_id, "approval key_id")
            except ValueError as exc:
                return str(exc)
        return base_approval_structural_error(record, *args, **kwargs)

    def guarded_authentication_status(record, resolver):
        if isinstance(record, Mapping):
            try:
                _validate_machine_identifier(record.get("role"), "approval role")
                auth = record.get("authentication")
                if isinstance(auth, Mapping):
                    key_id = auth.get("key_id")
                    if key_id is not None:
                        _validate_machine_identifier(key_id, "approval key_id")
            except ValueError as exc:
                return VerificationStatus.INVALID, str(exc)
        return base_authentication_status(record, resolver)

    def guarded_candidate_state(candidate, template, authentication_key, audits_by_approval):
        if not audit._approval_request_matches(candidate, template, authentication_key):
            return "conflict"
        structural_error = _candidate_current_package_error(candidate, template, audit)
        if structural_error is not None:
            return "conflict"
        return base_candidate_state(
            candidate, template, authentication_key, audits_by_approval
        )

    trust_guards._validate_retry_semantics = guarded_retry_semantics
    trust_guards._validate_incomplete_prefix = guarded_incomplete_prefix
    audit.ApprovalCredential.__post_init__ = guarded_credential_post_init
    audit._new_approval_record = guarded_new_approval_record
    audit._approval_structural_error = guarded_approval_structural_error
    audit._authentication_status = guarded_authentication_status
    audit._candidate_state = guarded_candidate_state
    audit._ates_authority_guards_installed = True


__all__ = ["install"]
