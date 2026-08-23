from __future__ import annotations

import json
from pathlib import Path

import pytest

import argus.ates.finalization as finalization_module
from argus.ates import (
    ArtifactContext,
    AtesAppendError,
    AtesArtifactRepository,
    AtesEventStore,
    EventType,
    EvidenceValue,
    StepAttemptId,
    finalize_revision_one,
    recover_revision_one,
    to_json_compatible,
    verify_finalized_run,
)
from argus.engine.roam import roam
from argus.engine.runner import run_test
from argus.engine.spec import parse_spec
from argus.tokens import Budget
from tests.conftest import FakeAdapter, FakeProvider
from tests.test_ates_finalization import _open_run


def _run_json_dirs(tmp_path: Path):
    return list((tmp_path / ".argus" / "runs").glob("RUN-*/run.json"))


def _open_run_with_protected_artifact(tmp_path):
    store = _open_run(tmp_path)
    repository = AtesArtifactRepository(store)
    captured = repository.capture_bytes(
        b"protected screenshot bytes",
        context=ArtifactContext.FAILURE_SCREENSHOT,
        kind="screenshot",
        media_type="image/png",
    )
    assert captured.record is not None
    record = captured.record
    store.append(
        EventType.CHECKPOINT_CAPTURED,
        {
            "artifact": to_json_compatible(record),
            "context": ArtifactContext.FAILURE_SCREENSHOT.value,
            "step_attempt_id": None,
        },
    )
    return store, record


def test_run_json_status_edit_cannot_upgrade_failed_run(tmp_path):
    store = _open_run(tmp_path, step_status="failed")
    try:
        result = finalize_revision_one(store)
    finally:
        store.close()
    binding = json.loads(result.binding_path.read_text("utf-8"))
    binding["finalization"]["effective_status"] = "passed"
    result.binding_path.write_text(json.dumps(binding), encoding="utf-8")
    with pytest.raises(finalization_module.FinalizationError):
        verify_finalized_run(result.run_dir)


@pytest.mark.parametrize("mutation", ["delete", "tamper"])
def test_verifier_rechecks_retained_artifact_bytes(tmp_path, mutation):
    store, record = _open_run_with_protected_artifact(tmp_path)
    try:
        result = finalize_revision_one(store)
    finally:
        store.close()
    artifact_path = result.run_dir / record.path
    if mutation == "delete":
        artifact_path.unlink()
    else:
        artifact_path.write_bytes(b"tampered screenshot bytes")
    with pytest.raises(finalization_module.FinalizationError):
        verify_finalized_run(result.run_dir)


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


def test_recovery_after_evidence_manifest_only(tmp_path, monkeypatch):
    store = _open_run(tmp_path)
    run_id = store.run_id
    real_publish = finalization_module._publish

    def fail_package(directory, name, data):
        if name == "package-manifest-0001.json":
            raise RuntimeError("forced crash after evidence manifest")
        return real_publish(directory, name, data)

    monkeypatch.setattr(finalization_module, "_publish", fail_package)
    try:
        with pytest.raises(RuntimeError, match="forced crash"):
            finalize_revision_one(store)
    finally:
        store.close()
    monkeypatch.setattr(finalization_module, "_publish", real_publish)
    recovered = recover_revision_one(tmp_path, run_id)
    assert recovered.binding_path.exists()
    assert recovered.outcome.revision == 1


def test_recovery_after_package_before_completion(tmp_path, monkeypatch):
    store = _open_run(tmp_path)
    run_id = store.run_id

    def crash_before_completion(_event):
        raise RuntimeError("forced crash before completion")

    monkeypatch.setattr(store, "append_event", crash_before_completion)
    try:
        with pytest.raises(RuntimeError, match="forced crash"):
            finalize_revision_one(store)
    finally:
        store.close()
    recovered = recover_revision_one(tmp_path, run_id)
    assert recovered.binding_path.exists()
    assert recovered.outcome.revision == 1


def test_recovery_reconciles_durable_ambiguous_completion(tmp_path, monkeypatch):
    store = _open_run(tmp_path)
    run_id = store.run_id
    real_append = store.append_event

    def durable_then_ambiguous(event):
        real_append(event)
        raise AtesAppendError("forced ambiguous durable completion", event)

    monkeypatch.setattr(store, "append_event", durable_then_ambiguous)
    try:
        with pytest.raises(AtesAppendError):
            finalize_revision_one(store)
    finally:
        store.close()
    recovered = recover_revision_one(tmp_path, run_id)
    assert recovered.binding_path.exists()
    with AtesEventStore(tmp_path, run_id) as reopened:
        assert sum(e.envelope.event_type is EventType.RUN_COMPLETED for e in reopened.events) == 1


def test_recovery_after_completion_before_binding(tmp_path, monkeypatch):
    store = _open_run(tmp_path)
    run_id = store.run_id
    real_publish = finalization_module._publish

    def fail_binding(directory, name, data):
        if name == "run.json":
            raise RuntimeError("forced crash before binding")
        return real_publish(directory, name, data)

    monkeypatch.setattr(finalization_module, "_publish", fail_binding)
    try:
        with pytest.raises(RuntimeError, match="forced crash"):
            finalize_revision_one(store)
    finally:
        store.close()
    monkeypatch.setattr(finalization_module, "_publish", real_publish)
    recovered = recover_revision_one(tmp_path, run_id)
    assert recovered.binding_path.exists()


def test_recovery_is_idempotent_after_binding(tmp_path):
    store = _open_run(tmp_path)
    run_id = store.run_id
    try:
        first = finalize_revision_one(store)
    finally:
        store.close()
    second = recover_revision_one(tmp_path, run_id)
    assert second.outcome.finalization_id == first.outcome.finalization_id
    assert second.outcome.effective_status == first.outcome.effective_status


def test_run_test_publishes_bound_revision_one_and_authoritative_status(tmp_path):
    spec = parse_spec(
        """\
name: finalization integration
target: {adapter: desktop-gui, launch: fake.exe}
steps:
  - "finish"
"""
    )
    provider = FakeProvider([json.dumps({"action": "done", "success": True})])
    result = run_test(spec, provider, FakeAdapter(), project_dir=tmp_path)
    assert result.status == "pass"
    assert result.ates_effective_status == "passed"
    bindings = _run_json_dirs(tmp_path)
    assert len(bindings) == 1
    verified = verify_finalized_run(bindings[0].parent)
    assert verified.outcome.effective_status.value == "passed"


def test_roam_publishes_bound_revision_one_and_authoritative_status(tmp_path):
    session_dir = tmp_path / ".argus" / "roam-review"
    session_dir.mkdir(parents=True)
    session = roam(
        target="fake.exe",
        provider=FakeProvider([]),
        adapter=FakeAdapter(),
        budget=Budget(max_tokens=1000),
        session_dir=session_dir,
        project_dir=tmp_path,
        stop_flag=lambda: True,
        generate_regressions=False,
    )
    assert session.execution_status == "cancelled"
    assert session.ates_effective_status == "cancelled"
    bindings = _run_json_dirs(tmp_path)
    assert len(bindings) == 1
    verified = verify_finalized_run(bindings[0].parent)
    assert verified.outcome.effective_status.value == "cancelled"
