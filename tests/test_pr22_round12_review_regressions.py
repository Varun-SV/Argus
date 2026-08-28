from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest

from argus.ates import (
    ActionId,
    ActionOperationId,
    ActionRecord,
    ApprovalError,
    ArtifactContext,
    AtesArtifactRepository,
    AtesEventStore,
    EventType,
    EvidenceValue,
    FinalizationError,
    FinalizationTrustState,
    RunId,
    RunStatus,
    StepAttemptId,
    StepId,
    StepRecord,
    append_audit_event,
    finalize_revision_one,
    render_reports,
    to_json_compatible,
    validate_audit_chain,
    verify_report_bundle,
)
from tests.test_ates_finalization import _open_run, _run_record_json
from tests.test_pr22_round3_review_regressions import _attempt_payload
from tests.test_pr22_round9_review_regressions import _finalized_package


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


def test_protected_artifact_missing_policy_metadata_cannot_finalize(tmp_path):
    store = _open_run(tmp_path)
    record = _captured_protected_artifact(store)
    artifact = to_json_compatible(record)
    assert artifact["protection_state"] == "protected_ref"
    artifact.pop("authorization_ref")
    store.append(
        EventType.CHECKPOINT_CAPTURED,
        {
            "artifact": artifact,
            "context": ArtifactContext.FAILURE_SCREENSHOT.value,
            "step_attempt_id": None,
        },
    )
    try:
        with pytest.raises(FinalizationError, match="artifact metadata"):
            finalize_revision_one(store)
    finally:
        store.close()


def test_full_artifact_policy_metadata_is_preserved_in_manifest(tmp_path):
    store = _open_run(tmp_path)
    record = _captured_protected_artifact(store)
    artifact = to_json_compatible(record)
    store.append(
        EventType.CHECKPOINT_CAPTURED,
        {
            "artifact": artifact,
            "context": ArtifactContext.FAILURE_SCREENSHOT.value,
            "step_attempt_id": None,
        },
    )
    try:
        result = finalize_revision_one(store)
    finally:
        store.close()

    manifest = json.loads(result.evidence_manifest_path.read_text("utf-8"))
    assert len(manifest["artifacts"]) == 1
    persisted = manifest["artifacts"][0]
    for field in (
        "artifact_id",
        "kind",
        "path",
        "sensitivity",
        "capture_policy",
        "size_bytes",
        "protection_state",
        "protected_ref",
        "access_policy",
        "retention_policy",
        "authorization_ref",
    ):
        assert persisted[field] == artifact[field]
    assert persisted["content_digest"] == artifact["content_digest"]
    assert persisted["content_digest"]["method"] == "hmac-sha256"


def test_externally_bound_report_remains_valid_after_detached_audit_update(tmp_path):
    finalization = _finalized_package(tmp_path)
    root = finalization.run_dir
    bundle = render_reports(root)
    manifest_bytes = bundle.manifest_path.read_bytes()
    trusted_digest = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()

    append_audit_event(
        root,
        "review.note",
        actor="round12-reviewer",
        details={"note": "post-report append-only audit activity"},
    )

    stale = verify_report_bundle(root)
    assert stale.trust_state is FinalizationTrustState.INVALID

    historical = verify_report_bundle(
        root,
        trusted_report_manifest_digest=trusted_digest,
    )
    assert historical.trust_state is FinalizationTrustState.BOUND_VERIFIED


@pytest.mark.parametrize(
    "mutation",
    [
        "event_type",
        "actor",
        "occurred_at",
        "details",
        "dedupe_key",
    ],
)
def test_audit_chain_rejects_malformed_required_record_fields(tmp_path, mutation):
    finalization = _finalized_package(tmp_path)
    root = finalization.run_dir
    path = root / "audit.jsonl"
    records = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
    assert len(records) == 1
    record = deepcopy(records[0])

    if mutation in {"event_type", "actor", "dedupe_key"}:
        record.pop(mutation, None)
    elif mutation == "occurred_at":
        record[mutation] = "2026-08-28T07:00:00"
    else:
        record[mutation] = ["not", "an", "object"]

    path.write_text(
        json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ApprovalError, match=f"audit record 1.*{mutation}"):
        validate_audit_chain(root)
