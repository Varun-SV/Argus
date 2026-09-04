"""Shared ATES package, ledger, capture, and lifecycle test builders."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import argus.ates.audit as audit_module
import argus.ates.finalization as finalization_module
from argus.ates import (
    ARTIFACT_POLICY_VERSION,
    ApprovalCredential,
    ApprovalError,
    ArtifactContext,
    ArtifactId,
    AssertionId,
    AssertionRecord,
    AtesArtifactRepository,
    AtesEventStore,
    EventType,
    EvidenceValue,
    FinalizationTrustState,
    RequirementIdentity,
    RunId,
    StepAttemptId,
    StepId,
    StepRecord,
    complete_run_package,
    finalize_revision_one,
    recover_revision_one,
    to_json_compatible,
)
from tests.test_ates_finalization import _open_run, _run_record_json

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


def _approval_credential():
    key = b"0123456789abcdef0123456789abcdef"
    credential = ApprovalCredential(
        key_id="test-reviewer-key",
        key=key,
        actor="reviewer@example.invalid",
        roles=("test_reviewer",),
    )
    resolver = lambda key_id: credential if key_id == credential.key_id else None
    return key, credential, resolver


def _finalize_failed_missing_close(tmp_path, *, retained: bool, capsule: bool = False):
    run_id = RunId.new()
    step_id = StepId.new()
    attempt_id = StepAttemptId.new()
    step = StepRecord(
        step_id=step_id,
        instruction=EvidenceValue.redacted("privacy.authored_text"),
        kind="act",
    )
    run_record = _run_record_json(run_id)
    if capsule:
        run_record["environment_type"] = "capsule"
    store = AtesEventStore(tmp_path, run_id)
    store.append(
        EventType.RUN_STARTED,
        {
            "run": run_record,
            "steps": [to_json_compatible(step)],
        },
    )
    store.append(
        EventType.ENVIRONMENT_PREPARED,
        {
            "environment_type": "capsule" if capsule else "direct",
            "isolated": capsule,
        },
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


_REPORT_FILES = (
    "report.json",
    "report.md",
    "report.html",
    "junit.xml",
    "report-manifest-0001.json",
)


def _append_pending(store) -> None:
    store.append(
        EventType.RUN_MARKED_INCOMPLETE,
        {"reason": "runtime.finalization_pending", "execution_result": "pass"},
    )


def _finalized_root(tmp_path: Path) -> Path:
    store = _open_run(tmp_path)
    try:
        result = finalize_revision_one(store)
    finally:
        store.close()
    return result.run_dir


def _bundle_bytes(root: Path) -> dict[str, bytes]:
    report_dir = root / "reports"
    return {name: (report_dir / name).read_bytes() for name in _REPORT_FILES}


def _assert_no_transaction_residue(root: Path) -> None:
    names = [item.name for item in (root / "reports").iterdir()]
    assert not any(".stage-" in name for name in names)
    assert not any(".backup-" in name for name in names)
    assert not any(".failed-" in name for name in names)


def _base_lifecycle_store(tmp_path):
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
        {"run": _run_record_json(run_id), "steps": [to_json_compatible(step)]},
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
    return store, step_id, attempt_id


def _captured_protected_artifact(store):
    repository = AtesArtifactRepository(store)
    captured = repository.capture_bytes(
        b"round-12 protected screenshot bytes",
        context=ArtifactContext.FAILURE_SCREENSHOT,
        kind="screenshot",
        media_type="image/png",
    )
    assert captured.record is not None
    return captured.record


def _canonical_lines(records) -> bytes:
    return b"".join(
        (
            json.dumps(
                record,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")
        for record in records
    )


def _valid_suppression_payload() -> dict[str, object]:
    return {
        "artifact_id": str(ArtifactId.new()),
        "context": ArtifactContext.FAILURE_SCREENSHOT.value,
        "kind": "screenshot",
        "capture_policy": ARTIFACT_POLICY_VERSION,
        "reason": "artifact.screenshot_unavailable",
        "step_attempt_id": None,
    }


def _active_action_store(tmp_path):
    run_id = _run_id = __import__("argus.ates", fromlist=["RunId"]).RunId.new()
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
        {"run": _run_record_json(run_id), "steps": [to_json_compatible(step)]},
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
    return store, step_id, attempt_id


def _suppression_payload(**changes) -> dict[str, object]:
    payload: dict[str, object] = {
        "artifact_id": str(ArtifactId.new()),
        "context": ArtifactContext.FAILURE_SCREENSHOT.value,
        "kind": "screenshot",
        "capture_policy": ARTIFACT_POLICY_VERSION,
        "reason": "artifact.screenshot_unavailable",
        "step_attempt_id": None,
    }
    payload.update(changes)
    return payload


def _payload_validation_store(tmp_path, *, environment, target):
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
        {"run": _run_record_json(run_id), "steps": [to_json_compatible(step)]},
    )
    store.append(EventType.ENVIRONMENT_PREPARED, environment)
    store.append(EventType.TARGET_LAUNCHED, target)
    store.append(
        EventType.STEP_ATTEMPT_STARTED,
        _attempt_payload(step_id, attempt_id, "running", ended=False),
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
    return store


def _open_validation_store(
    tmp_path,
    *,
    step_kind="act",
    step_extra=None,
    run_payload_extra=None,
):
    run_id = RunId.new()
    step_id = StepId.new()
    attempt_id = StepAttemptId.new()
    step = to_json_compatible(
        StepRecord(
            step_id=step_id,
            instruction=EvidenceValue.redacted("privacy.authored_text"),
            kind=step_kind,
        )
    )
    if step_extra:
        step.update(step_extra)
    store = AtesEventStore(tmp_path, run_id)
    run_payload = {"run": _run_record_json(run_id), "steps": [step]}
    if run_payload_extra:
        run_payload.update(run_payload_extra)
    store.append(EventType.RUN_STARTED, run_payload)
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
    return store, step_id, attempt_id


def _finish_validation_store(store, step_id, attempt_id, *, status="passed", close=True, result="pass"):
    store.append(
        EventType.STEP_ATTEMPT_COMPLETED,
        _attempt_payload(step_id, attempt_id, status),
    )
    if close:
        store.append(EventType.TARGET_CLOSED, {})
    store.append(EventType.ENVIRONMENT_RELEASED, {})
    store.append(
        EventType.RUN_MARKED_INCOMPLETE,
        {"reason": "runtime.finalization_pending", "execution_result": result},
    )


def _invalidate_supersession(root, approval_id, target, key):
    approvals = [json.loads(line) for line in (root / "approvals.jsonl").read_text("utf-8").splitlines()]
    changed = next(row for row in approvals if row["approval_id"] == approval_id)
    changed["supersedes_approval_id"] = target
    changed["authentication"]["signature"] = audit_module._sign_record(changed, key)
    (root / "approvals.jsonl").write_bytes(_canonical_lines(approvals))

    # Keep every signature, audit binding, and subsequent chain link valid so
    # the invalid historical relationship is the sole source of rejection.
    audits = [json.loads(line) for line in (root / "audit.jsonl").read_text("utf-8").splitlines()]
    previous = None
    for record in audits:
        details = record["details"]
        if record["event_type"] == "approval.changed" and details.get("approval_id") == approval_id:
            details["supersedes_approval_id"] = target
            details["approval_record_digest"] = audit_module._approval_digest(changed)
        record["previous_record_digest"] = previous
        previous = audit_module._audit_digest(record)
    (root / "audit.jsonl").write_bytes(_canonical_lines(audits))


def _append_collection_outcome(store, retained, ordinal):
    if retained:
        capture = AtesArtifactRepository(store).capture_bytes(
            b"collected file",
            context=ArtifactContext.COLLECTED_FILE,
            kind="collected_file",
            media_type="application/octet-stream",
        )
        assert capture.record is not None
        store.append(
            EventType.ARTIFACT_COLLECTED,
            {"artifact": to_json_compatible(capture.record), "collection_ordinal": ordinal},
        )
    else:
        store.append(
            EventType.ARTIFACT_SUPPRESSED,
            {
                "artifact_id": str(ArtifactId.new()),
                "context": ArtifactContext.COLLECTED_FILE.value,
                "kind": "collected_file",
                "capture_policy": ARTIFACT_POLICY_VERSION,
                "reason": "artifact.too_large",
                "step_attempt_id": None,
                "collection_ordinal": ordinal,
            },
        )


_ATTEMPT_STARTED = "2026-08-23T10:00:00+00:00"


_ATTEMPT_ENDED = "2026-08-23T10:00:01+00:00"


def _target():
    return to_json_compatible(EvidenceValue.redacted("privacy.target_value"))


def _open_action_store(tmp_path, *, kind="act"):
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
                "started_at": _ATTEMPT_STARTED,
                "ended_at": None,
                "retry_reason": None,
            }
        },
    )
    return store, step_id, attempt_id


def _complete_action_store(store, step_id, attempt_id, *, status="passed", close=True):
    store.append(
        EventType.STEP_ATTEMPT_COMPLETED,
        {
            "attempt": {
                "step_attempt_id": str(attempt_id),
                "step_id": str(step_id),
                "attempt": 1,
                "status": status,
                "started_at": _ATTEMPT_STARTED,
                "ended_at": _ATTEMPT_ENDED,
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


def _attempt_payload(step_id, attempt_id, status, *, ordinal=1, ended=True):
    return {
        "attempt": {
            "step_attempt_id": str(attempt_id),
            "step_id": str(step_id),
            "attempt": ordinal,
            "status": status,
            "started_at": _ATTEMPT_STARTED,
            "ended_at": _ATTEMPT_ENDED if ended else None,
            "retry_reason": None,
        }
    }


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
                "started_at": _ATTEMPT_STARTED,
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
                "started_at": _ATTEMPT_STARTED,
                "ended_at": _ATTEMPT_ENDED,
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


_TRACEABILITY_STARTED = "2026-08-24T05:00:00+00:00"


_TRACEABILITY_ENDED = "2026-08-24T05:00:01+00:00"


def _traceability_attempt(step_id, attempt_id, status, *, ordinal=1, ended):
    return {
        "attempt": {
            "step_attempt_id": str(attempt_id),
            "step_id": str(step_id),
            "attempt": ordinal,
            "status": status,
            "started_at": _TRACEABILITY_STARTED,
            "ended_at": _TRACEABILITY_ENDED if ended else None,
            "retry_reason": None if ordinal == 1 else EvidenceValue.safe("retry"),
        }
    }


def _finalize_and_recover(store: AtesEventStore, project_dir):
    run_id = store.run_id
    try:
        finalize_revision_one(store)
    finally:
        store.close()
    return recover_revision_one(project_dir, run_id)


def _run_with_two_assertions_and_artifacts(tmp_path):
    run_id = RunId.new()
    step_id = StepId.new()
    attempt_id = StepAttemptId.new()
    step = StepRecord(
        step_id=step_id,
        instruction=EvidenceValue.safe("verify two independent requirements"),
        kind="assert",
    )
    store = AtesEventStore(tmp_path, run_id)
    store.append(
        EventType.RUN_STARTED,
        {"run": _run_record_json(run_id), "steps": [to_json_compatible(step)]},
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
        _traceability_attempt(step_id, attempt_id, "running", ended=False),
    )

    for revision in ("rev-a", "rev-b"):
        requirement = RequirementIdentity(
            requirement_id="REQ-shared",
            source_system="round6",
            source_revision=revision,
        )
        assertion = AssertionRecord(
            assertion_id=AssertionId.new(),
            step_id=step_id,
            step_attempt_id=attempt_id,
            kind="equals",
            expected=EvidenceValue.safe("ok"),
            actual=EvidenceValue.safe("ok"),
            result="passed",
            method="deterministic",
            required=True,
            requirement=requirement,
        )
        store.append(
            EventType.ASSERTION_EVALUATED,
            {"assertion": to_json_compatible(assertion)},
        )

    artifacts = AtesArtifactRepository(store)
    for payload in (b"artifact-for-something-a", b"artifact-for-something-b"):
        captured = artifacts.capture_bytes(
            payload,
            context=ArtifactContext.CHECKPOINT_SCREENSHOT,
            kind="screenshot",
            media_type="image/png",
        )
        assert captured.record is not None
        store.append(
            EventType.CHECKPOINT_CAPTURED,
            {
                "artifact": to_json_compatible(captured.record),
                "context": ArtifactContext.CHECKPOINT_SCREENSHOT.value,
                "step_attempt_id": str(attempt_id),
            },
        )

    store.append(
        EventType.STEP_ATTEMPT_COMPLETED,
        _traceability_attempt(step_id, attempt_id, "passed", ended=True),
    )
    store.append(EventType.TARGET_CLOSED, {})
    store.append(EventType.ENVIRONMENT_RELEASED, {})
    store.append(
        EventType.RUN_MARKED_INCOMPLETE,
        {"reason": "runtime.finalization_pending", "execution_result": "pass"},
    )
    return store


def _audit_worker(run_dir: str, index: int, start_event, queue) -> None:
    try:
        from argus.ates import append_audit_event as child_append

        start_event.wait()
        record = child_append(
            run_dir,
            "round6.concurrent",
            actor=f"worker-{index}",
            details={"index": index},
            dedupe_key=f"round6:{index}",
        )
        queue.put(("ok", record["audit_id"]))
    except BaseException as exc:  # pragma: no cover - child diagnostics
        queue.put(("error", f"{type(exc).__name__}: {exc}"))


def _finalize_without_package(store):
    try:
        return finalize_revision_one(store)
    finally:
        store.close()


def _finalize_and_complete(tmp_path):
    store = _open_run(tmp_path)
    try:
        finalization = finalize_revision_one(store)
    finally:
        store.close()
    package = complete_run_package(finalization)
    return finalization, package


def _report_model(root):
    return json.loads((root / "reports" / "report.json").read_text("utf-8"))


def _assert_report_is_self_described_as_derived(root):
    model = _report_model(root)
    assert model["report_trust_state"] == FinalizationTrustState.UNVERIFIED_DERIVED.value
    snapshot = model["source"]["detached_ledger_snapshot"]
    assert snapshot == model["detached_ledger_snapshot"]
    by_path = {item["path"]: item for item in snapshot["members"]}
    assert set(by_path) == {"approvals.jsonl", "audit.jsonl"}
    for name, meta in by_path.items():
        raw = (root / name).read_bytes()
        assert meta["size_bytes"] == len(raw)
        assert meta["sha256"].startswith("sha256:")
    return model


def _partial_append_then_raise(target_name: str, original):
    injected = {"done": False}

    def writer(pin, lock, name, line):
        if name == target_name and not injected["done"]:
            injected["done"] = True
            lock.assert_authoritative()
            handle, _created = audit_module._open_regular_file(pin, name)
            try:
                handle.seek(0, os.SEEK_END)
                # Canonical ledger records contain no embedded newlines, so this
                # is guaranteed to leave an unterminated final record.
                partial = line[: max(1, (len(line) - 1) // 2)]
                assert partial and not partial.endswith(b"\n")
                assert handle.write(partial) == len(partial)
                handle.flush()
                os.fsync(handle.fileno())
                pin.assert_file_identity(name, handle.fileno(), f"injected partial {name}")
            finally:
                handle.close()
            raise ApprovalError(f"injected partial {name} append")
        return original(pin, lock, name, line)

    return writer


def _finalized_package(tmp_path):
    store = _open_run(tmp_path)
    try:
        finalization = finalize_revision_one(store)
    finally:
        store.close()
    complete_run_package(finalization)
    return finalization


def _custom_run(tmp_path, *, instruction: str, requirements=()):
    run_id = RunId.new()
    step_id = StepId.new()
    attempt_id = StepAttemptId.new()
    step = StepRecord(
        step_id=step_id,
        instruction=EvidenceValue.safe(instruction),
        kind="assert" if requirements else "act",
    )
    store = AtesEventStore(tmp_path, run_id)
    store.append(
        EventType.RUN_STARTED,
        {"run": _run_record_json(run_id), "steps": [to_json_compatible(step)]},
    )
    store.append(EventType.ENVIRONMENT_PREPARED, {"environment_type": "direct", "isolated": False})
    store.append(
        EventType.TARGET_LAUNCHED,
        {"target": to_json_compatible(EvidenceValue.redacted("privacy.target_value"))},
    )
    store.append(EventType.STEP_ATTEMPT_STARTED, _attempt_payload(step_id, attempt_id, "running", ended=False))
    for revision in requirements:
        requirement = RequirementIdentity(
            requirement_id="REQ-shared-display-id",
            source_system="test-suite",
            source_revision=revision,
        )
        assertion = AssertionRecord(
            assertion_id=AssertionId.new(),
            step_id=step_id,
            step_attempt_id=attempt_id,
            kind="equals",
            expected=EvidenceValue.safe("expected"),
            actual=EvidenceValue.safe("expected"),
            result="passed",
            method="deterministic",
            required=True,
            requirement=requirement,
        )
        store.append(EventType.ASSERTION_EVALUATED, {"assertion": to_json_compatible(assertion)})
    store.append(EventType.STEP_ATTEMPT_COMPLETED, _attempt_payload(step_id, attempt_id, "passed", ended=True))
    store.append(EventType.TARGET_CLOSED, {})
    store.append(EventType.ENVIRONMENT_RELEASED, {})
    store.append(
        EventType.RUN_MARKED_INCOMPLETE,
        {"reason": "runtime.finalization_pending", "execution_result": "pass"},
    )
    return store
