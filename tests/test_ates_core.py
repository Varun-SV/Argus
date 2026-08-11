from datetime import datetime, timezone

import pytest

from argus.ates import (
    ATES_VERSION, ActionId, ActionOperationId, ActionRecord, ArtifactId, ArtifactRecord,
    AssertionResult, CorrectionId, EventEnvelope, EventId, EventType, EvidenceDisposition,
    EvidenceValue, ExecutionKind, FinalizationId, RequirementIdentity, RoamSource, RunId,
    RunOutcomeRevision, RunRecord, RunStatus, ScriptedSource, SourceCommitment,
    StatusInputs, StepAttemptId, StepAttemptRecord, StepAttemptStatus, StepId,
    Verification, VerificationStatus, derive_run_status, effective_outcome,
    validate_artifact_path,
)

NOW = datetime(2026, 8, 11, 6, 0, tzinfo=timezone.utc)


def test_typed_ids_validate_prefix_and_generate_unique_values():
    run_a = RunId.new()
    run_b = RunId.new()
    assert run_a.startswith("RUN-")
    assert run_a != run_b
    with pytest.raises(ValueError):
        RunId("EVT-not-a-run")
    with pytest.raises(ValueError):
        StepId("STEP-../escape")


def test_event_envelope_requires_supported_version_positive_sequence_and_aware_time():
    envelope = EventEnvelope(
        ATES_VERSION, RunId.new(), EventId.new(), 1, EventType.RUN_STARTED, NOW
    )
    assert envelope.sequence == 1
    with pytest.raises(ValueError, match="sequence"):
        EventEnvelope(ATES_VERSION, RunId.new(), EventId.new(), 0, EventType.RUN_STARTED, NOW)
    with pytest.raises(ValueError, match="timezone-aware"):
        EventEnvelope(
            ATES_VERSION, RunId.new(), EventId.new(), 1, EventType.RUN_STARTED,
            datetime(2026, 8, 11, 6, 0),
        )


def test_run_source_identity_is_conditional_on_execution_kind():
    commitment = SourceCommitment(
        "sha256_redacted_canonical", "sha256:abc", "ates-source-v1"
    )
    scripted = RunRecord(
        RunId.new(), ExecutionKind.SCRIPTED, ScriptedSource("TEST-1", commitment),
        NOW, "0.1.0", "desktop-gui", "capsule", "standard",
    )
    assert scripted.source.test_case_id == "TEST-1"

    roam_source = RoamSource(
        objective_present=False,
        config_commitment=SourceCommitment("hmac-sha256", "hmac:123"),
    )
    roam = RunRecord(
        RunId.new(), ExecutionKind.ROAM, roam_source, NOW, "0.1.0",
        "desktop-gui", "capsule", "standard",
    )
    assert roam.source.kind == "roam_session"

    with pytest.raises(ValueError, match="ScriptedSource"):
        RunRecord(
            RunId.new(), ExecutionKind.SCRIPTED, roam_source, NOW, "0.1.0",
            "desktop-gui", "local", "standard",
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


def test_step_attempts_have_immutable_identity_and_positive_ordinal():
    attempt = StepAttemptRecord(
        StepAttemptId.new(), StepId.new(), 2, StepAttemptStatus.PASSED,
        NOW, NOW, "first attempt assertion failed",
    )
    assert attempt.attempt == 2
    with pytest.raises(ValueError, match="attempt"):
        StepAttemptRecord(
            StepAttemptId.new(), StepId.new(), 0, StepAttemptStatus.RUNNING, NOW
        )


@pytest.mark.parametrize(
    ("inputs", "expected"),
    [
        (StatusInputs(), RunStatus.PASSED),
        (StatusInputs(required_assertion_results=(AssertionResult.FAILED,)), RunStatus.FAILED),
        (StatusInputs(required_assertion_results=(AssertionResult.UNEVALUATED,)), RunStatus.ERROR),
        (StatusInputs(unresolved_action_outcome=True), RunStatus.ERROR),
        (
            StatusInputs(
                deterministic_failure=True, cancelled=True,
                required_assertion_results=(AssertionResult.PASSED,),
            ),
            RunStatus.FAILED,
        ),
        (StatusInputs(cancelled=True), RunStatus.CANCELLED),
    ],
)
def test_canonical_status_precedence(inputs, expected):
    assert derive_run_status(inputs) is expected


def test_artifact_paths_are_package_relative_and_confined():
    assert validate_artifact_path("artifacts/STEP-1/screenshot.png") == (
        "artifacts/STEP-1/screenshot.png"
    )
    artifact = ArtifactRecord(
        ArtifactId.new(), "screenshot", "artifacts/shot.png",
        "internal", EvidenceDisposition.REDACTED,
    )
    assert artifact.path == "artifacts/shot.png"

    for bad in (
        "/etc/passwd", "../outside", "artifacts/../outside",
        r"artifacts\..\outside", "artifacts/%2e%2e/outside",
        "C:/Windows/system.ini",
    ):
        with pytest.raises(ValueError):
            validate_artifact_path(bad)


def test_requirement_identity_cannot_rely_on_mutable_display_id_alone():
    with pytest.raises(ValueError, match="version"):
        RequirementIdentity("REQ-42", "product-requirements")

    identity = RequirementIdentity(
        "REQ-42", "product-requirements", version="3",
        source_revision="release-2026.08",
        commitment=SourceCommitment("sha256_redacted_canonical", "sha256:req"),
    )
    assert identity.identity_key[2] == "3"


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
            VerificationStatus.VERIFIED,
            method="signature",
            actor="qa-reviewer",
        )


def test_status_bearing_correction_requires_verified_refinalization():
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

    second = RunOutcomeRevision(
        FinalizationId.new(), run_id, 2, RunStatus.FAILED, 2, NOW,
        supersedes_finalization_id=first.finalization_id,
        correction_ids=(CorrectionId.new(),),
        verification=Verification(
            VerificationStatus.VERIFIED, method="signature",
            actor="qa-reviewer", binding_ref="sig://review/2",
        ),
    )
    assert first.effective_status is RunStatus.PASSED
    assert effective_outcome((first, second)).effective_status is RunStatus.FAILED


def test_effective_outcome_rejects_cross_run_revision_chains():
    first = RunOutcomeRevision(
        FinalizationId.new(), RunId.new(), 1, RunStatus.PASSED, 1, NOW
    )
    other_run = RunOutcomeRevision(
        FinalizationId.new(), RunId.new(), 1, RunStatus.PASSED, 1, NOW
    )
    with pytest.raises(ValueError, match="mix run IDs"):
        effective_outcome((first, other_run))
