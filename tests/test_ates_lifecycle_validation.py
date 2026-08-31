"""ATES lifecycle validation regression coverage."""
from __future__ import annotations

import pytest

from argus.ates import (
    ActionId,
    ActionOperationId,
    ActionRecord,
    AtesEventStore,
    EventType,
    EvidenceValue,
    FinalizationError,
    RunId,
    RunStatus,
    StepAttemptId,
    StepId,
    StepRecord,
    finalize_revision_one,
    to_json_compatible,
)
from tests.ates_test_support import (
    _active_action_store,
    _attempt_payload,
    _base_lifecycle_store,
    _complete_action_store,
    _finalize_failed_missing_close,
    _open_action_store,
    _open_validation_store,
    _payload_validation_store,
)
from tests.test_ates_finalization import _open_run, _run_record_json

def test_missing_target_close_is_error_without_retention_evidence(tmp_path):
    result = _finalize_failed_missing_close(tmp_path, retained=False)
    assert result.outcome.effective_status is RunStatus.ERROR


def test_failure_capsule_retention_evidence_preserves_deterministic_failure(tmp_path):
    # A retained target is a legitimate substitute for TARGET_CLOSED only when
    # canonical provenance proves an isolated Capsule can actually preserve it.
    result = _finalize_failed_missing_close(tmp_path, retained=True, capsule=True)
    assert result.outcome.effective_status is RunStatus.FAILED


def test_target_close_while_attempt_active_is_rejected(tmp_path):
    store, step_id, attempt_id = _base_lifecycle_store(tmp_path)
    store.append(EventType.TARGET_CLOSED, {})
    store.append(
        EventType.STEP_ATTEMPT_COMPLETED,
        _attempt_payload(step_id, attempt_id, "passed"),
    )
    store.append(EventType.ENVIRONMENT_RELEASED, {})
    store.append(
        EventType.RUN_MARKED_INCOMPLETE,
        {"reason": "runtime.finalization_pending", "execution_result": "pass"},
    )
    try:
        with pytest.raises(FinalizationError, match="TARGET_CLOSED.*active"):
            finalize_revision_one(store)
    finally:
        store.close()


def test_failure_capsule_does_not_clear_independent_action_lifecycle_error(tmp_path):
    store, step_id, attempt_id = _base_lifecycle_store(tmp_path)
    action = ActionRecord(
        action_id=ActionId.new(),
        step_id=step_id,
        step_attempt_id=attempt_id,
        action_type="click",
        parameters={},
        operation_id=ActionOperationId.new(),
    )
    payload = {"action": to_json_compatible(action)}
    store.append(EventType.ACTION_PROPOSED, payload)
    store.append(EventType.ACTION_POLICY_VALIDATED, payload)
    # Deliberately no ACTION_DISPATCH_COMMITTED: this is an independent
    # lifecycle error and must survive the retained-target compatibility rule.
    store.append(
        EventType.STEP_ATTEMPT_COMPLETED,
        _attempt_payload(step_id, attempt_id, "failed"),
    )
    store.append(EventType.FAILURE_CAPSULE_RETAINED, {"retained": True})
    store.append(EventType.ENVIRONMENT_RELEASED, {})
    store.append(
        EventType.RUN_MARKED_INCOMPLETE,
        {"reason": "runtime.finalization_pending", "execution_result": "fail"},
    )
    try:
        result = finalize_revision_one(store)
        assert result.outcome.effective_status is RunStatus.ERROR
    finally:
        store.close()


def test_dangling_action_proposal_is_an_execution_lifecycle_error(tmp_path):
    store, step_id, attempt_id = _active_action_store(tmp_path)
    action = ActionRecord(
        action_id=ActionId.new(),
        step_id=step_id,
        step_attempt_id=attempt_id,
        action_type="click",
        parameters={},
        operation_id=ActionOperationId.new(),
    )
    store.append(EventType.ACTION_PROPOSED, {"action": to_json_compatible(action)})
    store.append(
        EventType.STEP_ATTEMPT_COMPLETED,
        _attempt_payload(step_id, attempt_id, "passed"),
    )
    store.append(EventType.TARGET_CLOSED, {})
    store.append(EventType.ENVIRONMENT_RELEASED, {})
    store.append(
        EventType.RUN_MARKED_INCOMPLETE,
        {"reason": "runtime.finalization_pending", "execution_result": "pass"},
    )
    try:
        result = finalize_revision_one(store)
        assert result.outcome.effective_status is RunStatus.ERROR
    finally:
        store.close()


@pytest.mark.parametrize(
    "payload",
    [
        {"reason": "runtime.finalization_pending"},
        {"reason": "runtime.finalization_pending", "execution_result": 1},
        {"reason": "runtime.finalization_pending", "execution_result": "paased"},
        {"reason": "runtime.finalization_pending", "execution_result": "failed"},
    ],
)
def test_unknown_provisional_execution_result_is_rejected(tmp_path, payload):
    store = _open_run(tmp_path, provisional=False)
    store.append(EventType.RUN_MARKED_INCOMPLETE, payload)
    try:
        with pytest.raises(FinalizationError, match="execution_result"):
            finalize_revision_one(store)
    finally:
        store.close()


def test_target_launch_requires_privacy_classified_evidence(tmp_path):
    store = _payload_validation_store(
        tmp_path,
        environment={"environment_type": "direct", "isolated": False},
        target={"target": "password=plaintext-secret"},
    )
    try:
        with pytest.raises(FinalizationError, match="privacy-classified"):
            finalize_revision_one(store)
    finally:
        store.close()


@pytest.mark.parametrize(
    "payload",
    [
        {
            "reason": "customer password was visible in the crash dialog",
            "execution_result": "error",
        },
        {
            "reason": "runtime.execution_interrupted",
            "execution_result": "error",
            "detail": "secret-bearing extension",
        },
    ],
)
def test_nonprovisional_incomplete_marker_cannot_certify_free_text_or_extensions(
    tmp_path, payload
):
    store = _open_run(tmp_path, provisional=False)
    store.append(EventType.RUN_MARKED_INCOMPLETE, payload)
    try:
        with pytest.raises(FinalizationError, match="RUN_MARKED_INCOMPLETE"):
            finalize_revision_one(store)
    finally:
        store.close()


def test_non_capsule_retention_cannot_excuse_missing_target_close(tmp_path):
    store, step_id, attempt_id = _open_validation_store(tmp_path)
    store.append(
        EventType.STEP_ATTEMPT_COMPLETED,
        _attempt_payload(step_id, attempt_id, "failed"),
    )
    store.append(EventType.FAILURE_CAPSULE_RETAINED, {"retained": True})
    store.append(EventType.ENVIRONMENT_RELEASED, {})
    store.append(
        EventType.RUN_MARKED_INCOMPLETE,
        {"reason": "runtime.finalization_pending", "execution_result": "fail"},
    )
    try:
        result = finalize_revision_one(store)
        assert result.outcome.effective_status is RunStatus.ERROR
    finally:
        store.close()


def test_incomplete_target_lifecycle_cannot_pass(tmp_path):
    store, step_id, attempt_id = _open_action_store(tmp_path)
    _complete_action_store(store, step_id, attempt_id, close=False)
    try:
        result = finalize_revision_one(store)
        assert result.outcome.effective_status is RunStatus.ERROR
    finally:
        store.close()


def test_misordered_target_close_is_rejected(tmp_path):
    run_id = RunId.new()
    step_id = StepId.new()
    store = AtesEventStore(tmp_path, run_id)
    step = StepRecord(
        step_id=step_id,
        instruction=EvidenceValue.redacted("privacy.authored_text"),
        kind="act",
    )
    store.append(
        EventType.RUN_STARTED,
        {
            "run": _run_record_json(run_id),
            "steps": [to_json_compatible(step)],
        },
    )
    store.append(
        EventType.ENVIRONMENT_PREPARED,
        {"environment_type": "direct", "isolated": False},
    )
    store.append(EventType.TARGET_CLOSED, {})
    try:
        with pytest.raises(FinalizationError, match="target close lifecycle"):
            finalize_revision_one(store)
    finally:
        store.close()


def test_action_terminal_after_target_close_is_rejected(tmp_path):
    run_id = RunId.new()
    step_id = StepId.new()
    attempt_id = StepAttemptId.new()
    step = StepRecord(
        step_id=step_id,
        instruction=EvidenceValue.redacted("privacy.authored_text"),
        kind="act",
    )
    store = AtesEventStore(tmp_path, run_id)
    store.append(
        EventType.RUN_STARTED,
        {
            "run": _run_record_json(run_id),
            "steps": [to_json_compatible(step)],
        },
    )
    store.append(
        EventType.ENVIRONMENT_PREPARED,
        {"environment_type": "direct", "isolated": False},
    )
    store.append(
        EventType.TARGET_LAUNCHED,
        {
            "target": to_json_compatible(
                EvidenceValue.redacted("privacy.target_value")
            )
        },
    )
    store.append(
        EventType.STEP_ATTEMPT_STARTED,
        _attempt_payload(step_id, attempt_id, "running", ended=False),
    )
    action = ActionRecord(
        action_id=ActionId.new(),
        step_id=step_id,
        step_attempt_id=attempt_id,
        action_type="click",
        parameters={},
        operation_id=ActionOperationId.new(),
    )
    action_payload = {"action": to_json_compatible(action)}
    store.append(EventType.ACTION_PROPOSED, action_payload)
    store.append(EventType.ACTION_POLICY_VALIDATED, action_payload)
    store.append(EventType.ACTION_DISPATCH_COMMITTED, action_payload)
    store.append(EventType.TARGET_CLOSED, {})
    store.append(
        EventType.ACTION_EXECUTED,
        {
            "action_id": str(action.action_id),
            "operation_id": str(action.operation_id),
            "result": "executed",
        },
    )
    store.append(
        EventType.STEP_ATTEMPT_COMPLETED,
        _attempt_payload(step_id, attempt_id, "passed"),
    )
    store.append(EventType.ENVIRONMENT_RELEASED, {})
    store.append(
        EventType.RUN_MARKED_INCOMPLETE,
        {"reason": "runtime.finalization_pending", "execution_result": "pass"},
    )
    try:
        with pytest.raises(
            FinalizationError,
            match="action terminal event occurred outside an active target lifecycle",
        ):
            finalize_revision_one(store)
    finally:
        store.close()


def test_preserved_target_does_not_clear_independent_attempt_error(tmp_path):
    run_id = RunId.new()
    failed_step = StepId.new()
    error_step = StepId.new()
    failed_attempt = StepAttemptId.new()
    error_attempt = StepAttemptId.new()
    steps = [
        StepRecord(
            step_id=failed_step,
            instruction=EvidenceValue.redacted("privacy.authored_text"),
            kind="act",
        ),
        StepRecord(
            step_id=error_step,
            instruction=EvidenceValue.redacted("privacy.authored_text"),
            kind="act",
        ),
    ]
    store = AtesEventStore(tmp_path, run_id)
    store.append(
        EventType.RUN_STARTED,
        {
            "run": _run_record_json(run_id),
            "steps": [to_json_compatible(step) for step in steps],
        },
    )
    store.append(
        EventType.ENVIRONMENT_PREPARED,
        {"environment_type": "direct", "isolated": False},
    )
    store.append(
        EventType.TARGET_LAUNCHED,
        {
            "target": to_json_compatible(
                EvidenceValue.redacted("privacy.target_value")
            )
        },
    )
    store.append(
        EventType.STEP_ATTEMPT_STARTED,
        _attempt_payload(failed_step, failed_attempt, "running", ended=False),
    )
    store.append(
        EventType.STEP_ATTEMPT_COMPLETED,
        _attempt_payload(failed_step, failed_attempt, "failed"),
    )
    store.append(
        EventType.STEP_ATTEMPT_STARTED,
        _attempt_payload(error_step, error_attempt, "running", ended=False),
    )
    store.append(
        EventType.STEP_ATTEMPT_COMPLETED,
        _attempt_payload(error_step, error_attempt, "error"),
    )
    # Failure Capsule retention intentionally preserves the live target, so no
    # TARGET_CLOSED is emitted before release/finalization handoff.
    store.append(EventType.ENVIRONMENT_RELEASED, {})
    store.append(
        EventType.RUN_MARKED_INCOMPLETE,
        {"reason": "runtime.finalization_pending", "execution_result": "fail"},
    )
    try:
        result = finalize_revision_one(store)
        assert result.outcome.effective_status is RunStatus.ERROR
    finally:
        store.close()
