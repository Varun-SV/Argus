from datetime import datetime, timedelta, timezone, tzinfo

import pytest

from argus.ates import (
    ATES_VERSION,
    RUN_OUTCOME_REFINALIZATION_SCOPE,
    STATUS_POLICY_VERSION,
    AssertionResult,
    AuthorizationDecision,
    CorrectionId,
    EventEnvelope,
    EventId,
    EventType,
    EvidenceValue,
    FinalizationId,
    FindingId,
    FindingRecord,
    RunId,
    RunOutcomeRevision,
    RunStatus,
    StatusInputs,
    StepAttemptId,
    StepAttemptRecord,
    StepAttemptStatus,
    StepId,
    StepRecord,
    Verification,
    VerificationStatus,
    effective_outcome,
    to_json_compatible,
    validate_artifact_path,
    validate_step_attempt_history,
    validate_step_evidence_relationships,
)

NOW = datetime(2026, 8, 11, 10, 40, tzinfo=timezone.utc)
VERIFIED_REVIEWER = Verification(
    VerificationStatus.VERIFIED,
    method="signature",
    actor="qa-reviewer",
    binding_ref="sig://review/round8",
)
VERIFIED_AUTHZ = Verification(
    VerificationStatus.VERIFIED,
    method="policy-signature",
    actor="argus-authz-service",
    binding_ref="sig://authz/round8",
)


def authorization_for(
    run_id: RunId,
    finalization_id: FinalizationId,
    supersedes_finalization_id: FinalizationId,
    correction_ids: tuple[CorrectionId, ...],
    *,
    evidence_revision: int = 2,
    effective_status: RunStatus = RunStatus.FAILED,
) -> AuthorizationDecision:
    return AuthorizationDecision(
        True,
        RUN_OUTCOME_REFINALIZATION_SCOPE,
        "argus-evidence-authz",
        "2026.08",
        "authz://decision/round8",
        VERIFIED_AUTHZ,
        "qa-reviewer",
        run_id,
        finalization_id,
        supersedes_finalization_id,
        correction_ids,
        effective_status,
        evidence_revision,
        STATUS_POLICY_VERSION,
    )


def test_status_inputs_reject_collection_impostors_before_normalization():
    for malformed in ("", b"", bytearray(), {}, {"failed": True}, {"failed"}):
        with pytest.raises(ValueError, match="required_assertion_results"):
            StatusInputs(required_assertion_results=malformed)  # type: ignore[arg-type]

    normalized = StatusInputs(required_assertion_results=["failed"])  # type: ignore[arg-type]
    assert normalized.required_assertion_results == (AssertionResult.FAILED,)


def test_logical_steps_are_required_unique_and_snapshotted_with_attempts():
    step_id = StepId.new()
    step = StepRecord(step_id, EvidenceValue.safe("Open settings"))
    attempt = StepAttemptRecord(
        StepAttemptId.new(), step_id, 1, StepAttemptStatus.RUNNING, NOW
    )

    with pytest.raises(ValueError, match="unknown logical step_id"):
        validate_step_evidence_relationships((attempt,))

    duplicate = StepRecord(step_id, EvidenceValue.safe("Conflicting intent"))
    with pytest.raises(ValueError, match="step IDs must be unique"):
        validate_step_evidence_relationships(
            (attempt,), steps=(step, duplicate)
        )

    steps = [step]
    attempts = [attempt]
    snapshots = validate_step_evidence_relationships(attempts, steps=steps)
    steps.clear()
    attempts.clear()
    assert snapshots[0] == (step,)
    assert snapshots[1] == (attempt,)


def test_findings_store_machine_readable_classification_and_provenance():
    finding = FindingRecord(
        FindingId.new(),
        EvidenceValue.safe("Unsafe delete"),
        EvidenceValue.safe("Delete action bypassed guard"),
        classification_source="policy-engine-v2",
        classification="critical",
    )
    encoded = to_json_compatible(finding)
    assert encoded["classification"] == "critical"  # type: ignore[index]
    assert encoded["classification_source"] == "policy-engine-v2"  # type: ignore[index]

    with pytest.raises(ValueError, match="classification"):
        FindingRecord(
            FindingId.new(),
            EvidenceValue.safe("Title"),
            EvidenceValue.safe("Description"),
            classification="",
        )
    with pytest.raises(ValueError, match="classification"):
        FindingRecord(
            FindingId.new(),
            EvidenceValue.safe("Title"),
            EvidenceValue.safe("Description"),
            classification=["critical"],  # type: ignore[arg-type]
        )


def test_effective_outcome_rejects_time_reversed_authoritative_chain():
    run_id = RunId.new()
    first = RunOutcomeRevision(
        FinalizationId.new(), run_id, 1, RunStatus.PASSED, 1, NOW
    )
    second_id = FinalizationId.new()
    corrections = (CorrectionId.new(),)
    second = RunOutcomeRevision(
        second_id,
        run_id,
        2,
        RunStatus.FAILED,
        2,
        NOW - timedelta(seconds=1),
        supersedes_finalization_id=first.finalization_id,
        correction_ids=corrections,
        verification=VERIFIED_REVIEWER,
        authorization=authorization_for(
            run_id, second_id, first.finalization_id, corrections
        ),
    )

    with pytest.raises(ValueError, match="prior finalization time"):
        effective_outcome((first, second))


class MutableOffset(tzinfo):
    def __init__(self, minutes: int) -> None:
        self.minutes = minutes

    def utcoffset(self, dt):
        return timedelta(minutes=self.minutes)

    def dst(self, dt):
        return timedelta(0)

    def tzname(self, dt):
        return f"mutable-{self.minutes}"


def test_core_timestamps_snapshot_mutable_timezone_as_immutable_utc():
    mutable_zone = MutableOffset(330)
    supplied = datetime(2026, 8, 11, 9, 0, tzinfo=mutable_zone)
    envelope = EventEnvelope(
        ATES_VERSION,
        RunId.new(),
        EventId.new(),
        1,
        EventType.RUN_STARTED,
        supplied,
    )
    serialized_before = to_json_compatible(envelope)["occurred_at"]  # type: ignore[index]

    mutable_zone.minutes = -480

    assert envelope.occurred_at.tzinfo is timezone.utc
    assert envelope.occurred_at == datetime(2026, 8, 11, 3, 30, tzinfo=timezone.utc)
    assert to_json_compatible(envelope)["occurred_at"] == serialized_before  # type: ignore[index]


def test_artifact_paths_reject_windows_reserved_and_normalized_aliases():
    for invalid in (
        "artifacts/CON",
        "artifacts/con.txt",
        "artifacts/aux/image.png",
        "artifacts/COM1.log",
        "artifacts/lpt9/report.txt",
        "artifacts/file.",
        "artifacts/file ",
        "artifacts/foo?.png",
        "artifacts/bad|name.png",
    ):
        with pytest.raises(ValueError):
            validate_artifact_path(invalid)

    assert validate_artifact_path("artifacts/console/report.txt") == (
        "artifacts/console/report.txt"
    )


def test_secret_reference_helpers_do_not_pre_split_scalar_strings():
    with pytest.raises(ValueError, match="secret_refs must be a sequence"):
        EvidenceValue.redacted("secret_input", secret_refs="SECRET-login")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="secret_refs must be a sequence"):
        EvidenceValue(
            "redacted",  # type: ignore[arg-type]
            value="<redacted>",
            reason="secret_input",
            secret_refs={"SECRET-login": True},  # type: ignore[arg-type]
        )


def test_adjacent_canonical_sequence_fields_reject_mappings_and_scalars():
    with pytest.raises(ValueError, match="attempt history must be a sequence"):
        validate_step_attempt_history({})  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="finalization history must be a sequence"):
        effective_outcome({})  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="evidence_refs must be a sequence"):
        FindingRecord(
            FindingId.new(),
            EvidenceValue.safe("Title"),
            EvidenceValue.safe("Description"),
            evidence_refs={},  # type: ignore[arg-type]
        )

    with pytest.raises(ValueError, match="correction_ids must be a sequence"):
        RunOutcomeRevision(
            FinalizationId.new(),
            RunId.new(),
            1,
            RunStatus.PASSED,
            1,
            NOW,
            correction_ids={},  # type: ignore[arg-type]
        )
