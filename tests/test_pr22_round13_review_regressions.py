from __future__ import annotations

import json
import os
from copy import deepcopy

import pytest

import argus.ates.audit as audit_api
import argus.ates.audit_impl as audit_impl
import argus.ates.audit_round2 as audit_round2
from argus.ates import (
    ARTIFACT_POLICY_VERSION,
    ActionId,
    ActionOperationId,
    ActionRecord,
    ApprovalCredential,
    ApprovalError,
    ArtifactContext,
    ArtifactId,
    AtesEventStore,
    EventType,
    EvidenceValue,
    FinalizationError,
    FindingId,
    RunStatus,
    StepAttemptId,
    StepId,
    StepRecord,
    VerificationStatus,
    append_approval,
    finalize_revision_one,
    to_json_compatible,
    validate_approvals,
    validate_audit_chain,
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


def _valid_suppression_payload() -> dict[str, object]:
    return {
        "artifact_id": str(ArtifactId.new()),
        "context": ArtifactContext.FAILURE_SCREENSHOT.value,
        "kind": "screenshot",
        "capture_policy": ARTIFACT_POLICY_VERSION,
        "reason": "artifact.screenshot_unavailable",
        "step_attempt_id": None,
    }


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


def test_dangling_action_proposal_is_an_execution_lifecycle_error(tmp_path):
    store, step_id, attempt_id = _active_action_store(tmp_path)
    action = ActionRecord(
        action_id=ActionId.new(),
        step_id=step_id,
        step_attempt_id=attempt_id,
        action_type="click",
        parameters={},
        operation_id=ActionOperationId.new(),
    )
    store.append(EventType.ACTION_PROPOSED, {"action": to_json_compatible(action)})
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
        result = finalize_revision_one(store)
        assert result.outcome.effective_status is RunStatus.ERROR
    finally:
        store.close()


@pytest.mark.parametrize(
    "payload",
    [
        {"reason": "runtime.finalization_pending"},
        {"reason": "runtime.finalization_pending", "execution_result": 1},
        {"reason": "runtime.finalization_pending", "execution_result": "paased"},
        {"reason": "runtime.finalization_pending", "execution_result": "failed"},
    ],
)
def test_unknown_provisional_execution_result_is_rejected(tmp_path, payload):
    store = _open_run(tmp_path, provisional=False)
    store.append(EventType.RUN_MARKED_INCOMPLETE, payload)
    try:
        with pytest.raises(FinalizationError, match="execution_result"):
            finalize_revision_one(store)
    finally:
        store.close()


def _credential():
    key = b"0123456789abcdef0123456789abcdef"
    credential = ApprovalCredential(
        key_id="round13-reviewer-key",
        key=key,
        actor="reviewer@example.invalid",
        roles=("test_reviewer",),
    )
    resolver = lambda key_id: credential if key_id == credential.key_id else None
    return key, credential, resolver


def test_signed_audit_bound_approval_without_timestamp_is_not_effective(tmp_path):
    finalization = _finalized_package(tmp_path)
    root = finalization.run_dir
    key, credential, resolver = _credential()
    approval = append_approval(
        root,
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
    )

    approvals_path = root / "approvals.jsonl"
    approval_records = [
        json.loads(line) for line in approvals_path.read_text("utf-8").splitlines()
    ]
    assert len(approval_records) == 1
    malformed = deepcopy(approval_records[0])
    malformed.pop("occurred_at")
    malformed["authentication"]["signature"] = audit_impl._sign_record(malformed, key)
    approvals_path.write_bytes(_canonical_lines([malformed]))

    audit_path = root / "audit.jsonl"
    audit_records = [
        json.loads(line) for line in audit_path.read_text("utf-8").splitlines()
    ]
    matching = [
        record
        for record in audit_records
        if isinstance(record.get("details"), dict)
        and record["details"].get("approval_id") == approval["approval_id"]
    ]
    assert len(matching) == 1
    matching[0]["details"]["approval_record_digest"] = audit_round2._approval_digest(
        malformed
    )
    audit_path.write_bytes(_canonical_lines(audit_records))

    # The malformed row is still correctly signed by the trusted reviewer and
    # has an exact approval.changed digest binding; chronology alone must keep
    # it from becoming effective.
    auth_status, _ = audit_api._authentication_status(malformed, resolver)
    assert auth_status is VerificationStatus.VERIFIED
    state = validate_approvals(root, key_resolver=resolver)
    assert len(state.records) == 1
    assert state.records[0].verification_status is VerificationStatus.INVALID
    assert "occurred_at" in (state.records[0].reason or "")
    assert state.effective_approval_ids == ()


@pytest.mark.skipif(os.name == "nt", reason="symlink creation is privilege-dependent on Windows CI")
@pytest.mark.parametrize("ledger", ["approvals.jsonl", "audit.jsonl"])
def test_dangling_symlink_ledger_is_not_treated_as_absent(tmp_path, ledger):
    store = _open_run(tmp_path)
    try:
        result = finalize_revision_one(store)
    finally:
        store.close()
    root = result.run_dir
    path = root / ledger
    path.symlink_to(root / "does-not-exist.jsonl")

    with pytest.raises(ApprovalError):
        if ledger == "approvals.jsonl":
            validate_approvals(root)
        else:
            validate_audit_chain(root)


def test_duplicate_audit_dedupe_keys_in_hash_valid_chain_are_rejected(tmp_path):
    finalization = _finalized_package(tmp_path)
    root = finalization.run_dir
    path = root / "audit.jsonl"
    records = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
    assert records and isinstance(records[-1].get("dedupe_key"), str)

    prior = records[-1]
    duplicate = deepcopy(prior)
    duplicate["audit_id"] = "AUDIT-" + "a" * 32
    if duplicate["audit_id"] == prior["audit_id"]:
        duplicate["audit_id"] = "AUDIT-" + "b" * 32
    duplicate["previous_record_digest"] = audit_impl._audit_digest(prior)
    records.append(duplicate)
    path.write_bytes(_canonical_lines(records))

    with pytest.raises(ApprovalError, match="duplicate dedupe_key"):
        validate_audit_chain(root)
