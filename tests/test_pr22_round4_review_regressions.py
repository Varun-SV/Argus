from __future__ import annotations

import json

import pytest

import argus.ates.finalization as finalization_module
from argus.ates import (
    AtesEventStore,
    EventType,
    FinalizationError,
    RunId,
    StepAttemptId,
    StepId,
    finalize_revision_one,
    recover_revision_one,
)
from argus.ates.store import _run_directory_key
from tests.test_ates_finalization import _run_record_json


_STARTED = "2026-08-23T10:00:00+00:00"
_ENDED = "2026-08-23T10:00:01+00:00"


def _open_run_with_id(tmp_path, run_id: RunId) -> AtesEventStore:
    step_id = StepId.new()
    attempt_id = StepAttemptId.new()
    store = AtesEventStore(tmp_path, run_id)
    store.append(
        EventType.RUN_STARTED,
        {
            "run": _run_record_json(run_id),
            "steps": [
                {
                    "step_id": str(step_id),
                    "kind": "act",
                    "instruction": {
                        "disposition": "redacted",
                        "value": "<redacted>",
                        "reason": "policy.default",
                    },
                }
            ],
        },
    )
    store.append(
        EventType.ENVIRONMENT_PREPARED,
        {"environment_type": "direct", "isolated": False},
    )
    store.append(
        EventType.TARGET_LAUNCHED,
        {
            "target": {
                "disposition": "redacted",
                "value": "<redacted>",
                "reason": "policy.default",
            }
        },
    )
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
    store.append(
        EventType.STEP_ATTEMPT_COMPLETED,
        {
            "attempt": {
                "step_attempt_id": str(attempt_id),
                "step_id": str(step_id),
                "attempt": 1,
                "status": "passed",
                "started_at": _STARTED,
                "ended_at": _ENDED,
                "retry_reason": None,
            }
        },
    )
    store.append(EventType.TARGET_CLOSED, {})
    store.append(EventType.ENVIRONMENT_RELEASED, {})
    store.append(
        EventType.RUN_MARKED_INCOMPLETE,
        {"reason": "runtime.finalization_pending", "execution_result": "pass"},
    )
    return store


def _crash_after_evidence_manifest(store, monkeypatch):
    real_publish = finalization_module._publish

    def publish_then_crash(directory, name, data):
        path = real_publish(directory, name, data)
        if name == "manifest-0001.json":
            raise RuntimeError("forced crash after evidence manifest")
        return path

    monkeypatch.setattr(finalization_module, "_publish", publish_then_crash)
    try:
        with pytest.raises(RuntimeError, match="forced crash"):
            finalize_revision_one(store)
    finally:
        store.close()
    monkeypatch.setattr(finalization_module, "_publish", real_publish)


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


def test_recovery_repairs_only_partial_completion_tail_before_reconciling(
    tmp_path,
    monkeypatch,
):
    run_id = RunId.new()
    store = _open_run_with_id(tmp_path, run_id)
    _crash_after_evidence_manifest(store, monkeypatch)

    run_dir = tmp_path / ".argus" / "runs" / _run_directory_key(run_id)
    manifest_path = run_dir / "manifests" / "manifest-0001.json"
    evidence_path = run_dir / "evidence.jsonl"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    _outcome, completion = finalization_module._candidate_from_manifest(
        manifest,
        run_id,
    )

    canonical_before = evidence_path.read_bytes()
    completion_line = completion.canonical_line()
    partial = completion_line[: max(1, len(completion_line) // 2)]
    assert not partial.endswith(b"\n")
    with evidence_path.open("ab") as handle:
        handle.write(partial)
        handle.flush()

    assert evidence_path.read_bytes() == canonical_before + partial

    recovered = recover_revision_one(tmp_path, run_id)
    assert recovered.binding_path.exists()
    assert recovered.package_manifest_path.exists()
    assert evidence_path.read_bytes() == canonical_before + completion_line

    with AtesEventStore(tmp_path, run_id) as reopened:
        completed = [
            event
            for event in reopened.events
            if event.envelope.event_type is EventType.RUN_COMPLETED
        ]
        assert len(completed) == 1
        assert completed[0].canonical_line() == completion_line
