from __future__ import annotations

import json
import os

import pytest

import argus.ates.finalization as finalization_module
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
    recover_revision_one,
    to_json_compatible,
)
from tests.test_ates_finalization import _open_run, _run_record_json


_STARTED = "2026-08-23T10:00:00+00:00"
_ENDED = "2026-08-23T10:00:01+00:00"


def _attempt_payload(step_id, attempt_id, status, *, ordinal=1, ended=True):
    return {
        "attempt": {
            "step_attempt_id": str(attempt_id),
            "step_id": str(step_id),
            "attempt": ordinal,
            "status": status,
            "started_at": _STARTED,
            "ended_at": _ENDED if ended else None,
            "retry_reason": None,
        }
    }


@pytest.mark.parametrize(
    ("member_name", "package_should_exist"),
    [
        ("manifest-0001.json", False),
        ("package-manifest-0001.json", True),
    ],
)
def test_recovery_rejects_reformatted_existing_member_before_mutation(
    tmp_path, monkeypatch, member_name, package_should_exist
):
    store = _open_run(tmp_path)
    run_id = store.run_id
    real_publish = finalization_module._publish

    def publish_then_crash(directory, name, data):
        path = real_publish(directory, name, data)
        if name == member_name:
            raise RuntimeError(f"forced crash after {member_name}")
        return path

    monkeypatch.setattr(finalization_module, "_publish", publish_then_crash)
    try:
        with pytest.raises(RuntimeError, match="forced crash"):
            finalize_revision_one(store)
    finally:
        store.close()

    run_dir = tmp_path / ".argus" / "runs" / str(run_id)
    manifests = run_dir / "manifests"
    target = manifests / member_name
    assert target.exists()
    assert (manifests / "package-manifest-0001.json").exists() is package_should_exist
    assert not (run_dir / "run.json").exists()

    with AtesEventStore(tmp_path, run_id) as reopened:
        before_count = len(reopened.events)
        assert not any(
            event.envelope.event_type is EventType.RUN_COMPLETED
            for event in reopened.events
        )

    parsed = json.loads(target.read_text("utf-8"))
    target.write_text(
        json.dumps(parsed, indent=2, sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(finalization_module, "_publish", real_publish)

    with pytest.raises(
        FinalizationError,
        match="canonical persisted representation|bytes differ from regenerated candidate",
    ):
        recover_revision_one(tmp_path, run_id)

    assert not (run_dir / "run.json").exists()
    with AtesEventStore(tmp_path, run_id) as reopened:
        assert len(reopened.events) == before_count
        assert not any(
            event.envelope.event_type is EventType.RUN_COMPLETED
            for event in reopened.events
        )
    if member_name == "manifest-0001.json":
        assert not (manifests / "package-manifest-0001.json").exists()


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


@pytest.mark.skipif(os.name == "nt", reason="POSIX durable rollback regression")
def test_postpublication_verification_failure_surfaces_rollback_ambiguity(
    tmp_path, monkeypatch
):
    store = _open_run(tmp_path)
    real_pinned_bytes = finalization_module._pinned_bytes
    real_unlink = os.unlink
    injected_verification_failure = False

    def corrupt_postpublish_read(directory, name, label):
        nonlocal injected_verification_failure
        data = real_pinned_bytes(directory, name, label)
        if name == "manifest-0001.json" and not injected_verification_failure:
            injected_verification_failure = True
            return data + b"x"
        return data

    def fail_final_unlink(path, *args, **kwargs):
        if path == "manifest-0001.json" and kwargs.get("dir_fd") is not None:
            raise OSError("forced rollback unlink failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(
        finalization_module, "_pinned_bytes", corrupt_postpublish_read
    )
    monkeypatch.setattr(os, "unlink", fail_final_unlink)
    try:
        with pytest.raises(
            FinalizationError,
            match="rollback is incomplete or ambiguous",
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
