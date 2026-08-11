import json
from dataclasses import asdict
from datetime import datetime, timezone

import pytest

from argus.ates import (
    ATES_VERSION, RUN_OUTCOME_REFINALIZATION_SCOPE, STATUS_POLICY_VERSION,
    ActionId, ActionOperationId, ActionRecord, ArtifactId, ArtifactRecord,
    AssertionId, AssertionRecord, AssertionResult, AuthorizationDecision, CorrectionId,
    EventEnvelope, EventId, EventType, EvidenceDisposition, EvidenceValue, ExecutionKind,
    FinalizationId, ObservationId, ObservationRecord, RequirementIdentity, RoamSource,
    RunId, RunOutcomeRevision, RunRecord, RunStatus, ScriptedSource, SourceCommitment,
    StatusInputs, StepAttemptId, StepAttemptRecord, StepAttemptStatus, StepId, StepRecord,
    Verification, VerificationStatus, derive_run_status, effective_outcome,
    to_json_compatible, validate_artifact_path, validate_step_attempt_history,
)

NOW = datetime(2026, 8, 11, 6, 0, tzinfo=timezone.utc)
CONFIG_COMMITMENT = SourceCommitment(
    "hmac-sha256", "hmac:runtime-config", "ates-config-v1"
)
ARTIFACT_DIGEST = SourceCommitment("sha256", "sha256:final-persisted-bytes")
VERIFIED_REVIEWER = Verification(
    VerificationStatus.VERIFIED,
    method="signature",
    actor="qa-reviewer",
    binding_ref="sig://review/authorized",
)
AUTHORIZED_REFINALIZATION = AuthorizationDecision(
    True,
    RUN_OUTCOME_REFINALIZATION_SCOPE,
    "argus-evidence-authz",
    "2026.08",
    "authz://decision/authorized-refinalization",
    VERIFIED_REVIEWER,
)


def test_typed_ids_validate_prefix_and_generate_unique_values():
    run_a = RunId.new()
    run_b = RunId.new()
    assert run_a.startswith("RUN-")
    assert run_a != run_b
    with pytest.raises(ValueError):
        RunId("EVT-not-a-run")
    with pytest.raises(ValueError):
        StepId("STEP-../escape")


def test_event_envelope_requires_supported_version_positive_integer_sequence_and_aware_time():
    envelope = EventEnvelope(
        ATES_VERSION, RunId.new(), EventId.new(), 1, EventType.RUN_STARTED, NOW
    )
    assert envelope.sequence == 1
    with pytest.raises(ValueError, match="positive integer"):
        EventEnvelope(ATES_VERSION, RunId.new(), EventId.new(), 0, EventType.RUN_STARTED, NOW)
    for invalid_sequence in (True, 1.5):
        with pytest.raises(ValueError, match="positive integer"):
            EventEnvelope(
                ATES_VERSION, RunId.new(), EventId.new(), invalid_sequence,
                EventType.RUN_STARTED, NOW,
            )
    with pytest.raises(ValueError, match="timezone-aware"):
        EventEnvelope(
            ATES_VERSION, RunId.new(), EventId.new(), 1, EventType.RUN_STARTED,
            datetime(2026, 8, 11, 6, 0),
        )


def test_event_envelope_normalizes_and_validates_typed_ids():
    run_id = RunId.new()
    event_id = EventId.new()
    envelope = EventEnvelope(
        ATES_VERSION, str(run_id), str(event_id), 1, EventType.RUN_STARTED, NOW  # type: ignore[arg-type]
    )
    assert isinstance(envelope.run_id, RunId)
    assert isinstance(envelope.event_id, EventId)

    with pytest.raises(ValueError, match="run_id"):
        EventEnvelope(
            ATES_VERSION, "EVT-wrong", str(event_id), 1,  # type: ignore[arg-type]
            EventType.RUN_STARTED, NOW,
        )
    with pytest.raises(ValueError, match="event_id"):
        EventEnvelope(
            ATES_VERSION, str(run_id), "RUN-wrong", 1,  # type: ignore[arg-type]
            EventType.RUN_STARTED, NOW,
        )


def test_event_type_is_normalized_from_serialized_input():
    envelope = EventEnvelope(
        ATES_VERSION, RunId.new(), EventId.new(), 1, "RUN_STARTED", NOW  # type: ignore[arg-type]
    )
    assert envelope.event_type is EventType.RUN_STARTED
    with pytest.raises(ValueError, match="event_type"):
        EventEnvelope(
            ATES_VERSION, RunId.new(), EventId.new(), 1, "NOT_AN_EVENT", NOW  # type: ignore[arg-type]
        )


def test_run_source_identity_is_conditional_on_execution_kind():
    commitment = SourceCommitment(
        "sha256_redacted_canonical", "sha256:abc", "ates-source-v1"
    )
    scripted = RunRecord(
        RunId.new(), ExecutionKind.SCRIPTED, ScriptedSource("TEST-1", commitment),
        NOW, "0.1.0", "desktop-gui", "capsule", "standard", CONFIG_COMMITMENT,
    )
    assert scripted.source.test_case_id == "TEST-1"
    assert scripted.configuration_commitment is CONFIG_COMMITMENT

    roam_source = RoamSource(
        objective_present=False,
        config_commitment=SourceCommitment("hmac-sha256", "hmac:123"),
    )
    roam = RunRecord(
        RunId.new(), ExecutionKind.ROAM, roam_source, NOW, "0.1.0",
        "desktop-gui", "capsule", "standard", CONFIG_COMMITMENT,
    )
    assert roam.source.kind == "roam_session"

    with pytest.raises(ValueError, match="ScriptedSource"):
        RunRecord(
            RunId.new(), ExecutionKind.SCRIPTED, roam_source, NOW, "0.1.0",
            "desktop-gui", "local", "standard", CONFIG_COMMITMENT,
        )


def test_execution_kind_is_normalized_before_source_validation():
    commitment = SourceCommitment(
        "sha256_redacted_canonical", "sha256:abc", "ates-source-v1"
    )
    scripted_source = ScriptedSource("TEST-1", commitment)
    record = RunRecord(
        RunId.new(), "scripted", scripted_source, NOW, "0.1.0",  # type: ignore[arg-type]
        "desktop-gui", "capsule", "standard", CONFIG_COMMITMENT,
    )
    assert record.execution_kind is ExecutionKind.SCRIPTED

    roam_source = RoamSource(
        objective_present=False,
        config_commitment=SourceCommitment("hmac-sha256", "hmac:123"),
    )
    with pytest.raises(ValueError, match="ScriptedSource"):
        RunRecord(
            RunId.new(), "scripted", roam_source, NOW, "0.1.0",  # type: ignore[arg-type]
            "desktop-gui", "capsule", "standard", CONFIG_COMMITMENT,
        )
    with pytest.raises(ValueError, match="execution_kind"):
        RunRecord(
            RunId.new(), "unknown", scripted_source, NOW, "0.1.0",  # type: ignore[arg-type]
            "desktop-gui", "capsule", "standard", CONFIG_COMMITMENT,
        )


def test_scripted_run_requires_run_configuration_commitment():
    source = ScriptedSource(
        "TEST-1", SourceCommitment("sha256_redacted_canonical", "sha256:abc")
    )
    with pytest.raises(ValueError, match="configuration_commitment"):
        RunRecord(
            RunId.new(), ExecutionKind.SCRIPTED, source, NOW, "0.1.0",
            "desktop-gui", "capsule", "standard", "raw-config",  # type: ignore[arg-type]
        )


def test_roam_source_rejects_contradictory_objective_metadata():
    commitment = SourceCommitment("sha256_redacted_canonical", "sha256:objective")
    with pytest.raises(ValueError, match="objective_present=false"):
        RoamSource(
            objective_present=False,
            objective_commitment=commitment,
            config_commitment=SourceCommitment("hmac-sha256", "hmac:config"),
        )


def test_sensitive_values_never_require_plaintext_in_ordinary_evidence():
    redacted = EvidenceValue.redacted("secret_input", secret_refs=("SECRET-login",))
    assert redacted.value == "<redacted>"
    protected = EvidenceValue.protected(
        "protected://obs/1", "authorized_sensitive_observation"
    )
    assert protected.value is None

    with pytest.raises(ValueError, match="exactly"):
        EvidenceValue(EvidenceDisposition.REDACTED, value="password123", reason="secret")


def test_raw_disposition_is_normalized_before_secret_safety_validation():
    with pytest.raises(ValueError, match="exactly"):
        EvidenceValue("redacted", value="password123", reason="secret")  # type: ignore[arg-type]

    value = EvidenceValue("redacted", value="<redacted>", reason="secret")  # type: ignore[arg-type]
    assert value.disposition is EvidenceDisposition.REDACTED


def test_secret_references_are_snapshotted_before_validation():
    mutable_refs = ["SECRET-login"]
    value = EvidenceValue(
        "redacted", value="<redacted>", reason="secret",  # type: ignore[arg-type]
        secret_refs=mutable_refs,  # type: ignore[arg-type]
    )
    mutable_refs.clear()
    assert value.secret_refs == ("SECRET-login",)
    with pytest.raises(ValueError, match="secret_refs"):
        EvidenceValue(
            "redacted", value="<redacted>", reason="secret",  # type: ignore[arg-type]
            secret_refs="SECRET-login",  # type: ignore[arg-type]
        )


def test_frozen_json_remains_serializable_through_normal_and_explicit_paths():
    value = EvidenceValue.safe({"items": ["one", {"nested": True}]})
    assert json.loads(json.dumps(asdict(value)))["value"]["items"][1]["nested"] is True

    observation = ObservationRecord(
        ObservationId.new(), StepAttemptId.new(), "screen", NOW,
        "capture-standard-v1", {"payload": value},
    )
    encoded = json.dumps(to_json_compatible(observation))
    assert json.loads(encoded)["captured_at"] == NOW.isoformat()


def test_step_instruction_requires_secret_safe_evidence_value():
    step = StepRecord(StepId.new(), EvidenceValue.safe("Open settings"))
    assert step.instruction.value == "Open settings"
    with pytest.raises(ValueError, match="instruction"):
        StepRecord(StepId.new(), "Type password123")  # type: ignore[arg-type]


def test_action_parameters_are_explicit_evidence_values():
    action = ActionRecord(
        ActionId.new(), StepId.new(), StepAttemptId.new(), "type",
        {"text": EvidenceValue.redacted("typed_input")}, ActionOperationId.new(),
    )
    assert action.parameters["text"].disposition is EvidenceDisposition.REDACTED
    with pytest.raises(ValueError, match="EvidenceValue"):
        ActionRecord(
            ActionId.new(), StepId.new(), StepAttemptId.new(), "type",
            {"text": "plaintext"},  # type: ignore[arg-type]
        )


def test_core_records_normalize_typed_ids_at_runtime():
    step_id = StepId.new()
    attempt_id = StepAttemptId.new()
    action_id = ActionId.new()
    operation_id = ActionOperationId.new()
    action = ActionRecord(
        str(action_id), str(step_id), str(attempt_id), "click",  # type: ignore[arg-type]
        {}, str(operation_id),  # type: ignore[arg-type]
    )
    assert isinstance(action.action_id, ActionId)
    assert isinstance(action.step_id, StepId)
    assert isinstance(action.step_attempt_id, StepAttemptId)
    assert isinstance(action.operation_id, ActionOperationId)

    with pytest.raises(ValueError, match="step_id"):
        StepRecord("RUN-wrong", EvidenceValue.safe("Open"))  # type: ignore[arg-type]


def test_assertion_result_and_evidence_fields_are_runtime_validated():
    assertion = AssertionRecord(
        AssertionId.new(), StepId.new(), StepAttemptId.new(), "text_visible",
        EvidenceValue.safe("Ready"), "failed", "deterministic",  # type: ignore[arg-type]
    )
    assert assertion.result is AssertionResult.FAILED
    with pytest.raises(ValueError, match="expected value"):
        AssertionRecord(
            AssertionId.new(), StepId.new(), StepAttemptId.new(), "text_visible",
            "Ready", AssertionResult.PASSED, "deterministic",  # type: ignore[arg-type]
        )


def test_step_attempts_have_immutable_identity_and_positive_ordinal():
    attempt = StepAttemptRecord(
        StepAttemptId.new(), StepId.new(), 2, StepAttemptStatus.PASSED,
        NOW, NOW, EvidenceValue.safe("first attempt assertion failed"),
    )
    assert attempt.attempt == 2
    for invalid_attempt in (0, True, 1.5):
        with pytest.raises(ValueError, match="positive integer"):
            StepAttemptRecord(
                StepAttemptId.new(), StepId.new(), invalid_attempt,
                StepAttemptStatus.RUNNING, NOW,  # type: ignore[arg-type]
            )


def test_step_attempt_history_requires_unique_contiguous_ordinals_per_step():
    step_id = StepId.new()
    first = StepAttemptRecord(
        StepAttemptId.new(), step_id, 1, StepAttemptStatus.PASSED, NOW, NOW
    )
    second = StepAttemptRecord(
        StepAttemptId.new(), step_id, 2, StepAttemptStatus.PASSED, NOW, NOW,
        EvidenceValue.safe("retry after assertion failure"),
    )
    assert validate_step_attempt_history((second, first)) == (second, first)

    duplicate = StepAttemptRecord(
        StepAttemptId.new(), step_id, 1, StepAttemptStatus.PASSED, NOW, NOW
    )
    with pytest.raises(ValueError, match="unique and contiguous"):
        validate_step_attempt_history((first, duplicate))

    third = StepAttemptRecord(
        StepAttemptId.new(), step_id, 3, StepAttemptStatus.PASSED, NOW, NOW,
        EvidenceValue.safe("retry after second failure"),
    )
    with pytest.raises(ValueError, match="unique and contiguous"):
        validate_step_attempt_history((first, third))


def test_step_attempt_status_is_normalized_before_lifecycle_validation():
    attempt = StepAttemptRecord(
        StepAttemptId.new(), StepId.new(), 1, "passed", NOW, NOW  # type: ignore[arg-type]
    )
    assert attempt.status is StepAttemptStatus.PASSED
    with pytest.raises(ValueError, match="step attempt status"):
        StepAttemptRecord(
            StepAttemptId.new(), StepId.new(), 1, "unknown", NOW  # type: ignore[arg-type]
        )


def test_step_attempt_lifecycle_requires_consistent_end_timestamp():
    with pytest.raises(ValueError, match="terminal attempts require ended_at"):
        StepAttemptRecord(
            StepAttemptId.new(), StepId.new(), 1, StepAttemptStatus.PASSED, NOW
        )

    with pytest.raises(ValueError, match="running attempts cannot have ended_at"):
        StepAttemptRecord(
            StepAttemptId.new(), StepId.new(), 1, StepAttemptStatus.RUNNING, NOW, NOW
        )


def test_retry_attempt_requires_secret_safe_nonempty_reason():
    with pytest.raises(ValueError, match="retry attempts require retry_reason"):
        StepAttemptRecord(
            StepAttemptId.new(), StepId.new(), 2, StepAttemptStatus.RUNNING, NOW
        )

    with pytest.raises(ValueError, match="non-empty"):
        StepAttemptRecord(
            StepAttemptId.new(), StepId.new(), 2, StepAttemptStatus.RUNNING, NOW,
            retry_reason=EvidenceValue.safe(""),
        )

    retry = StepAttemptRecord(
        StepAttemptId.new(), StepId.new(), 2, StepAttemptStatus.RUNNING, NOW,
        retry_reason=EvidenceValue.redacted("retry_reason_contains_secret"),
    )
    assert retry.retry_reason.disposition is EvidenceDisposition.REDACTED


@pytest.mark.parametrize(
    ("inputs", "expected"),
    [
        (StatusInputs(), RunStatus.PASSED),
        (StatusInputs(required_assertion_results=(AssertionResult.FAILED,)), RunStatus.FAILED),
        (StatusInputs(required_assertion_results=(AssertionResult.UNEVALUATED,)), RunStatus.ERROR),
        (StatusInputs(required_assertions_satisfied=False), RunStatus.ERROR),
        (StatusInputs(unresolved_action_outcome=True), RunStatus.ERROR),
        (
            StatusInputs(
                deterministic_failure=True, cancelled=True,
                required_assertion_results=(AssertionResult.PASSED,),
            ),
            RunStatus.FAILED,
        ),
        (StatusInputs(cancelled=True), RunStatus.CANCELLED),
        (
            StatusInputs(cancelled=True, required_steps_satisfied=False),
            RunStatus.CANCELLED,
        ),
        (
            StatusInputs(cancelled=True, required_assertions_satisfied=False),
            RunStatus.CANCELLED,
        ),
        (
            StatusInputs(
                cancelled=True,
                required_steps_satisfied=False,
                required_assertion_results=(AssertionResult.UNEVALUATED,),
            ),
            RunStatus.CANCELLED,
        ),
    ],
)
def test_canonical_status_precedence(inputs, expected):
    assert derive_run_status(inputs) is expected


def test_status_inputs_normalize_serialized_assertion_results():
    failed = StatusInputs(required_assertion_results=("failed",))  # type: ignore[arg-type]
    errored = StatusInputs(required_assertion_results=("error",))  # type: ignore[arg-type]
    assert failed.required_assertion_results == (AssertionResult.FAILED,)
    assert derive_run_status(failed) is RunStatus.FAILED
    assert derive_run_status(errored) is RunStatus.ERROR
    with pytest.raises(ValueError, match="AssertionResult"):
        StatusInputs(required_assertion_results=("bogus",))  # type: ignore[arg-type]


def test_status_inputs_require_real_booleans():
    with pytest.raises(ValueError, match="cancelled"):
        StatusInputs(cancelled="false")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="required_assertions_satisfied"):
        StatusInputs(required_assertions_satisfied="true")  # type: ignore[arg-type]


def test_artifact_paths_are_package_relative_and_confined():
    assert validate_artifact_path("artifacts/STEP-1/screenshot.png") == (
        "artifacts/STEP-1/screenshot.png"
    )
    artifact = ArtifactRecord(
        ArtifactId.new(), "screenshot", "artifacts/shot.png",
        "internal", "capture-standard-v1", ARTIFACT_DIGEST, 123,
        EvidenceDisposition.REDACTED,
    )
    assert artifact.path == "artifacts/shot.png"
    assert artifact.capture_policy == "capture-standard-v1"
    assert artifact.content_digest is ARTIFACT_DIGEST
    assert artifact.size_bytes == 123

    for bad in (
        "/etc/passwd", "../outside", "artifacts/../outside",
        r"artifacts\..\outside", "artifacts/%2e%2e/outside",
        "C:/Windows/system.ini", "artifacts//shot.png",
        "artifacts/./shot.png", "artifacts/shot.png/",
        "artifacts/\x00shot.png", "artifacts/\nshot.png",
    ):
        with pytest.raises(ValueError):
            validate_artifact_path(bad)


def test_retained_artifact_requires_applied_capture_policy():
    with pytest.raises(ValueError, match="capture_policy"):
        ArtifactRecord(
            ArtifactId.new(), "screenshot", "artifacts/shot.png",
            "internal", "", ARTIFACT_DIGEST, 123, EvidenceDisposition.REDACTED,
        )


def test_retained_artifact_is_bound_to_final_persisted_bytes():
    with pytest.raises(ValueError, match="content_digest"):
        ArtifactRecord(
            ArtifactId.new(), "screenshot", "artifacts/shot.png",
            "internal", "capture-standard-v1", "sha256:raw", 123,  # type: ignore[arg-type]
            EvidenceDisposition.REDACTED,
        )
    for invalid_size in (-1, True, 1.5):
        with pytest.raises(ValueError, match="size_bytes"):
            ArtifactRecord(
                ArtifactId.new(), "screenshot", "artifacts/shot.png",
                "internal", "capture-standard-v1", ARTIFACT_DIGEST,
                invalid_size, EvidenceDisposition.REDACTED,  # type: ignore[arg-type]
            )


def test_retained_artifact_record_cannot_claim_suppression():
    with pytest.raises(ValueError, match="ARTIFACT_SUPPRESSED"):
        ArtifactRecord(
            ArtifactId.new(), "screenshot", "artifacts/shot.png",
            "secret", "capture-standard-v1", ARTIFACT_DIGEST, 123,
            EvidenceDisposition.SUPPRESSED,
        )

    with pytest.raises(ValueError, match="ARTIFACT_SUPPRESSED"):
        ArtifactRecord(
            ArtifactId.new(), "screenshot", "artifacts/shot.png",
            "secret", "capture-standard-v1", ARTIFACT_DIGEST, 123,
            "suppressed",  # type: ignore[arg-type]
        )


def test_requirement_identity_cannot_rely_on_mutable_display_id_alone():
    with pytest.raises(ValueError, match="version"):
        RequirementIdentity("REQ-42", "product-requirements")

    identity = RequirementIdentity(
        "REQ-42", "product-requirements", version="3",
        source_revision="release-2026.08",
        commitment=SourceCommitment("sha256_redacted_canonical", "sha256:req"),
    )
    assert identity.identity_key[2] == "3"


def test_requirement_identity_key_includes_commitment_semantics():
    left = RequirementIdentity(
        "REQ-42", "product-requirements",
        commitment=SourceCommitment(
            "sha256", "same-value", "canonical-v1", "verify://one"
        ),
    )
    different_method = RequirementIdentity(
        "REQ-42", "product-requirements",
        commitment=SourceCommitment(
            "hmac-sha256", "same-value", "canonical-v1", "verify://one"
        ),
    )
    different_profile = RequirementIdentity(
        "REQ-42", "product-requirements",
        commitment=SourceCommitment(
            "sha256", "same-value", "canonical-v2", "verify://one"
        ),
    )

    assert left.identity_key != different_method.identity_key
    assert left.identity_key != different_profile.identity_key


def test_safe_nested_values_and_record_mappings_are_immutable_snapshots():
    original = {"items": ["one", "two"]}
    value = EvidenceValue.safe(original)
    original["items"].append("three")
    assert tuple(value.value["items"]) == ("one", "two")  # type: ignore[index]

    parameters = {"text": EvidenceValue.safe("hello")}
    action = ActionRecord(
        ActionId.new(), StepId.new(), StepAttemptId.new(), "type", parameters
    )
    parameters["text"] = EvidenceValue.safe("changed")
    assert action.parameters["text"].value == "hello"
    with pytest.raises(TypeError):
        action.parameters["other"] = EvidenceValue.safe("x")  # type: ignore[index]


def test_verified_records_require_independent_binding_reference():
    with pytest.raises(ValueError, match="binding_ref"):
        Verification(
            "verified",  # type: ignore[arg-type]
            method="signature",
            actor="qa-reviewer",
        )
    verified = Verification(
        "verified", method="signature", actor="qa-reviewer",  # type: ignore[arg-type]
        binding_ref="sig://review/1",
    )
    assert verified.status is VerificationStatus.VERIFIED


def test_status_bearing_correction_requires_verified_and_authorized_refinalization():
    run_id = RunId.new()
    first = RunOutcomeRevision(
        FinalizationId.new(), run_id, 1, RunStatus.PASSED, 1, NOW
    )

    with pytest.raises(ValueError, match="authenticated/verified"):
        RunOutcomeRevision(
            FinalizationId.new(), run_id, 2, RunStatus.FAILED, 2, NOW,
            supersedes_finalization_id=first.finalization_id,
            correction_ids=(CorrectionId.new(),),
            verification=Verification(VerificationStatus.UNVERIFIED),
        )

    with pytest.raises(ValueError, match="authorization decision"):
        RunOutcomeRevision(
            FinalizationId.new(), run_id, 2, RunStatus.FAILED, 2, NOW,
            supersedes_finalization_id=first.finalization_id,
            correction_ids=(CorrectionId.new(),),
            verification=VERIFIED_REVIEWER,
        )

    denied = AuthorizationDecision(
        False, RUN_OUTCOME_REFINALIZATION_SCOPE, "argus-evidence-authz", "2026.08",
        "authz://decision/denied", VERIFIED_REVIEWER,
    )
    with pytest.raises(ValueError, match="not authorized"):
        RunOutcomeRevision(
            FinalizationId.new(), run_id, 2, RunStatus.FAILED, 2, NOW,
            supersedes_finalization_id=first.finalization_id,
            correction_ids=(CorrectionId.new(),),
            verification=VERIFIED_REVIEWER,
            authorization=denied,
        )

    second = RunOutcomeRevision(
        FinalizationId.new(), run_id, 2, RunStatus.FAILED, 2, NOW,
        supersedes_finalization_id=first.finalization_id,
        correction_ids=(CorrectionId.new(),),
        verification=VERIFIED_REVIEWER,
        authorization=AUTHORIZED_REFINALIZATION,
    )
    assert first.effective_status is RunStatus.PASSED
    assert effective_outcome((first, second)).effective_status is RunStatus.FAILED


def test_refinalization_authorization_is_verified_versioned_and_scope_bound():
    with pytest.raises(ValueError, match="itself be verified"):
        AuthorizationDecision(
            True, RUN_OUTCOME_REFINALIZATION_SCOPE, "argus-evidence-authz", "2026.08",
            "authz://decision/unverified", Verification(VerificationStatus.UNVERIFIED),
        )

    run_id = RunId.new()
    first = RunOutcomeRevision(
        FinalizationId.new(), run_id, 1, RunStatus.PASSED, 1, NOW
    )
    wrong_scope = AuthorizationDecision(
        True, "evidence.supersede_finding", "argus-evidence-authz", "2026.08",
        "authz://decision/wrong-scope", VERIFIED_REVIEWER,
    )
    with pytest.raises(ValueError, match="does not cover"):
        RunOutcomeRevision(
            FinalizationId.new(), run_id, 2, RunStatus.FAILED, 2, NOW,
            supersedes_finalization_id=first.finalization_id,
            correction_ids=(CorrectionId.new(),),
            verification=VERIFIED_REVIEWER,
            authorization=wrong_scope,
        )


def test_refinalization_requires_validated_verification_instance():
    class ForgedVerification:
        status = VerificationStatus.VERIFIED

    run_id = RunId.new()
    first = RunOutcomeRevision(
        FinalizationId.new(), run_id, 1, RunStatus.PASSED, 1, NOW
    )
    with pytest.raises(ValueError, match="validated Verification"):
        RunOutcomeRevision(
            FinalizationId.new(), run_id, 2, RunStatus.FAILED, 2, NOW,
            supersedes_finalization_id=first.finalization_id,
            correction_ids=(CorrectionId.new(),),
            verification=ForgedVerification(),  # type: ignore[arg-type]
            authorization=AUTHORIZED_REFINALIZATION,
        )


def test_effective_status_is_normalized_and_invalid_values_are_rejected():
    run_id = RunId.new()
    normalized = RunOutcomeRevision(
        FinalizationId.new(), run_id, 1, "passed", 1, NOW  # type: ignore[arg-type]
    )
    assert normalized.effective_status is RunStatus.PASSED
    with pytest.raises(ValueError, match="effective_status"):
        RunOutcomeRevision(
            FinalizationId.new(), run_id, 1, "bogus", 1, NOW  # type: ignore[arg-type]
        )


def test_outcome_revision_rejects_unsupported_status_policy_version():
    RunOutcomeRevision(
        FinalizationId.new(), RunId.new(), 1, RunStatus.PASSED, 1, NOW,
        status_policy_version=STATUS_POLICY_VERSION,
    )
    with pytest.raises(ValueError, match="unsupported status_policy_version"):
        RunOutcomeRevision(
            FinalizationId.new(), RunId.new(), 1, RunStatus.PASSED, 1, NOW,
            status_policy_version="unknown",
        )


def test_outcome_revision_numbers_require_positive_integers():
    run_id = RunId.new()
    for revision, evidence_revision in ((True, 1), (1.5, 1), (1, True), (1, 1.5)):
        with pytest.raises(ValueError, match="positive integer"):
            RunOutcomeRevision(
                FinalizationId.new(), run_id, revision, RunStatus.PASSED,
                evidence_revision, NOW,  # type: ignore[arg-type]
            )


def test_correction_ids_are_snapshotted_before_refinalization_validation():
    run_id = RunId.new()
    first = RunOutcomeRevision(
        FinalizationId.new(), run_id, 1, RunStatus.PASSED, 1, NOW
    )
    mutable_ids = [CorrectionId.new()]
    second = RunOutcomeRevision(
        FinalizationId.new(), run_id, 2, RunStatus.FAILED, 2, NOW,
        supersedes_finalization_id=first.finalization_id,
        correction_ids=mutable_ids,  # type: ignore[arg-type]
        verification=VERIFIED_REVIEWER,
        authorization=AUTHORIZED_REFINALIZATION,
    )
    captured = second.correction_ids
    mutable_ids.clear()
    assert second.correction_ids == captured
    assert second.correction_ids


def test_correction_ids_are_typed_before_authorizing_refinalization():
    run_id = RunId.new()
    first = RunOutcomeRevision(
        FinalizationId.new(), run_id, 1, RunStatus.PASSED, 1, NOW
    )
    raw_correction = str(CorrectionId.new())
    normalized = RunOutcomeRevision(
        FinalizationId.new(), run_id, 2, RunStatus.FAILED, 2, NOW,
        supersedes_finalization_id=first.finalization_id,
        correction_ids=[raw_correction],  # type: ignore[arg-type]
        verification=VERIFIED_REVIEWER,
        authorization=AUTHORIZED_REFINALIZATION,
    )
    assert isinstance(normalized.correction_ids[0], CorrectionId)

    with pytest.raises(ValueError, match="correction_id"):
        RunOutcomeRevision(
            FinalizationId.new(), run_id, 2, RunStatus.FAILED, 2, NOW,
            supersedes_finalization_id=first.finalization_id,
            correction_ids=["EVT-not-a-correction"],  # type: ignore[arg-type]
            verification=VERIFIED_REVIEWER,
            authorization=AUTHORIZED_REFINALIZATION,
        )
    with pytest.raises(ValueError, match="correction_ids"):
        RunOutcomeRevision(
            FinalizationId.new(), run_id, 2, RunStatus.FAILED, 2, NOW,
            supersedes_finalization_id=first.finalization_id,
            correction_ids=raw_correction,  # type: ignore[arg-type]
            verification=VERIFIED_REVIEWER,
            authorization=AUTHORIZED_REFINALIZATION,
        )


def test_effective_outcome_can_render_historical_evidence_revision():
    run_id = RunId.new()
    first = RunOutcomeRevision(
        FinalizationId.new(), run_id, 1, RunStatus.PASSED, 1, NOW
    )
    second = RunOutcomeRevision(
        FinalizationId.new(), run_id, 2, RunStatus.FAILED, 3, NOW,
        supersedes_finalization_id=first.finalization_id,
        correction_ids=(CorrectionId.new(),),
        verification=VERIFIED_REVIEWER,
        authorization=AUTHORIZED_REFINALIZATION,
    )

    assert effective_outcome(
        (first, second), evidence_revision=1
    ).effective_status is RunStatus.PASSED
    assert effective_outcome(
        (first, second), evidence_revision=2
    ).effective_status is RunStatus.PASSED
    assert effective_outcome(
        (first, second), evidence_revision=3
    ).effective_status is RunStatus.FAILED
    for invalid_revision in (0, True, 1.5):
        with pytest.raises(ValueError, match="evidence_revision"):
            effective_outcome(
                (first, second), evidence_revision=invalid_revision  # type: ignore[arg-type]
            )


def test_effective_outcome_rejects_cross_run_revision_chains():
    first = RunOutcomeRevision(
        FinalizationId.new(), RunId.new(), 1, RunStatus.PASSED, 1, NOW
    )
    other_run = RunOutcomeRevision(
        FinalizationId.new(), RunId.new(), 1, RunStatus.PASSED, 1, NOW
    )
    with pytest.raises(ValueError, match="mix run IDs"):
        effective_outcome((first, other_run))
