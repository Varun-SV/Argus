"""ATES evidence validation regression coverage."""
from __future__ import annotations

import json

import pytest

import argus.ates.evidence_validation as evidence_validation_module
import argus.ates.finalization as finalization_module
from argus.ates import (
    ActionId,
    ActionOperationId,
    ActionRecord,
    AssertionId,
    AssertionRecord,
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
    recover_revision_one,
    to_json_compatible,
)
from argus.ates.store import _run_directory_key
from tests.ates_test_support import (
    _append_pending,
    _attempt_payload,
    _complete_action_store,
    _crash_after_evidence_manifest,
    _finish_validation_store,
    _open_action_store,
    _open_run_with_id,
    _open_validation_store,
    _payload_validation_store,
)
from tests.test_ates_finalization import _open_run, _run_record_json

def test_unfinished_later_attempt_cannot_finalize_passed(tmp_path):
    store = _open_run(tmp_path)
    first_completed = next(
        e for e in store.events if e.envelope.event_type is EventType.STEP_ATTEMPT_COMPLETED
    )
    first = first_completed.payload["attempt"]
    retry_id = StepAttemptId.new()
    retry_reason = to_json_compatible(EvidenceValue.safe("retry"))
    store.append(
        EventType.STEP_RETRY_SCHEDULED,
        {
            "step_id": first["step_id"],
            "previous_step_attempt_id": first["step_attempt_id"],
            "next_step_attempt_id": str(retry_id),
            "next_attempt": 2,
            "reason": retry_reason,
        },
    )
    store.append(
        EventType.STEP_ATTEMPT_STARTED,
        {
            "attempt": {
                "step_attempt_id": str(retry_id),
                "step_id": first["step_id"],
                "attempt": 2,
                "status": "running",
                "started_at": "2026-08-23T10:00:02+00:00",
                "ended_at": None,
                "retry_reason": retry_reason,
            }
        },
    )
    try:
        with pytest.raises(finalization_module.FinalizationError, match="unfinished"):
            finalize_revision_one(store)
    finally:
        store.close()


def test_completion_without_matching_start_is_rejected(tmp_path):
    store = _open_run(tmp_path)
    first = next(
        e for e in store.events if e.envelope.event_type is EventType.STEP_ATTEMPT_COMPLETED
    ).payload["attempt"]
    store.append(
        EventType.STEP_ATTEMPT_COMPLETED,
        {
            "attempt": {
                "step_attempt_id": str(StepAttemptId.new()),
                "step_id": first["step_id"],
                "attempt": 2,
                "status": "passed",
                "started_at": "2026-08-23T10:00:02+00:00",
                "ended_at": "2026-08-23T10:00:03+00:00",
                "retry_reason": to_json_compatible(EvidenceValue.safe("retry")),
            }
        },
    )
    try:
        with pytest.raises(finalization_module.FinalizationError, match="without a matching start"):
            finalize_revision_one(store)
    finally:
        store.close()


def test_malformed_sequence_tombstone_cannot_finalize_passed(tmp_path):
    store = _open_run(tmp_path, provisional=False)
    store.append(EventType.SEQUENCE_TOMBSTONE, {})
    _append_pending(store)
    try:
        with pytest.raises(FinalizationError, match="SEQUENCE_TOMBSTONE.*reason"):
            finalize_revision_one(store)
    finally:
        store.close()


def test_reasoned_sequence_tombstone_remains_explicit_and_finalizable(tmp_path):
    store = _open_run(tmp_path, provisional=False)
    store.append(
        EventType.SEQUENCE_TOMBSTONE,
        {"reason": "producer intentionally omitted non-core diagnostic detail"},
    )
    _append_pending(store)
    try:
        result = finalize_revision_one(store)
        assert result.outcome.effective_status is RunStatus.PASSED
    finally:
        store.close()


def test_environment_payload_must_match_run_record(tmp_path):
    store = _payload_validation_store(
        tmp_path,
        environment={"environment_type": "container", "isolated": False},
        target={
            "target": to_json_compatible(
                EvidenceValue.redacted("privacy.target_value")
            )
        },
    )
    try:
        with pytest.raises(FinalizationError, match="environment_type contradicts"):
            finalize_revision_one(store)
    finally:
        store.close()


def test_step_extension_cannot_be_certified_or_republished(tmp_path):
    store, step_id, attempt_id = _open_validation_store(
        tmp_path,
        step_extra={"debug_note": "API_TOKEN=plaintext-secret"},
    )
    _finish_validation_store(store, step_id, attempt_id)
    try:
        with pytest.raises(FinalizationError, match="RUN_STARTED step contains unexpected fields"):
            finalize_revision_one(store)
    finally:
        store.close()


def test_run_started_payload_extension_cannot_be_certified(tmp_path):
    store, step_id, attempt_id = _open_validation_store(
        tmp_path,
        run_payload_extra={"debug_note": "API_TOKEN=plaintext-secret"},
    )
    _finish_validation_store(store, step_id, attempt_id)
    try:
        with pytest.raises(
            FinalizationError,
            match="RUN_STARTED payload contains unexpected fields",
        ):
            finalize_revision_one(store)
    finally:
        store.close()


def test_omitted_required_defaults_true_and_failed_assertion_fails_run(tmp_path):
    store, step_id, attempt_id = _open_validation_store(tmp_path)
    assertion = {
        "assertion_id": str(AssertionId.new()),
        "step_id": str(step_id),
        "step_attempt_id": str(attempt_id),
        "kind": "process_running",
        "expected": to_json_compatible(EvidenceValue.safe(True)),
        "result": "failed",
        "method": "deterministic.adapter_observation",
        "observation_id": None,
        "actual": to_json_compatible(EvidenceValue.safe(False)),
        "requirement": None,
    }
    store.append(EventType.ASSERTION_EVALUATED, {"assertion": assertion})
    _finish_validation_store(store, step_id, attempt_id)
    try:
        result = finalize_revision_one(store)
        assert result.outcome.effective_status is RunStatus.FAILED
    finally:
        store.close()


def test_wrong_assertion_step_id_cannot_manufacture_pass(tmp_path):
    store, step_id, attempt_id = _open_action_store(tmp_path, kind="assert")
    wrong_step = StepId.new()
    assertion = AssertionRecord(
        assertion_id=AssertionId.new(),
        step_id=wrong_step,
        step_attempt_id=attempt_id,
        kind="text_visible",
        expected=EvidenceValue.redacted("privacy.assertion_value"),
        result="passed",
        method="deterministic.adapter_observation",
        required=True,
    )
    store.append(
        EventType.ASSERTION_EVALUATED,
        {"assertion": to_json_compatible(assertion)},
    )
    _complete_action_store(store, step_id, attempt_id)
    try:
        with pytest.raises(FinalizationError, match="relationships"):
            finalize_revision_one(store)
    finally:
        store.close()


def test_dispatch_commit_without_terminal_forces_error(tmp_path):
    store, step_id, attempt_id = _open_action_store(tmp_path)
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
    store.append(EventType.ACTION_DISPATCH_COMMITTED, payload)
    _complete_action_store(store, step_id, attempt_id)
    try:
        result = finalize_revision_one(store)
        assert result.outcome.effective_status is RunStatus.ERROR
    finally:
        store.close()


def test_dispatch_commit_requires_exact_policy_validated_json_types(tmp_path):
    store, step_id, attempt_id = _open_action_store(tmp_path)
    action_id = ActionId.new()
    operation_id = ActionOperationId.new()
    validated = ActionRecord(
        action_id=action_id,
        step_id=step_id,
        step_attempt_id=attempt_id,
        action_type="toggle",
        parameters={"enabled": EvidenceValue.safe(True)},
        operation_id=operation_id,
    )
    dispatched = ActionRecord(
        action_id=action_id,
        step_id=step_id,
        step_attempt_id=attempt_id,
        action_type="toggle",
        parameters={"enabled": EvidenceValue.safe(1)},
        operation_id=operation_id,
    )
    store.append(
        EventType.ACTION_PROPOSED,
        {"action": to_json_compatible(validated)},
    )
    store.append(
        EventType.ACTION_POLICY_VALIDATED,
        {"action": to_json_compatible(validated)},
    )
    store.append(
        EventType.ACTION_DISPATCH_COMMITTED,
        {"action": to_json_compatible(dispatched)},
    )
    store.append(
        EventType.ACTION_EXECUTED,
        {
            "action_id": str(action_id),
            "operation_id": str(operation_id),
            "result": "executed",
        },
    )
    _complete_action_store(store, step_id, attempt_id)
    try:
        with pytest.raises(
            FinalizationError,
            match="dispatch commit differs from policy-validated action",
        ):
            finalize_revision_one(store)
    finally:
        store.close()


@pytest.mark.parametrize(
    "mutation",
    ["executed_payload", "unknown_payload", "unknown_error"],
)
def test_terminal_action_payloads_reject_plaintext_extensions(tmp_path, mutation):
    store, step_id, attempt_id = _open_action_store(tmp_path)
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

    if mutation == "executed_payload":
        kind = EventType.ACTION_EXECUTED
        terminal_payload = {
            "action_id": str(action.action_id),
            "operation_id": str(action.operation_id),
            "result": "executed",
            "debug_note": "API_TOKEN=plaintext-secret",
        }
    else:
        kind = EventType.ACTION_OUTCOME_UNKNOWN
        error = to_json_compatible(EvidenceValue.redacted("privacy.error_text"))
        if mutation == "unknown_error":
            error["debug_note"] = "API_TOKEN=plaintext-secret"
        terminal_payload = {
            "action_id": str(action.action_id),
            "operation_id": str(action.operation_id),
            "error": error,
        }
        if mutation == "unknown_payload":
            terminal_payload["debug_note"] = "API_TOKEN=plaintext-secret"

    store.append(kind, terminal_payload)
    _complete_action_store(store, step_id, attempt_id)
    try:
        with pytest.raises(FinalizationError, match="unexpected fields"):
            finalize_revision_one(store)
    finally:
        store.close()


def test_retry_reason_equality_preserves_canonical_json_types():
    assert not evidence_validation_module._canonical_json_equal(
        EvidenceValue.safe({"retry": True}),
        EvidenceValue.safe({"retry": 1}),
    )


def test_scripted_run_cannot_spoof_roam_prelaunch_attempt(tmp_path):
    run_id = RunId.new()
    step_id = StepId.new()
    attempt_id = StepAttemptId.new()
    step = StepRecord(
        step_id=step_id,
        instruction=EvidenceValue.redacted("privacy.authored_text"),
        kind="roam",
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
        EventType.STEP_ATTEMPT_STARTED,
        _attempt_payload(step_id, attempt_id, "running", ended=False),
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
        with pytest.raises(
            FinalizationError,
            match="scripted runs cannot declare roam steps|before TARGET_LAUNCHED",
        ):
            finalize_revision_one(store)
    finally:
        store.close()


@pytest.mark.parametrize(
    "run_id",
    [
        RunId("RUN-Abc123"),
        RunId("RUN-ab_cd123"),
    ],
)
def test_encoded_run_ids_cannot_skip_exact_manifest_preflight(
    tmp_path,
    monkeypatch,
    run_id,
):
    store = _open_run_with_id(tmp_path, run_id)
    _crash_after_evidence_manifest(store, monkeypatch)

    encoded_name = _run_directory_key(run_id)
    assert encoded_name != str(run_id)
    run_dir = tmp_path / ".argus" / "runs" / encoded_name
    raw_run_dir = tmp_path / ".argus" / "runs" / str(run_id)
    assert run_dir.is_dir()
    assert not raw_run_dir.exists()

    manifest = run_dir / "manifests" / "manifest-0001.json"
    package = run_dir / "manifests" / "package-manifest-0001.json"
    binding = run_dir / "run.json"
    evidence = run_dir / "evidence.jsonl"
    before_evidence = evidence.read_bytes()

    parsed = json.loads(manifest.read_text("utf-8"))
    manifest.write_text(
        json.dumps(parsed, indent=2, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(
        FinalizationError,
        match="canonical persisted representation|bytes differ from regenerated candidate",
    ):
        recover_revision_one(tmp_path, run_id)

    assert evidence.read_bytes() == before_evidence
    assert not package.exists()
    assert not binding.exists()
    with AtesEventStore(tmp_path, run_id) as reopened:
        assert not any(
            event.envelope.event_type is EventType.RUN_COMPLETED
            for event in reopened.events
        )
