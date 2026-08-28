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

# Preserve the known deterministic Failure Capsule result only for the one
# intentionally missing TARGET_CLOSED contribution.  Round 3 restores any
# independent canonical execution error before status is exposed.
_hardened_derive = _finalization_impl._derive


def _derive_preserving_known_failure(events, run_id):
    snapshot = tuple(events)
    state = _hardened_derive(snapshot, run_id)
    inputs = state.status_inputs
    if not (inputs.execution_error and inputs.deterministic_failure):
        return state

    launch_index = next(
        (
            index
            for index, event in enumerate(snapshot)
            if event.envelope.event_type is EventType.TARGET_LAUNCHED
        ),
        None,
    )
    close_index = next(
        (
            index
            for index, event in enumerate(snapshot)
            if event.envelope.event_type is EventType.TARGET_CLOSED
        ),
        None,
    )
    release_index = next(
        (
            index
            for index, event in enumerate(snapshot)
            if event.envelope.event_type is EventType.ENVIRONMENT_RELEASED
        ),
        None,
    )
    retained_index = next(
        (
            index
            for index, event in enumerate(snapshot)
            if event.envelope.event_type is EventType.FAILURE_CAPSULE_RETAINED
            and event.payload.get("retained") is True
        ),
        None,
    )
    retention_evidence = (
        launch_index is not None
        and retained_index is not None
        and release_index is not None
        and launch_index < retained_index < release_index
    )
    provisional_fail = any(
        e.envelope.event_type is EventType.RUN_MARKED_INCOMPLETE
        and e.payload.get("reason") == "runtime.finalization_pending"
        and str(e.payload.get("execution_result") or "").strip().lower() in {"fail", "failed"}
        for e in snapshot
    )
    nonprovisional_incomplete = any(
        e.envelope.event_type is EventType.RUN_MARKED_INCOMPLETE
        and e.payload.get("reason") != "runtime.finalization_pending"
        for e in snapshot
    )
    unresolved_action = any(e.envelope.event_type is EventType.ACTION_OUTCOME_UNKNOWN for e in snapshot)
    if (
        launch_index is not None
        and close_index is None
        and release_index is not None
        and retention_evidence
        and provisional_fail
        and not nonprovisional_incomplete
        and not unresolved_action
    ):
        return _dc_replace(state, status_inputs=_dc_replace(inputs, execution_error=False))
    return state


_finalization_impl._derive = _derive_preserving_known_failure

from .finalization_round3 import install as _install_finalization_round3
_install_finalization_round3(_finalization_impl)

from .finalization_round4 import install as _install_finalization_round4
_install_finalization_round4()

from .finalization_round5 import install as _install_finalization_round5
_install_finalization_round5()

# Detached audit/report APIs are installed only after the canonical finalization
# compatibility layers are complete.  Recovery runs after runtime recorders are
# closed, so it is the safe boundary for report regeneration/re-verification.
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
    complete_run_package, install_recovery_completion as _install_recovery_completion,
)

_install_recovery_completion(_finalization_impl)
finalize_revision_one = _finalization_impl.finalize_revision_one
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
