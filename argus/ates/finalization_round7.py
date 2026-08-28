"""Round-7 lifecycle and retained-artifact hardening for PR #22.

This layer closes two trust gaps without weakening the earlier compatibility
layers:

* TARGET_CLOSED may not occur while a step attempt is active; and a retained
  Failure Capsule may excuse only the intentionally missing close contribution,
  never an independently incomplete action lifecycle.
* every retained artifact is reconstructed as the full ArtifactRecord contract
  before its bytes are accepted, and the manifest preserves that normalized
  policy metadata instead of projecting only digest/path fields.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from .artifacts import (
    ARTIFACT_BYTES_PROFILE,
    PROTECTED_ARTIFACT_COMMITMENT_PROFILE,
    PROTECTED_ARTIFACT_VERIFICATION_REF,
)
from .core import (
    ArtifactRecord,
    EventType,
    EvidenceDisposition,
    SourceCommitment,
    to_json_compatible,
)


def _commitment(value: object, impl) -> SourceCommitment:
    if not isinstance(value, Mapping):
        raise impl.FinalizationError("artifact content commitment is malformed")
    try:
        return SourceCommitment(
            method=value["method"],
            value=value["value"],
            canonicalization_profile=value.get("canonicalization_profile"),
            verification_ref=value.get("verification_ref"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise impl.FinalizationError("artifact content commitment is invalid") from exc


def _artifact_record(value: object, impl) -> ArtifactRecord:
    if not isinstance(value, Mapping):
        raise impl.FinalizationError("retained artifact metadata is malformed")
    try:
        record = ArtifactRecord(
            artifact_id=value["artifact_id"],
            kind=value["kind"],
            path=value["path"],
            sensitivity=value["sensitivity"],
            capture_policy=value["capture_policy"],
            content_digest=_commitment(value.get("content_digest"), impl),
            size_bytes=value["size_bytes"],
            protection_state=value["protection_state"],
            protected_ref=value.get("protected_ref"),
            access_policy=value.get("access_policy"),
            retention_policy=value.get("retention_policy"),
            authorization_ref=value.get("authorization_ref"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, impl.FinalizationError):
            raise
        raise impl.FinalizationError("retained artifact metadata is invalid") from exc

    commitment = record.content_digest
    if record.protection_state is EvidenceDisposition.PROTECTED_REF:
        if (
            commitment.method != "hmac-sha256"
            or commitment.canonicalization_profile
            != PROTECTED_ARTIFACT_COMMITMENT_PROFILE
            or commitment.verification_ref
            != PROTECTED_ARTIFACT_VERIFICATION_REF
        ):
            raise impl.FinalizationError(
                "protected retained artifact requires the protected HMAC commitment profile"
            )
    elif (
        commitment.method != "sha256"
        or commitment.canonicalization_profile != ARTIFACT_BYTES_PROFILE
        or commitment.verification_ref is not None
    ):
        raise impl.FinalizationError(
            "ordinary retained artifact requires the canonical artifact SHA-256 profile"
        )
    return record


def _active_close_and_independent_lifecycle(events, impl) -> bool:
    """Validate close timing and detect lifecycle errors independent of retention."""
    active_attempt: str | None = None
    action_states: dict[str, str] = {}

    for event in tuple(events):
        kind = event.envelope.event_type
        if kind is EventType.STEP_ATTEMPT_STARTED:
            attempt = event.payload.get("attempt")
            if isinstance(attempt, Mapping):
                attempt_id = attempt.get("step_attempt_id")
                if isinstance(attempt_id, str):
                    active_attempt = attempt_id
        elif kind is EventType.STEP_ATTEMPT_COMPLETED:
            attempt = event.payload.get("attempt")
            if isinstance(attempt, Mapping):
                attempt_id = attempt.get("step_attempt_id")
                if isinstance(attempt_id, str) and active_attempt == attempt_id:
                    active_attempt = None
        elif kind is EventType.TARGET_CLOSED:
            if active_attempt is not None:
                raise impl.FinalizationError(
                    "TARGET_CLOSED cannot occur while a step attempt is active"
                )
        elif kind in {
            EventType.ACTION_PROPOSED,
            EventType.ACTION_POLICY_VALIDATED,
            EventType.ACTION_DISPATCH_COMMITTED,
        }:
            action = event.payload.get("action")
            if not isinstance(action, Mapping):
                continue
            action_id = action.get("action_id")
            if not isinstance(action_id, str):
                continue
            if kind is EventType.ACTION_PROPOSED:
                action_states[action_id] = "proposed"
            elif kind is EventType.ACTION_POLICY_VALIDATED:
                action_states[action_id] = "validated"
            else:
                action_states[action_id] = "committed"
        elif kind in {EventType.ACTION_EXECUTED, EventType.ACTION_OUTCOME_UNKNOWN}:
            action_id = event.payload.get("action_id")
            if isinstance(action_id, str):
                action_states[action_id] = "terminal"

    # The existing relationship validator deliberately treats a policy-validated
    # action with no durable dispatch decision as an execution lifecycle error.
    # Failure-Capsule retention may excuse a missing TARGET_CLOSED only; it must
    # not erase this independent contribution.
    return any(state == "validated" for state in action_states.values())


def install(impl) -> None:
    previous_derive = impl._derive
    previous_artifacts = impl._artifacts

    def derive(events, run_id):
        snapshot = tuple(events)
        independent_lifecycle_error = _active_close_and_independent_lifecycle(
            snapshot, impl
        )
        state = previous_derive(snapshot, run_id)
        if independent_lifecycle_error and not state.status_inputs.execution_error:
            state = replace(
                state,
                status_inputs=replace(
                    state.status_inputs,
                    execution_error=True,
                ),
            )
        return state

    def artifacts(store, records):
        normalized = tuple(_artifact_record(record, impl) for record in tuple(records))
        # Preserve all of the previous pinned/no-follow byte verification before
        # replacing the lossy manifest projection with the complete schema.
        previous_artifacts(store, records)
        return [to_json_compatible(record) for record in normalized]

    impl._derive = derive
    impl._artifacts = artifacts


__all__ = ["install"]
