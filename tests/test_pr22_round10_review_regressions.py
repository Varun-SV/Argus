from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from argus.ates import (
    ApprovalCredential,
    AtesEventStore,
    EventType,
    EvidenceValue,
    FinalizationError,
    RunId,
    RunStatus,
    StepAttemptId,
    StepId,
    StepRecord,
    append_approval,
    finalize_revision_one,
    recover_revision_one,
    render_reports,
    revoke_approval,
    to_json_compatible,
    validate_approvals,
)
from tests.test_ates_finalization import _open_run, _run_record_json
from tests.test_pr22_round3_review_regressions import _attempt_payload
from tests.test_pr22_round9_review_regressions import _finalized_package


def _credential():
    key = b"0123456789abcdef0123456789abcdef"
    credential = ApprovalCredential(
        key_id="round10-reviewer-key",
        key=key,
        actor="reviewer@example.invalid",
        roles=("test_reviewer",),
    )
    resolver = lambda key_id: credential if key_id == credential.key_id else None
    return key, credential, resolver


def test_invalid_superseder_cannot_advance_approval_generation(tmp_path):
    finalization = _finalized_package(tmp_path)
    root = finalization.run_dir
    key, credential, resolver = _credential()

    first = append_approval(
        root,
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
    )

    # Same claimed identity/key_id, but the revoke is signed by the wrong key.
    wrong_key = b"fedcba9876543210fedcba9876543210"
    revoke_approval(
        root,
        first["approval_id"],
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=wrong_key,
    )

    retried = append_approval(
        root,
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
    )
    assert retried["approval_id"] == first["approval_id"]
    assert retried["request_id"] == first["request_id"]

    state = validate_approvals(root, key_resolver=resolver)
    assert [item.record["approval_id"] for item in state.verified_approvals] == [
        first["approval_id"]
    ]
    # The invalid revoke remains visible for audit, but it cannot manufacture a
    # second effective approval generation.
    assert len((root / "approvals.jsonl").read_text("utf-8").splitlines()) == 2


def test_explicit_approval_timestamps_are_distinct_request_identity(tmp_path):
    finalization = _finalized_package(tmp_path)
    root = finalization.run_dir
    key, credential, resolver = _credential()
    first_when = datetime(2026, 8, 28, 1, 2, 3, tzinfo=timezone.utc)
    second_when = datetime(2026, 8, 28, 1, 2, 4, tzinfo=timezone.utc)

    first = append_approval(
        root,
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
        occurred_at=first_when,
    )
    second = append_approval(
        root,
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
        occurred_at=second_when,
    )

    assert second["approval_id"] != first["approval_id"]
    assert second["request_id"] != first["request_id"]
    assert first["occurred_at"] == first_when.isoformat()
    assert second["occurred_at"] == second_when.isoformat()

    retried_second = append_approval(
        root,
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
        occurred_at=second_when,
    )
    assert retried_second["approval_id"] == second["approval_id"]

    state = validate_approvals(root, key_resolver=resolver)
    assert [item.record["approval_id"] for item in state.verified_approvals] == [
        first["approval_id"],
        second["approval_id"],
    ]


def _finalize_failed_missing_close(tmp_path, *, retained: bool):
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
        {"target": to_json_compatible(EvidenceValue.redacted("privacy.target_value"))},
    )
    store.append(
        EventType.STEP_ATTEMPT_STARTED,
        _attempt_payload(step_id, attempt_id, "running", ended=False),
    )
    store.append(
        EventType.STEP_ATTEMPT_COMPLETED,
        _attempt_payload(step_id, attempt_id, "failed"),
    )
    if retained:
        store.append(EventType.FAILURE_CAPSULE_RETAINED, {"retained": True})
    store.append(EventType.ENVIRONMENT_RELEASED, {})
    store.append(
        EventType.RUN_MARKED_INCOMPLETE,
        {"reason": "runtime.finalization_pending", "execution_result": "fail"},
    )
    try:
        return finalize_revision_one(store)
    finally:
        store.close()


def test_missing_target_close_is_error_without_retention_evidence(tmp_path):
    result = _finalize_failed_missing_close(tmp_path, retained=False)
    assert result.outcome.effective_status is RunStatus.ERROR


def test_failure_capsule_retention_evidence_preserves_deterministic_failure(tmp_path):
    result = _finalize_failed_missing_close(tmp_path, retained=True)
    assert result.outcome.effective_status is RunStatus.FAILED


def test_recovery_of_absent_run_is_read_only(tmp_path):
    run_id = RunId.new()
    assert not (tmp_path / ".argus").exists()

    with pytest.raises(FinalizationError, match="absent ATES run"):
        recover_revision_one(tmp_path, run_id)

    assert not (tmp_path / ".argus").exists()


def test_reports_render_read_only_when_detached_ledgers_are_absent(tmp_path):
    store = _open_run(tmp_path)
    try:
        result = finalize_revision_one(store)
    finally:
        store.close()
    root = result.run_dir

    assert not (root / "approvals.jsonl").exists()
    assert not (root / "audit.jsonl").exists()

    bundle = render_reports(root)
    model = json.loads(bundle.json_path.read_text("utf-8"))
    members = {
        item["path"]: item
        for item in model["detached_ledger_snapshot"]["members"]
    }
    assert members["approvals.jsonl"] == {
        "path": "approvals.jsonl",
        "state": "absent",
        "size_bytes": 0,
        "sha256": None,
    }
    assert members["audit.jsonl"] == {
        "path": "audit.jsonl",
        "state": "absent",
        "size_bytes": 0,
        "sha256": None,
    }
    # Rendering a verified run must not initialize detached mutable ledgers.
    assert not (root / "approvals.jsonl").exists()
    assert not (root / "audit.jsonl").exists()
