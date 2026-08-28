from __future__ import annotations

import json
from copy import deepcopy

import pytest

import argus.ates.audit_impl as audit_impl
import argus.ates.audit_round2 as audit_round2
from argus.ates import (
    ApprovalCredential,
    ArtifactContext,
    AtesArtifactRepository,
    AtesEventStore,
    AssertionId,
    EventType,
    EvidenceValue,
    FinalizationError,
    FindingId,
    FindingRecord,
    RunId,
    RunStatus,
    StepAttemptId,
    StepId,
    StepRecord,
    append_approval,
    finalize_revision_one,
    render_reports,
    to_json_compatible,
    validate_approvals,
)
from tests.test_ates_finalization import _open_run, _run_record_json
from tests.test_pr22_round3_review_regressions import _attempt_payload
from tests.test_pr22_round9_review_regressions import _finalized_package


def _manual_store(tmp_path, *, step_kind="act", step_extra=None):
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
    store.append(
        EventType.RUN_STARTED,
        {"run": _run_record_json(run_id), "steps": [step]},
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


def _finish(store, step_id, attempt_id, *, status="passed", close=True, result="pass"):
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


def _canonical_lines(records) -> bytes:
    return b"".join(
        (json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
        for item in records
    )


def test_step_extension_cannot_be_certified_or_republished(tmp_path):
    store, step_id, attempt_id = _manual_store(
        tmp_path,
        step_extra={"debug_note": "API_TOKEN=plaintext-secret"},
    )
    _finish(store, step_id, attempt_id)
    try:
        with pytest.raises(FinalizationError, match="RUN_STARTED step contains unexpected fields"):
            finalize_revision_one(store)
    finally:
        store.close()


@pytest.mark.parametrize(
    "changes,pattern",
    [
        ({"context": "not-a-real-context"}, "context is invalid"),
        ({"step_attempt_id": str(StepAttemptId.new())}, "unknown step_attempt_id"),
        ({"finding_id": str(FindingId.new())}, "unknown finding_id"),
    ],
)
def test_checkpoint_relationships_are_validated(tmp_path, changes, pattern):
    store = _open_run(tmp_path, provisional=False)
    captured = AtesArtifactRepository(store).capture_bytes(
        b"retained screenshot",
        context=ArtifactContext.FAILURE_SCREENSHOT,
        kind="screenshot",
        media_type="image/png",
    )
    assert captured.record is not None
    payload = {
        "artifact": to_json_compatible(captured.record),
        "context": ArtifactContext.FAILURE_SCREENSHOT.value,
        "step_attempt_id": None,
    }
    payload.update(changes)
    store.append(EventType.CHECKPOINT_CAPTURED, payload)
    try:
        with pytest.raises(FinalizationError, match=pattern):
            finalize_revision_one(store)
    finally:
        store.close()


def test_finding_ids_and_evidence_refs_must_be_unique_and_resolved(tmp_path):
    store = _open_run(tmp_path, provisional=False)
    finding_id = FindingId.new()
    finding = FindingRecord(
        finding_id=finding_id,
        title=EvidenceValue.safe("finding"),
        description=EvidenceValue.safe("description"),
        evidence_refs=("OBS-00000000000000000000000000000000",),
    )
    store.append(EventType.FINDING_RECORDED, {"finding": to_json_compatible(finding)})
    store.append(EventType.FINDING_RECORDED, {"finding": to_json_compatible(finding)})
    try:
        with pytest.raises(FinalizationError, match="finding IDs must be unique|unknown canonical evidence"):
            finalize_revision_one(store)
    finally:
        store.close()


def test_finding_extension_is_rejected_before_report_generation(tmp_path):
    store = _open_run(tmp_path, provisional=False)
    finding = to_json_compatible(
        FindingRecord(
            finding_id=FindingId.new(),
            title=EvidenceValue.safe("finding"),
            description=EvidenceValue.safe("description"),
        )
    )
    finding["debug_note"] = "API_TOKEN=plaintext-secret"
    store.append(EventType.FINDING_RECORDED, {"finding": finding})
    try:
        with pytest.raises(FinalizationError, match="FINDING_RECORDED finding contains unexpected fields"):
            finalize_revision_one(store)
    finally:
        store.close()


def test_signed_audit_bound_approval_with_plaintext_reason_and_extension_is_invalid(tmp_path):
    finalization = _finalized_package(tmp_path)
    root = finalization.run_dir
    key = b"0123456789abcdef0123456789abcdef"
    credential = ApprovalCredential(
        key_id="round15-reviewer-key",
        key=key,
        actor="reviewer@example.invalid",
        roles=("test_reviewer",),
    )
    resolver = lambda key_id: credential if key_id == credential.key_id else None

    approval = append_approval(
        root,
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
    )
    approvals_path = root / "approvals.jsonl"
    approvals = [json.loads(line) for line in approvals_path.read_text("utf-8").splitlines()]
    malformed = deepcopy(approvals[-1])
    assert malformed["approval_id"] == approval["approval_id"]
    malformed["reason"] = "API_TOKEN=plaintext-secret"
    malformed["debug_note"] = "plaintext-extension"
    malformed["authentication"]["signature"] = audit_impl._sign_record(malformed, key)
    approvals[-1] = malformed
    approvals_path.write_bytes(_canonical_lines(approvals))

    audit_path = root / "audit.jsonl"
    audits = [json.loads(line) for line in audit_path.read_text("utf-8").splitlines()]
    matching = [
        row for row in audits
        if isinstance(row.get("details"), dict)
        and row["details"].get("approval_id") == malformed["approval_id"]
    ]
    assert len(matching) == 1
    matching[0]["details"]["approval_record_digest"] = audit_round2._approval_digest(malformed)
    audit_path.write_bytes(_canonical_lines(audits))

    state = validate_approvals(root, key_resolver=resolver)
    record = next(item for item in state.records if item.record.get("approval_id") == approval["approval_id"])
    assert record.effective is False
    assert record.verification_status.value == "invalid"


def test_non_capsule_retention_cannot_excuse_missing_target_close(tmp_path):
    store, step_id, attempt_id = _manual_store(tmp_path)
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


def test_retained_finding_screenshot_keeps_context_and_finding_relation(tmp_path):
    store, step_id, attempt_id = _manual_store(tmp_path)
    finding = FindingRecord(
        finding_id=FindingId.new(),
        title=EvidenceValue.safe("finding"),
        description=EvidenceValue.safe("description"),
    )
    store.append(EventType.FINDING_RECORDED, {"finding": to_json_compatible(finding)})
    captured = AtesArtifactRepository(store).capture_bytes(
        b"finding screenshot",
        context=ArtifactContext.FINDING_SCREENSHOT,
        kind="screenshot",
        media_type="image/png",
    )
    assert captured.record is not None
    store.append(
        EventType.CHECKPOINT_CAPTURED,
        {
            "artifact": to_json_compatible(captured.record),
            "context": ArtifactContext.FINDING_SCREENSHOT.value,
            "step_attempt_id": str(attempt_id),
            "finding_id": str(finding.finding_id),
        },
    )
    _finish(store, step_id, attempt_id)
    try:
        result = finalize_revision_one(store)
    finally:
        store.close()
    bundle = render_reports(result.run_dir)
    model = json.loads(bundle.json_path.read_text("utf-8"))
    retained = next(item for item in model["artifacts"] if "record" in item)
    assert retained["context"] == ArtifactContext.FINDING_SCREENSHOT.value
    assert retained["finding_id"] == str(finding.finding_id)
    assert retained["step_attempt_id"] == str(attempt_id)


def test_omitted_required_defaults_true_and_failed_assertion_fails_run(tmp_path):
    store, step_id, attempt_id = _manual_store(tmp_path)
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
    _finish(store, step_id, attempt_id)
    try:
        result = finalize_revision_one(store)
        assert result.outcome.effective_status is RunStatus.FAILED
    finally:
        store.close()
