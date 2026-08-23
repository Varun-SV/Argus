"""Argus Test Evidence Specification (ATES) core schema and event storage."""

from dataclasses import replace as _dc_replace

from .artifacts import (
    ARTIFACT_BYTES_PROFILE, ARTIFACT_POLICY_VERSION, ARTIFACT_SUPPRESSION_REASONS,
    PROTECTED_ARTIFACT_COMMITMENT_PROFILE, PROTECTED_ARTIFACT_VERIFICATION_REF,
    ArtifactCaptureConfig, ArtifactCaptureError, ArtifactCapturePolicy,
    ArtifactCaptureResult, ArtifactContext, ArtifactReservation, ArtifactSanitizer,
    ArtifactSuppression, AtesArtifactRepository,
)
from .core import (
    ATES_VERSION, RUN_OUTCOME_REFINALIZATION_SCOPE, STATUS_POLICY_VERSION,
    SUPPORTED_STATUS_POLICY_VERSIONS, ActionRecord, ArtifactRecord, AssertionRecord,
    AssertionResult, AuthorizationDecision, EventEnvelope, EventType, EvidenceDisposition,
    EvidenceValue, ExecutionKind, FindingRecord, FrozenDict, LifecycleState,
    ObservationRecord, RequirementIdentity, RoamSource, RunOutcomeRevision, RunRecord,
    RunStatus, ScriptedSource, SourceCommitment, StepAttemptRecord, StepAttemptStatus,
    StepRecord, Verification, VerificationStatus, to_json_compatible,
    validate_artifact_path, validate_step_attempt_history,
    validate_step_evidence_relationships,
)
from .finalization import (
    EVIDENCE_DIGEST_PROFILE, FINALIZATION_BINDING_VERSION, MANIFEST_VERSION,
    PACKAGE_MANIFEST_VERSION, FinalizationError, FinalizationResult,
    FinalizationTrustState, finalize_revision_one, recover_revision_one,
    verify_finalized_run,
)

# The hardened finalization layer validates the same immutable attempt stream as
# the implementation helper, but older call sites pass only the event snapshot.
# Keep one strict implementation and infer the already-bound RunId only for that
# compatibility form. These adapters can be removed when the hardening layer is
# folded back into finalization_impl.py.
from . import finalization_impl as _finalization_impl

_strict_validate_attempts = _finalization_impl._validate_attempts


def _validate_attempts_compat(events, run_id=None):
    snapshot = tuple(events)
    if run_id is None:
        if not snapshot:
            raise FinalizationError("cannot validate an empty ATES attempt stream")
        run_id = snapshot[0].run_id
    return _strict_validate_attempts(snapshot, run_id)


_finalization_impl._validate_attempts = _validate_attempts_compat

# A failed run may deliberately preserve the target after a Failure Capsule
# retention failure. In that shape the immutable evidence already contains a
# deterministic failure plus the provisional execution_result="fail" handoff,
# but there is intentionally no TARGET_CLOSED. Missing close must still prevent
# a successful outcome, while it must not upgrade that already-known failure to
# an execution error and erase the caller-visible test result.
_hardened_derive = _finalization_impl._derive


def _derive_preserving_known_failure(events, run_id):
    snapshot = tuple(events)
    state = _hardened_derive(snapshot, run_id)
    inputs = state.status_inputs
    if not (inputs.execution_error and inputs.deterministic_failure):
        return state

    has_launch = any(
        event.envelope.event_type is EventType.TARGET_LAUNCHED
        for event in snapshot
    )
    has_close = any(
        event.envelope.event_type is EventType.TARGET_CLOSED
        for event in snapshot
    )
    has_release = any(
        event.envelope.event_type is EventType.ENVIRONMENT_RELEASED
        for event in snapshot
    )
    provisional_fail = any(
        event.envelope.event_type is EventType.RUN_MARKED_INCOMPLETE
        and event.payload.get("reason") == "runtime.finalization_pending"
        and str(event.payload.get("execution_result") or "").strip().lower()
        in {"fail", "failed"}
        for event in snapshot
    )
    nonprovisional_incomplete = any(
        event.envelope.event_type is EventType.RUN_MARKED_INCOMPLETE
        and event.payload.get("reason") != "runtime.finalization_pending"
        for event in snapshot
    )
    unresolved_action = any(
        event.envelope.event_type is EventType.ACTION_OUTCOME_UNKNOWN
        for event in snapshot
    )

    if (
        has_launch
        and not has_close
        and has_release
        and provisional_fail
        and not nonprovisional_incomplete
        and not unresolved_action
    ):
        return _dc_replace(
            state,
            status_inputs=_dc_replace(inputs, execution_error=False),
        )
    return state


_finalization_impl._derive = _derive_preserving_known_failure

# Install the next hardening layer only after the compatibility hooks above are
# in place. It validates canonical RunRecord provenance, prevents action
# terminals after target close, preflights crash-recovery bytes before mutation,
# and restores genuine execution errors that the preserved-target exception must
# never erase.
from .finalization_round3 import install as _install_finalization_round3

_install_finalization_round3(_finalization_impl)

# Keep recovery preflight aligned with the event store's canonical RunId
# namespace encoding and its narrowly scoped trailing-partial repair contract.
# The round-3 recovery wrapper resolves this helper dynamically, so replacing
# only that helper keeps all other hardening behavior on one path.
from .finalization_round4 import install as _install_finalization_round4

_install_finalization_round4()
recover_revision_one = _finalization_impl.recover_revision_one

from .ids import (
    ActionId, ActionOperationId, ArtifactId, AssertionId, AtesId, CorrectionId,
    EventId, FinalizationId, FindingId, ObservationId, RunId, StepAttemptId, StepId,
)
from .privacy import (
    PRIVACY_POLICY_VERSION, EvidenceContext, EvidencePrivacyConfig,
    EvidencePrivacyPolicy, PrivacyPolicyError, ProtectedEvidenceSink,
)
from .status import StatusInputs, derive_run_status, effective_outcome
from .store import (
    AtesAppendError, AtesEventConflict, AtesEventStore, AtesStoreBusy,
    AtesStoreCorruption, AtesStoreError, StoredEvent,
)

__all__ = [
    "ARTIFACT_BYTES_PROFILE", "ARTIFACT_POLICY_VERSION", "ARTIFACT_SUPPRESSION_REASONS",
    "ATES_VERSION", "EVIDENCE_DIGEST_PROFILE", "FINALIZATION_BINDING_VERSION",
    "MANIFEST_VERSION", "PACKAGE_MANIFEST_VERSION", "PRIVACY_POLICY_VERSION",
    "PROTECTED_ARTIFACT_COMMITMENT_PROFILE", "PROTECTED_ARTIFACT_VERIFICATION_REF",
    "RUN_OUTCOME_REFINALIZATION_SCOPE", "STATUS_POLICY_VERSION",
    "SUPPORTED_STATUS_POLICY_VERSIONS", "ActionId", "ActionOperationId", "ActionRecord",
    "ArtifactCaptureConfig", "ArtifactCaptureError", "ArtifactCapturePolicy",
    "ArtifactCaptureResult", "ArtifactContext", "ArtifactId", "ArtifactRecord",
    "ArtifactReservation", "ArtifactSanitizer", "ArtifactSuppression", "AssertionId",
    "AssertionRecord", "AssertionResult", "AtesAppendError", "AtesArtifactRepository",
    "AtesEventConflict", "AtesEventStore", "AtesId", "AtesStoreBusy",
    "AtesStoreCorruption", "AtesStoreError", "AuthorizationDecision", "CorrectionId",
    "EventEnvelope", "EventId", "EventType", "EvidenceContext", "EvidenceDisposition",
    "EvidencePrivacyConfig", "EvidencePrivacyPolicy", "EvidenceValue", "ExecutionKind",
    "FinalizationError", "FinalizationId", "FinalizationResult", "FinalizationTrustState",
    "FindingId", "FindingRecord", "FrozenDict", "LifecycleState", "ObservationId",
    "ObservationRecord", "PrivacyPolicyError", "ProtectedEvidenceSink",
    "RequirementIdentity", "RoamSource", "RunId", "RunOutcomeRevision", "RunRecord",
    "RunStatus", "ScriptedSource", "SourceCommitment", "StatusInputs", "StepAttemptId",
    "StepAttemptRecord", "StepAttemptStatus", "StepId", "StepRecord", "StoredEvent",
    "Verification", "VerificationStatus", "derive_run_status", "effective_outcome",
    "finalize_revision_one", "recover_revision_one", "to_json_compatible",
    "validate_artifact_path", "validate_step_attempt_history",
    "validate_step_evidence_relationships", "verify_finalized_run",
]
