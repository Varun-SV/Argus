from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

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
    to_json_compatible,
    verify_finalized_run,
)
from argus.engine.roam import roam
from argus.engine.runner import run_test
from argus.engine.spec import parse_spec
from argus.tokens import Budget
from tests.conftest import FakeAdapter, FakeProvider
from tests.test_ates_finalization import _open_run, _run_record_json


_STARTED = "2026-08-23T10:00:00+00:00"
_ENDED = "2026-08-23T10:00:01+00:00"


def _instruction():
    return to_json_compatible(EvidenceValue.redacted("privacy.authored_text"))


def _target():
    return to_json_compatible(EvidenceValue.redacted("privacy.target_value"))


def _new_store(tmp_path, *, kind="act"):
    run_id = RunId.new()
    step_id = StepId.new()
    attempt_id = StepAttemptId.new()
    store = AtesEventStore(tmp_path, run_id)
    step = StepRecord(
        step_id=step_id,
        instruction=EvidenceValue.redacted("privacy.authored_text"),
        kind=kind,
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
    store.append(EventType.TARGET_LAUNCHED, {"target": _target()})
    store.append(
        EventType.STEP_ATTEMPT_STARTED,
        {
            "attempt": {
                "step_attempt_id": str(attempt_id),
                "step_id": str(step_id),
                "attempt": 1,
                "status": "running",
                "started_at": _STARTED,
                "ended_at": None,
                "retry_reason": None,
            }
        },
    )
    return store, step_id, attempt_id


def _complete(store, step_id, attempt_id, *, status="passed", close=True):
    store.append(
        EventType.STEP_ATTEMPT_COMPLETED,
        {
            "attempt": {
                "step_attempt_id": str(attempt_id),
                "step_id": str(step_id),
                "attempt": 1,
                "status": status,
                "started_at": _STARTED,
                "ended_at": _ENDED,
                "retry_reason": None,
            }
        },
    )
    if close:
        store.append(EventType.TARGET_CLOSED, {})
    store.append(EventType.ENVIRONMENT_RELEASED, {})
    store.append(
        EventType.RUN_MARKED_INCOMPLETE,
        {"reason": "runtime.finalization_pending", "execution_result": "pass"},
    )


def test_wrong_assertion_step_id_cannot_manufacture_pass(tmp_path):
    store, step_id, attempt_id = _new_store(tmp_path, kind="assert")
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
    _complete(store, step_id, attempt_id)
    try:
        with pytest.raises(FinalizationError, match="relationships"):
            finalize_revision_one(store)
    finally:
        store.close()


def test_dispatch_commit_without_terminal_forces_error(tmp_path):
    store, step_id, attempt_id = _new_store(tmp_path)
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
    _complete(store, step_id, attempt_id)
    try:
        result = finalize_revision_one(store)
        assert result.outcome.effective_status is RunStatus.ERROR
    finally:
        store.close()


def test_incomplete_target_lifecycle_cannot_pass(tmp_path):
    store, step_id, attempt_id = _new_store(tmp_path)
    _complete(store, step_id, attempt_id, close=False)
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


def test_pretty_printed_manifest_is_not_bound_verified(tmp_path):
    store = _open_run(tmp_path)
    try:
        result = finalize_revision_one(store)
    finally:
        store.close()

    manifest = json.loads(result.evidence_manifest_path.read_text("utf-8"))
    result.evidence_manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=False),
        encoding="utf-8",
    )
    with pytest.raises(FinalizationError, match="canonical persisted representation|digest"):
        verify_finalized_run(result.run_dir)


@pytest.mark.skipif(os.name == "nt", reason="POSIX hard-link substitution regression")
def test_publisher_rejects_hardlinked_final_member(tmp_path, monkeypatch):
    store = _open_run(tmp_path)
    real_publish = finalization_module._old._publish_no_overwrite

    def hardlinked_publish(directory, name, data):
        if name != "manifest-0001.json":
            return real_publish(directory, name, data)
        attacker = directory.path / ".attacker-source"
        attacker.write_bytes(data)
        os.link(attacker, directory.path / name)
        directory.fsync()
        return directory.path / name

    monkeypatch.setattr(
        finalization_module._old,
        "_publish_no_overwrite",
        hardlinked_publish,
    )
    try:
        with pytest.raises(FinalizationError):
            finalize_revision_one(store)
    finally:
        store.close()


@pytest.mark.skipif(os.name == "nt", reason="Windows pinned handle blocks directory replacement")
def test_publisher_rejects_manifests_directory_replacement(tmp_path, monkeypatch):
    store = _open_run(tmp_path)
    real_publish = finalization_module._old._publish_no_overwrite
    replaced = False

    def replacing_publish(directory, name, data):
        nonlocal replaced
        path = real_publish(directory, name, data)
        if name == "manifest-0001.json" and not replaced:
            replaced = True
            original = directory.path
            moved = original.with_name("manifests-displaced")
            os.rename(original, moved)
            original.mkdir()
        return path

    monkeypatch.setattr(
        finalization_module._old,
        "_publish_no_overwrite",
        replacing_publish,
    )
    try:
        with pytest.raises(FinalizationError, match="namespace"):
            finalize_revision_one(store)
    finally:
        store.close()


def test_run_test_preserves_positional_project_dir(tmp_path):
    spec = parse_spec(
        """\
name: positional runner project root
target: {adapter: desktop-gui, launch: fake.exe}
steps:
  - "finish"
"""
    )
    provider = FakeProvider([json.dumps({"action": "done", "success": True})])
    result = run_test(
        spec,
        provider,
        FakeAdapter(),
        None,
        None,
        None,
        None,
        None,
        tmp_path,
    )
    assert result.status == "pass"
    assert result.ates_effective_status == "passed"
    assert list((tmp_path / ".argus" / "runs").glob("RUN-*/run.json"))


def test_roam_preserves_positional_project_dir(tmp_path):
    session_dir = tmp_path / ".argus" / "roam-positional"
    session_dir.mkdir(parents=True)
    session = roam(
        "fake.exe",
        FakeProvider([]),
        FakeAdapter(),
        Budget(max_tokens=1000),
        session_dir,
        None,
        lambda: True,
        False,
        None,
        None,
        tmp_path,
    )
    assert session.execution_status == "cancelled"
    assert session.ates_effective_status == "cancelled"
    assert list((tmp_path / ".argus" / "runs").glob("RUN-*/run.json"))
