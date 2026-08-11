import json
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest

from argus.ates import (
    ActionId,
    ActionOperationId,
    ActionRecord,
    AssertionId,
    AssertionRecord,
    AssertionResult,
    EvidenceValue,
    ExecutionKind,
    FinalizationId,
    FrozenDict,
    RequirementIdentity,
    RunId,
    RunOutcomeRevision,
    RunRecord,
    RunStatus,
    ScriptedSource,
    SourceCommitment,
    StepAttemptId,
    StepAttemptRecord,
    StepAttemptStatus,
    StepId,
    effective_outcome,
    to_json_compatible,
    validate_step_evidence_relationships,
)

NOW = datetime(2026, 8, 11, 9, 35, tzinfo=timezone.utc)
CONFIG = SourceCommitment("hmac-sha256", "hmac:config", "ates-config-v1")
SOURCE = SourceCommitment("sha256", "sha256:test", "ates-source-v1")


def test_frozen_dict_has_no_mutable_dict_storage_escape():
    evidence = EvidenceValue.safe({"outer": {"secret_safe": "value"}})
    assert isinstance(evidence.value, FrozenDict)
    nested = evidence.value["outer"]
    assert isinstance(nested, FrozenDict)

    for mutator in (
        lambda: dict.__setitem__(evidence.value, "added", "bad"),
        lambda: dict.update(evidence.value, {"added": "bad"}),
        lambda: dict.clear(evidence.value),
        lambda: dict.__setitem__(nested, "added", "bad"),
    ):
        with pytest.raises(TypeError):
            mutator()

    serialized = to_json_compatible(evidence)
    assert json.loads(json.dumps(serialized))["value"]["outer"]["secret_safe"] == "value"
    serialized["value"]["outer"]["secret_safe"] = "changed"  # type: ignore[index]
    assert evidence.value["outer"]["secret_safe"] == "value"  # type: ignore[index]


def test_effective_outcome_rejects_forged_history_objects():
    run_id = RunId.new()
    first = RunOutcomeRevision(
        FinalizationId.new(), run_id, 1, RunStatus.PASSED, 1, NOW
    )
    forged = SimpleNamespace(
        finalization_id=FinalizationId.new(),
        run_id=run_id,
        revision=2,
        effective_status=RunStatus.FAILED,
        evidence_revision=2,
        supersedes_finalization_id=first.finalization_id,
    )

    with pytest.raises(ValueError, match="validated RunOutcomeRevision"):
        effective_outcome((first, forged))  # type: ignore[arg-type]


def test_assertions_can_bind_an_immutable_requirement_identity():
    step_id = StepId.new()
    attempt_id = StepAttemptId.new()
    requirement_v2 = RequirementIdentity(
        "REQ-42", "product-requirements", version="2",
        source_revision="release-2",
        commitment=SourceCommitment("sha256", "sha256:req-v2"),
    )
    requirement_v3 = RequirementIdentity(
        "REQ-42", "product-requirements", version="3",
        source_revision="release-3",
        commitment=SourceCommitment("sha256", "sha256:req-v3"),
    )

    assertion_v2 = AssertionRecord(
        AssertionId.new(), step_id, attempt_id, "requirement_check",
        EvidenceValue.safe(True), AssertionResult.PASSED, "deterministic",
        requirement=requirement_v2,
    )
    assertion_v3 = AssertionRecord(
        AssertionId.new(), step_id, attempt_id, "requirement_check",
        EvidenceValue.safe(True), AssertionResult.PASSED, "deterministic",
        requirement=requirement_v3,
    )

    assert assertion_v2.requirement.identity_key != assertion_v3.requirement.identity_key
    assert to_json_compatible(assertion_v2)["requirement"]["version"] == "2"  # type: ignore[index]

    with pytest.raises(ValueError, match="RequirementIdentity"):
        AssertionRecord(
            AssertionId.new(), step_id, attempt_id, "requirement_check",
            EvidenceValue.safe(True), AssertionResult.PASSED, "deterministic",
            requirement="REQ-42@v2",  # type: ignore[arg-type]
        )


def test_distinct_actions_cannot_reuse_an_operation_id():
    step_id = StepId.new()
    attempt = StepAttemptRecord(
        StepAttemptId.new(), step_id, 1, StepAttemptStatus.RUNNING, NOW
    )
    operation_id = ActionOperationId.new()
    first = ActionRecord(
        ActionId.new(), step_id, attempt.step_attempt_id, "click", {}, operation_id
    )
    second = ActionRecord(
        ActionId.new(), step_id, attempt.step_attempt_id, "delete", {}, operation_id
    )

    with pytest.raises(ValueError, match="operation IDs must be unique"):
        validate_step_evidence_relationships((attempt,), actions=(first, second))


def test_optional_run_identity_metadata_requires_nonempty_strings():
    source = ScriptedSource("TEST-1", SOURCE)

    RunRecord(
        RunId.new(), ExecutionKind.SCRIPTED, source, NOW, "0.1.0",
        "desktop-gui", "capsule", "standard", CONFIG,
        provider="openai", model_provider="openai", model="gpt-test",
    )

    invalid_cases = (
        {"provider": ["openai"]},
        {"model_provider": {"name": "openai"}},
        {"model": ""},
    )
    for invalid in invalid_cases:
        with pytest.raises(ValueError):
            RunRecord(
                RunId.new(), ExecutionKind.SCRIPTED, source, NOW, "0.1.0",
                "desktop-gui", "capsule", "standard", CONFIG,
                **invalid,  # type: ignore[arg-type]
            )
