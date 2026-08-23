from __future__ import annotations

import json

import pytest

from argus.ates import (
    AtesEventStore,
    EventType,
    RunId,
    RunStatus,
    StepAttemptId,
    StepId,
)
from argus.ates.finalization import (
    FinalizationError,
    FinalizationTrustState,
    finalize_revision_one,
    verify_finalized_run,
)


def _open_run(tmp_path, *, step_status="passed", provisional=True):
    run_id = RunId.new()
    step_id = StepId.new()
    attempt_id = StepAttemptId.new()
    store = AtesEventStore(tmp_path, run_id)
    store.append(
        EventType.RUN_STARTED,
        {
            "run": {"run_id": str(run_id)},
            "steps": [
                {
                    "step_id": str(step_id),
                    "kind": "act",
                    "instruction": {"disposition": "redacted", "value": "<redacted>", "reason": "policy.default"},
                }
            ],
        },
    )
    store.append(EventType.ENVIRONMENT_PREPARED, {"environment_type": "direct", "isolated": False})
    store.append(EventType.TARGET_LAUNCHED, {"target": {"disposition": "redacted", "value": "<redacted>", "reason": "policy.default"}})
    store.append(
        EventType.STEP_ATTEMPT_STARTED,
        {
            "attempt": {
                "step_attempt_id": str(attempt_id),
                "step_id": str(step_id),
                "attempt": 1,
                "status": "running",
                "started_at": "2026-08-23T10:00:00+00:00",
                "ended_at": None,
                "retry_reason": None,
            }
        },
    )
    store.append(
        EventType.STEP_ATTEMPT_COMPLETED,
        {
            "attempt": {
                "step_attempt_id": str(attempt_id),
                "step_id": str(step_id),
                "attempt": 1,
                "status": step_status,
                "started_at": "2026-08-23T10:00:00+00:00",
                "ended_at": "2026-08-23T10:00:01+00:00",
                "retry_reason": None,
            }
        },
    )
    store.append(EventType.TARGET_CLOSED, {})
    store.append(EventType.ENVIRONMENT_RELEASED, {})
    if provisional:
        store.append(
            EventType.RUN_MARKED_INCOMPLETE,
            {"reason": "runtime.finalization_pending", "execution_result": "pass"},
        )
    return store


def test_revision_one_pass_is_bound_only_after_manifests_and_run_json(tmp_path):
    store = _open_run(tmp_path)
    try:
        result = finalize_revision_one(store)
        assert result.outcome.effective_status is RunStatus.PASSED
        assert result.outcome.revision == 1
        assert result.outcome.evidence_revision == 1
        assert result.trust_state is FinalizationTrustState.BOUND_VERIFIED
        assert result.evidence_manifest_path.exists()
        assert result.package_manifest_path.exists()
        assert result.binding_path.exists()

        manifest = json.loads(result.evidence_manifest_path.read_text("utf-8"))
        assert manifest["effective_status"] == "passed"
        assert manifest["evidence"]["final_event_type"] == "RUN_COMPLETED"
        assert manifest["evidence"]["event_count"] == len(store.events)
        assert "manifest_sha256" not in manifest

        package = json.loads(result.package_manifest_path.read_text("utf-8"))
        assert all(member["path"] != "manifests/package-manifest-0001.json" for member in package["members"])

        binding = json.loads(result.binding_path.read_text("utf-8"))
        assert binding["trust_state"] == "bound_verified"
        assert binding["finalization"]["effective_status"] == "passed"
        assert store.events[-1].envelope.event_type is EventType.RUN_COMPLETED
    finally:
        store.close()

    verified = verify_finalized_run(result.run_dir)
    assert verified.outcome.effective_status is RunStatus.PASSED
    assert verified.trust_state is FinalizationTrustState.BOUND_VERIFIED


def test_failed_effective_step_derives_failed_even_if_provisional_legacy_says_pass(tmp_path):
    store = _open_run(tmp_path, step_status="failed")
    try:
        result = finalize_revision_one(store)
        assert result.outcome.effective_status is RunStatus.FAILED
    finally:
        store.close()


def test_action_outcome_unknown_forces_error(tmp_path):
    store = _open_run(tmp_path)
    try:
        # Remove the provisional marker from authority by starting a fresh run
        # shape where ACTION_OUTCOME_UNKNOWN occurs before release/finalization.
        # The marker itself is intentionally only a provisional lifecycle fact.
        store.append(
            EventType.ACTION_OUTCOME_UNKNOWN,
            {"action_id": "ACT-unknown", "operation_id": "OP-unknown", "error": None},
        )
        result = finalize_revision_one(store)
        assert result.outcome.effective_status is RunStatus.ERROR
    finally:
        store.close()


def test_nonprovisional_incomplete_marker_forces_error(tmp_path):
    store = _open_run(tmp_path, provisional=False)
    try:
        store.append(
            EventType.RUN_MARKED_INCOMPLETE,
            {"reason": "runtime.provider_check_failed", "execution_result": "error"},
        )
        result = finalize_revision_one(store)
        assert result.outcome.effective_status is RunStatus.ERROR
    finally:
        store.close()


def test_missing_required_step_attempt_cannot_pass(tmp_path):
    run_id = RunId.new()
    step_id = StepId.new()
    store = AtesEventStore(tmp_path, run_id)
    try:
        store.append(
            EventType.RUN_STARTED,
            {"run": {"run_id": str(run_id)}, "steps": [{"step_id": str(step_id), "kind": "act"}]},
        )
        store.append(EventType.ENVIRONMENT_PREPARED, {"environment_type": "direct", "isolated": False})
        store.append(EventType.ENVIRONMENT_RELEASED, {})
        result = finalize_revision_one(store)
        assert result.outcome.effective_status is RunStatus.ERROR
    finally:
        store.close()


def test_verifier_rejects_evidence_tampering_after_finalization(tmp_path):
    store = _open_run(tmp_path)
    try:
        result = finalize_revision_one(store)
    finally:
        store.close()

    evidence = result.run_dir / "evidence.jsonl"
    evidence.write_bytes(evidence.read_bytes() + b"{}\n")
    with pytest.raises(FinalizationError, match="evidence (size|digest)"):
        verify_finalized_run(result.run_dir)


def test_run_completed_without_binding_is_not_authoritative(tmp_path):
    store = _open_run(tmp_path)
    try:
        result = finalize_revision_one(store)
    finally:
        store.close()

    result.binding_path.unlink()
    with pytest.raises(FinalizationError):
        verify_finalized_run(result.run_dir)


def test_second_revision_one_finalization_is_rejected(tmp_path):
    store = _open_run(tmp_path)
    try:
        finalize_revision_one(store)
        with pytest.raises(FinalizationError, match="already contains RUN_COMPLETED"):
            finalize_revision_one(store)
    finally:
        store.close()
