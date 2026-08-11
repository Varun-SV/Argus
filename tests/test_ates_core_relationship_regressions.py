from datetime import datetime, timezone

import pytest

from argus.ates import (
    ActionId,
    ActionRecord,
    AssertionId,
    AssertionRecord,
    AssertionResult,
    EvidenceDisposition,
    EvidenceValue,
    ObservationId,
    ObservationRecord,
    StepAttemptId,
    StepAttemptRecord,
    StepAttemptStatus,
    StepId,
    validate_step_evidence_relationships,
)

NOW = datetime(2026, 8, 11, 9, 0, tzinfo=timezone.utc)


class SplitViewMapping(dict):
    """Expose validated items that differ from the mapping's backing entries."""

    def __init__(self, visible_value: EvidenceValue, hidden_value: object) -> None:
        super().__init__(payload=hidden_value)
        self._visible_value = visible_value

    def items(self):
        return {"payload": self._visible_value}.items()


def test_action_and_observation_validate_the_same_mapping_snapshot_they_store():
    safe_value = EvidenceValue.redacted("secret_input")
    malicious_action_mapping = SplitViewMapping(safe_value, "plaintext-secret")
    action = ActionRecord(
        ActionId.new(),
        StepId.new(),
        StepAttemptId.new(),
        "type",
        malicious_action_mapping,
    )
    assert action.parameters["payload"] is safe_value
    assert action.parameters["payload"].disposition is EvidenceDisposition.REDACTED

    malicious_observation_mapping = SplitViewMapping(safe_value, "plaintext-secret")
    observation = ObservationRecord(
        ObservationId.new(),
        StepAttemptId.new(),
        "screen",
        NOW,
        "capture-standard-v1",
        malicious_observation_mapping,
    )
    assert observation.facts["payload"] is safe_value
    assert observation.facts["payload"].disposition is EvidenceDisposition.REDACTED


def test_step_evidence_relationships_reject_cross_step_action_and_assertion_references():
    step_a = StepId.new()
    step_b = StepId.new()
    attempt_a = StepAttemptRecord(
        StepAttemptId.new(), step_a, 1, StepAttemptStatus.PASSED, NOW, NOW
    )

    mismatched_action = ActionRecord(
        ActionId.new(), step_b, attempt_a.step_attempt_id, "click", {}
    )
    with pytest.raises(ValueError, match="action step_id"):
        validate_step_evidence_relationships(
            (attempt_a,), actions=(mismatched_action,)
        )

    mismatched_assertion = AssertionRecord(
        AssertionId.new(),
        step_b,
        attempt_a.step_attempt_id,
        "text_visible",
        EvidenceValue.safe("Ready"),
        AssertionResult.PASSED,
        "deterministic",
    )
    with pytest.raises(ValueError, match="assertion step_id"):
        validate_step_evidence_relationships(
            (attempt_a,), assertions=(mismatched_assertion,)
        )


def test_step_evidence_relationships_validate_observation_ownership_and_snapshot_inputs():
    step_a = StepId.new()
    step_b = StepId.new()
    attempt_a = StepAttemptRecord(
        StepAttemptId.new(), step_a, 1, StepAttemptStatus.PASSED, NOW, NOW
    )
    attempt_b = StepAttemptRecord(
        StepAttemptId.new(), step_b, 1, StepAttemptStatus.PASSED, NOW, NOW
    )
    observation_b = ObservationRecord(
        ObservationId.new(),
        attempt_b.step_attempt_id,
        "screen",
        NOW,
        "capture-standard-v1",
        {"ready": EvidenceValue.safe(True)},
    )
    assertion_a = AssertionRecord(
        AssertionId.new(),
        step_a,
        attempt_a.step_attempt_id,
        "text_visible",
        EvidenceValue.safe("Ready"),
        AssertionResult.PASSED,
        "deterministic",
        observation_id=observation_b.observation_id,
    )
    with pytest.raises(ValueError, match="same step attempt"):
        validate_step_evidence_relationships(
            (attempt_a, attempt_b),
            observations=(observation_b,),
            assertions=(assertion_a,),
        )

    action_a = ActionRecord(
        ActionId.new(), step_a, attempt_a.step_attempt_id, "click", {}
    )
    observation_a = ObservationRecord(
        ObservationId.new(),
        attempt_a.step_attempt_id,
        "screen",
        NOW,
        "capture-standard-v1",
        {"ready": EvidenceValue.safe(True)},
    )
    assertion_a = AssertionRecord(
        AssertionId.new(),
        step_a,
        attempt_a.step_attempt_id,
        "text_visible",
        EvidenceValue.safe("Ready"),
        AssertionResult.PASSED,
        "deterministic",
        observation_id=observation_a.observation_id,
    )
    attempts = [attempt_a]
    actions = [action_a]
    observations = [observation_a]
    assertions = [assertion_a]
    snapshots = validate_step_evidence_relationships(
        attempts,
        actions=actions,
        observations=observations,
        assertions=assertions,
    )
    attempts.clear()
    actions.clear()
    observations.clear()
    assertions.clear()
    assert snapshots == (
        (attempt_a,),
        (action_a,),
        (observation_a,),
        (assertion_a,),
    )
