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

from .audit import (
    APPROVAL_AUTH_METHOD, APPROVAL_LEDGER_VERSION, AUDIT_LEDGER_VERSION,
    ApprovalAction, ApprovalCredential, ApprovalError, ApprovalLedgerResult,
    ApprovalValidation, KeyResolver, append_approval, append_audit_event,
    ensure_detached_ledgers, record_finalization_audit, revoke_approval,
    validate_approvals, validate_audit_chain,
)
from .reports import (
    REPORT_MANIFEST_VERSION, REPORT_RENDERER_ID, REPORT_VERSION,
    FinalizationTrustInspection, ReportBundle, ReportError,
    ReportVerificationResult, inspect_finalization_trust, inspect_report_bundle,
    render_reports, verify_report_bundle,
)
from .package import (
    PACKAGE_COMPLETION_VERSION, CompletedRunPackage, PackageCompletionError,
    complete_run_package,
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
    "APPROVAL_AUTH_METHOD", "APPROVAL_LEDGER_VERSION", "ARTIFACT_BYTES_PROFILE",
    "ARTIFACT_POLICY_VERSION", "ARTIFACT_SUPPRESSION_REASONS", "ATES_VERSION",
    "AUDIT_LEDGER_VERSION", "EVIDENCE_DIGEST_PROFILE", "FINALIZATION_BINDING_VERSION",
    "MANIFEST_VERSION", "PACKAGE_COMPLETION_VERSION", "PACKAGE_MANIFEST_VERSION",
    "PRIVACY_POLICY_VERSION", "PROTECTED_ARTIFACT_COMMITMENT_PROFILE",
    "PROTECTED_ARTIFACT_VERIFICATION_REF", "REPORT_MANIFEST_VERSION",
    "REPORT_RENDERER_ID", "REPORT_VERSION", "RUN_OUTCOME_REFINALIZATION_SCOPE",
    "STATUS_POLICY_VERSION", "SUPPORTED_STATUS_POLICY_VERSIONS", "ActionId",
    "ActionOperationId", "ActionRecord", "ApprovalAction", "ApprovalCredential",
    "ApprovalError", "ApprovalLedgerResult", "ApprovalValidation", "ArtifactCaptureConfig",
    "ArtifactCaptureError", "ArtifactCapturePolicy", "ArtifactCaptureResult",
    "ArtifactContext", "ArtifactId", "ArtifactRecord", "ArtifactReservation",
    "ArtifactSanitizer", "ArtifactSuppression", "AssertionId", "AssertionRecord",
    "AssertionResult", "AtesAppendError", "AtesArtifactRepository", "AtesEventConflict",
    "AtesEventStore", "AtesId", "AtesStoreBusy", "AtesStoreCorruption", "AtesStoreError",
    "AuthorizationDecision", "CompletedRunPackage", "CorrectionId", "EventEnvelope",
    "EventId", "EventType", "EvidenceContext", "EvidenceDisposition",
    "EvidencePrivacyConfig", "EvidencePrivacyPolicy", "EvidenceValue", "ExecutionKind",
    "FinalizationError", "FinalizationId", "FinalizationResult", "FinalizationTrustInspection",
    "FinalizationTrustState", "FindingId", "FindingRecord", "FrozenDict", "KeyResolver",
    "LifecycleState", "ObservationId", "ObservationRecord", "PackageCompletionError",
    "PrivacyPolicyError", "ProtectedEvidenceSink", "ReportBundle", "ReportError",
    "ReportVerificationResult", "RequirementIdentity", "RoamSource", "RunId",
    "RunOutcomeRevision", "RunRecord", "RunStatus", "ScriptedSource", "SourceCommitment",
    "StatusInputs", "StepAttemptId", "StepAttemptRecord", "StepAttemptStatus", "StepId",
    "StepRecord", "StoredEvent", "Verification", "VerificationStatus", "append_approval",
    "append_audit_event", "complete_run_package", "derive_run_status", "effective_outcome",
    "ensure_detached_ledgers", "finalize_revision_one", "inspect_finalization_trust",
    "inspect_report_bundle", "record_finalization_audit", "recover_revision_one",
    "render_reports", "revoke_approval", "to_json_compatible", "validate_approvals",
    "validate_artifact_path", "validate_audit_chain", "validate_step_attempt_history",
    "validate_step_evidence_relationships", "verify_finalized_run", "verify_report_bundle",
]

# Install cross-cutting trust-boundary hardening only after the canonical modules
# above are fully initialized. Public callable identities/signatures stay intact;
# the guards replace internal validation hooks used by those callables.
from .trust_guards import install as _install_trust_guards

_install_trust_guards()
del _install_trust_guards

# Authority-bearing identifiers and retry transitions must use the same policy
# after the base trust hooks are installed.  This remains a purpose-based guard,
# not a review-round implementation layer.
from .authority_guards import install as _install_authority_guards

_install_authority_guards()
del _install_authority_guards

# Derived reports, detached ledgers, and crash recovery need transaction-level
# authority in addition to namespace identity. Install those shared guards last
# so they wrap the already-hardened report and audit transactions.
from .transaction_guards import install as _install_transaction_guards

_install_transaction_guards()
del _install_transaction_guards
