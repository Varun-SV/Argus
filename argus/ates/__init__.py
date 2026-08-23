"""Argus Test Evidence Specification (ATES) core schema and event storage."""

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
