"""ATES capture validation regression coverage."""
from __future__ import annotations

import json

import pytest

import argus.ates.finalization as finalization_module
from argus.ates import (
    ArtifactContext,
    AtesArtifactRepository,
    EventType,
    EvidenceValue,
    FinalizationError,
    FindingId,
    FindingRecord,
    RunStatus,
    StepAttemptId,
    finalize_revision_one,
    render_reports,
    to_json_compatible,
    verify_finalized_run,
)
from tests.ates_test_support import (
    _append_collection_outcome,
    _append_pending,
    _captured_protected_artifact,
    _finish_validation_store,
    _open_run_with_protected_artifact,
    _open_validation_store,
    _suppression_payload,
    _valid_suppression_payload,
)
from tests.test_ates_finalization import _open_run

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


def test_malformed_finding_is_rejected_before_finalization(tmp_path):
    store = _open_run(tmp_path, provisional=False)
    store.append(EventType.FINDING_RECORDED, {})
    _append_pending(store)
    try:
        with pytest.raises(FinalizationError, match="FINDING_RECORDED finding"):
            finalize_revision_one(store)
    finally:
        store.close()


def test_valid_finding_survives_into_verified_report(tmp_path):
    store = _open_run(tmp_path, provisional=False)
    finding = FindingRecord(
        finding_id=FindingId.new(),
        title=EvidenceValue.safe("visible finding"),
        description=EvidenceValue.safe("canonical finding description"),
        evidence_refs=(),
        classification_source="model",
        classification="low",
    )
    store.append(
        EventType.FINDING_RECORDED,
        {"finding": to_json_compatible(finding)},
    )
    _append_pending(store)
    try:
        result = finalize_revision_one(store)
    finally:
        store.close()

    bundle = render_reports(result.run_dir)
    model = json.loads(bundle.json_path.read_text("utf-8"))
    assert [item["finding_id"] for item in model["findings"]] == [
        str(finding.finding_id)
    ]


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


@pytest.mark.parametrize("mutation", ["unsupported_reason", "unexpected_plaintext"])
def test_malformed_artifact_suppression_cannot_finalize(tmp_path, mutation):
    store = _open_run(tmp_path)
    payload = _valid_suppression_payload()
    if mutation == "unsupported_reason":
        payload["reason"] = "secret password copied from target"
    else:
        payload["secret"] = "must-never-be-certified-or-rendered"
    store.append(EventType.ARTIFACT_SUPPRESSED, payload)
    try:
        with pytest.raises(FinalizationError, match="ARTIFACT_SUPPRESSED"):
            finalize_revision_one(store)
    finally:
        store.close()


def test_valid_artifact_suppression_contract_remains_finalizable(tmp_path):
    store = _open_run(tmp_path)
    store.append(EventType.ARTIFACT_SUPPRESSED, _valid_suppression_payload())
    try:
        result = finalize_revision_one(store)
        assert result.outcome.effective_status is RunStatus.PASSED
    finally:
        store.close()


@pytest.mark.parametrize(
    "relationship",
    [
        {"step_attempt_id": str(StepAttemptId.new())},
        {"finding_id": str(FindingId.new())},
    ],
)
def test_suppression_relationships_must_reference_canonical_history(
    tmp_path, relationship
):
    store = _open_run(tmp_path)
    store.append(
        EventType.ARTIFACT_SUPPRESSED,
        _suppression_payload(**relationship),
    )
    try:
        with pytest.raises(FinalizationError, match="unknown (step_attempt_id|finding_id)"):
            finalize_revision_one(store)
    finally:
        store.close()


def test_artifact_id_is_unique_across_retained_and_suppressed_outcomes(tmp_path):
    store = _open_run(tmp_path)
    captured = AtesArtifactRepository(store).capture_bytes(
        b"round-14 retained screenshot",
        context=ArtifactContext.FAILURE_SCREENSHOT,
        kind="screenshot",
        media_type="image/png",
    )
    assert captured.record is not None
    store.append(
        EventType.CHECKPOINT_CAPTURED,
        {
            "artifact": to_json_compatible(captured.record),
            "context": ArtifactContext.FAILURE_SCREENSHOT.value,
            "step_attempt_id": None,
        },
    )
    store.append(
        EventType.ARTIFACT_SUPPRESSED,
        _suppression_payload(artifact_id=str(captured.record.artifact_id)),
    )
    try:
        with pytest.raises(FinalizationError, match="artifact_id is duplicated"):
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


def test_retained_finding_screenshot_keeps_context_and_finding_relation(tmp_path):
    store, step_id, attempt_id = _open_validation_store(tmp_path)
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
    _finish_validation_store(store, step_id, attempt_id)
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


@pytest.mark.parametrize("outcomes", [(True, True), (True, False), (False, True), (False, False)])
def test_collection_ordinals_are_unique_across_retained_and_suppressed(tmp_path, outcomes):
    store = _open_run(tmp_path, provisional=False)
    try:
        for retained in outcomes:
            _append_collection_outcome(store, retained, 1)
        with pytest.raises(FinalizationError, match="collection_ordinal.*duplicated"):
            finalize_revision_one(store)
    finally:
        store.close()


@pytest.mark.parametrize("outcomes", [(True, True), (True, False), (False, True), (False, False)])
def test_distinct_collection_ordinals_remain_finalizable(tmp_path, outcomes):
    store = _open_run(tmp_path, provisional=False)
    try:
        for ordinal, retained in enumerate(outcomes, 1):
            _append_collection_outcome(store, retained, ordinal)
        assert finalize_revision_one(store).outcome.effective_status is RunStatus.PASSED
    finally:
        store.close()
