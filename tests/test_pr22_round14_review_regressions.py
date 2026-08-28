from __future__ import annotations

import json
import threading
import time
from copy import deepcopy

import pytest

import argus.ates.audit_impl as audit_impl
import argus.ates.audit_round2 as audit_round2
import argus.ates.reports_runtime as reports_runtime
from argus.ates import (
    ARTIFACT_POLICY_VERSION,
    ApprovalCredential,
    ArtifactContext,
    ArtifactId,
    AtesArtifactRepository,
    AtesEventStore,
    EventType,
    EvidenceValue,
    FinalizationError,
    FinalizationTrustState,
    FindingId,
    RunId,
    StepAttemptId,
    StepId,
    StepRecord,
    append_approval,
    finalize_revision_one,
    render_reports,
    revoke_approval,
    to_json_compatible,
    validate_approvals,
    verify_report_bundle,
)
from tests.test_ates_finalization import _open_run, _run_record_json
from tests.test_pr22_round3_review_regressions import _attempt_payload
from tests.test_pr22_round9_review_regressions import _finalized_package


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


def _credential():
    key = b"0123456789abcdef0123456789abcdef"
    credential = ApprovalCredential(
        key_id="round14-reviewer-key",
        key=key,
        actor="reviewer@example.invalid",
        roles=("test_reviewer",),
    )
    resolver = lambda key_id: credential if key_id == credential.key_id else None
    return key, credential, resolver


def test_structurally_stale_superseder_cannot_advance_request_generation(tmp_path):
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
    revoked = revoke_approval(
        root,
        first["approval_id"],
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
    )

    approvals_path = root / "approvals.jsonl"
    approvals = [
        json.loads(line) for line in approvals_path.read_text("utf-8").splitlines()
    ]
    malformed = deepcopy(approvals[-1])
    assert malformed["approval_id"] == revoked["approval_id"]
    malformed["manifest_digest"] = "sha256:" + "0" * 64
    malformed["authentication"]["signature"] = audit_impl._sign_record(malformed, key)
    approvals[-1] = malformed
    approvals_path.write_bytes(_canonical_lines(approvals))

    # Keep the superseder correctly signed and exactly audit-bound. Only its
    # stale immutable-package binding makes it structurally invalid.
    audit_path = root / "audit.jsonl"
    audits = [json.loads(line) for line in audit_path.read_text("utf-8").splitlines()]
    matching = [
        record
        for record in audits
        if isinstance(record.get("details"), dict)
        and record["details"].get("approval_id") == malformed["approval_id"]
    ]
    assert len(matching) == 1
    matching[0]["details"]["approval_record_digest"] = audit_round2._approval_digest(
        malformed
    )
    audit_path.write_bytes(_canonical_lines(audits))

    retried = append_approval(
        root,
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
        key_resolver=resolver,
    )
    assert retried["approval_id"] == first["approval_id"]
    assert retried["request_id"] == first["request_id"]
    assert len(approvals_path.read_text("utf-8").splitlines()) == 2

    state = validate_approvals(root, key_resolver=resolver)
    assert [item.record["approval_id"] for item in state.verified_approvals] == [
        first["approval_id"]
    ]


def test_concurrent_report_writers_are_serialized_across_complete_transactions(
    tmp_path, monkeypatch
):
    finalization = _finalized_package(tmp_path)
    root = finalization.run_dir
    key, credential, resolver = _credential()
    append_approval(
        root,
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
    )

    real_model = reports_runtime._model
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    first_calls = 0
    call_guard = threading.Lock()

    def tracked_model(run_root, key_resolver):
        nonlocal first_calls
        name = threading.current_thread().name
        if name == "render-first":
            with call_guard:
                first_calls += 1
                first_call = first_calls == 1
            if first_call:
                first_entered.set()
                assert release_first.wait(3)
        elif name == "render-second":
            second_entered.set()
        return real_model(run_root, key_resolver)

    monkeypatch.setattr(reports_runtime, "_model", tracked_model)
    errors: list[BaseException] = []
    results = []

    def writer(resolver_arg):
        try:
            results.append(
                render_reports(root, approval_key_resolver=resolver_arg)
            )
        except BaseException as exc:  # surfaced below with the original traceback
            errors.append(exc)

    first = threading.Thread(target=writer, args=(None,), name="render-first")
    second = threading.Thread(target=writer, args=(resolver,), name="render-second")
    first.start()
    assert first_entered.wait(3)
    second.start()
    time.sleep(0.1)
    assert not second_entered.is_set()
    release_first.set()
    first.join(5)
    second.join(5)

    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert len(results) == 2
    assert second_entered.is_set()
    assert (
        verify_report_bundle(root, approval_key_resolver=resolver).trust_state
        is FinalizationTrustState.REGENERATED_VERIFIED
    )


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


def test_target_launch_requires_privacy_classified_evidence(tmp_path):
    store = _payload_validation_store(
        tmp_path,
        environment={"environment_type": "direct", "isolated": False},
        target={"target": "password=plaintext-secret"},
    )
    try:
        with pytest.raises(FinalizationError, match="privacy-classified"):
            finalize_revision_one(store)
    finally:
        store.close()


def test_environment_payload_must_match_run_record(tmp_path):
    store = _payload_validation_store(
        tmp_path,
        environment={"environment_type": "container", "isolated": False},
        target={
            "target": to_json_compatible(
                EvidenceValue.redacted("privacy.target_value")
            )
        },
    )
    try:
        with pytest.raises(FinalizationError, match="environment_type contradicts"):
            finalize_revision_one(store)
    finally:
        store.close()


@pytest.mark.parametrize(
    "payload",
    [
        {
            "reason": "customer password was visible in the crash dialog",
            "execution_result": "error",
        },
        {
            "reason": "runtime.execution_interrupted",
            "execution_result": "error",
            "detail": "secret-bearing extension",
        },
    ],
)
def test_nonprovisional_incomplete_marker_cannot_certify_free_text_or_extensions(
    tmp_path, payload
):
    store = _open_run(tmp_path, provisional=False)
    store.append(EventType.RUN_MARKED_INCOMPLETE, payload)
    try:
        with pytest.raises(FinalizationError, match="RUN_MARKED_INCOMPLETE"):
            finalize_revision_one(store)
    finally:
        store.close()
