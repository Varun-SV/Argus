"""Regressions for ATES recovery/privacy trust boundaries."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

import argus.ates.reports as report_module
from argus.ates import (
    ActionId,
    ActionOperationId,
    ActionRecord,
    ApprovalError,
    AtesEventStore,
    EventType,
    EvidenceValue,
    FinalizationError,
    FinalizationTrustState,
    ObservationId,
    ObservationRecord,
    RunId,
    RunStatus,
    StepAttemptId,
    StepId,
    StepRecord,
    append_approval,
    append_audit_event,
    finalize_revision_one,
    inspect_finalization_trust,
    recover_revision_one,
    render_reports,
    to_json_compatible,
    validate_approvals,
    validate_audit_chain,
)
from tests.ates_test_support import (
    _approval_credential,
    _attempt_payload,
    _canonical_lines,
    _complete_action_store,
    _finalized_package,
    _open_action_store,
)
from tests.test_ates_finalization import _open_run, _run_record_json


def _target():
    return to_json_compatible(EvidenceValue.redacted("privacy.target_value"))


def test_recovery_does_not_finalize_markerless_terminal_stream(tmp_path):
    store = _open_run(tmp_path, provisional=False)
    run_id = store.run_id
    root = store.run_dir
    before = (root / "evidence.jsonl").read_bytes()
    store.close()

    with pytest.raises(FinalizationError, match="producer terminal marker"):
        recover_revision_one(tmp_path, run_id)

    assert (root / "evidence.jsonl").read_bytes() == before
    assert not (root / "run.json").exists()
    assert not (root / "manifests" / "manifest-0001.json").exists()
    assert inspect_finalization_trust(root).trust_state is FinalizationTrustState.UNVERIFIED_DERIVED


def test_recovery_does_not_finalize_interrupted_execution_marker(tmp_path):
    store = _open_run(tmp_path, provisional=False)
    run_id = store.run_id
    root = store.run_dir
    store.append(
        EventType.RUN_MARKED_INCOMPLETE,
        {"reason": "runtime.execution_interrupted", "execution_result": "error"},
    )
    before = (root / "evidence.jsonl").read_bytes()
    store.close()

    with pytest.raises(FinalizationError, match="completion-ready"):
        recover_revision_one(tmp_path, run_id)

    assert (root / "evidence.jsonl").read_bytes() == before
    assert not (root / "run.json").exists()
    assert not (root / "manifests" / "manifest-0001.json").exists()
    with AtesEventStore(tmp_path, run_id) as reopened:
        assert not any(
            event.envelope.event_type is EventType.RUN_COMPLETED
            for event in reopened.events
        )


def test_incomplete_report_rejects_malformed_existing_step_record(tmp_path):
    store = _open_run(tmp_path)
    root = store.run_dir
    store.close()
    evidence = root / "evidence.jsonl"
    rows = [json.loads(line) for line in evidence.read_text("utf-8").splitlines()]
    rows[0]["payload"]["steps"][0].pop("step_id")
    evidence.write_bytes(_canonical_lines(rows))

    inspected = inspect_finalization_trust(root)
    assert inspected.trust_state is FinalizationTrustState.INVALID
    with pytest.raises(report_module.ReportError):
        render_reports(root)


def test_incomplete_report_rejects_completion_without_matching_start(tmp_path):
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
    store.append(EventType.TARGET_LAUNCHED, {"target": _target()})
    store.append(
        EventType.STEP_ATTEMPT_COMPLETED,
        _attempt_payload(step_id, attempt_id, "passed"),
    )
    root = store.run_dir
    store.close()

    inspected = inspect_finalization_trust(root)
    assert inspected.trust_state is FinalizationTrustState.INVALID
    with pytest.raises(report_module.ReportError):
        render_reports(root)


@pytest.mark.parametrize("action_state", ["proposed", "validated"])
def test_incomplete_report_rejects_completed_attempt_with_unfinished_action(
    tmp_path, action_state
):
    store, step_id, attempt_id = _open_action_store(tmp_path)
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
    if action_state == "validated":
        store.append(EventType.ACTION_POLICY_VALIDATED, payload)
    _complete_action_store(store, step_id, attempt_id)
    root = store.run_dir
    store.close()

    inspected = inspect_finalization_trust(root)
    assert inspected.trust_state is FinalizationTrustState.INVALID
    with pytest.raises(report_module.ReportError, match="record/relationship invariants"):
        render_reports(root)


def test_credential_resolver_process_cancellation_propagates(tmp_path):
    root = _finalized_package(tmp_path).run_dir
    key, credential, _resolver = _approval_credential()
    append_approval(
        root,
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
    )

    def cancelled(_key_id):
        raise KeyboardInterrupt("cancel review credential lookup")

    with pytest.raises(KeyboardInterrupt, match="cancel review credential lookup"):
        validate_approvals(root, key_resolver=cancelled)


def test_free_form_audit_details_are_sanitized_before_persistence(tmp_path):
    root = _finalized_package(tmp_path).run_dir
    secret = "Bearer TOP-SECRET-123"
    record = append_audit_event(
        root,
        "operator.note",
        actor="reviewer",
        details={"note": secret},
        dedupe_key="operator-note-raw",
    )
    assert record["details"]["note"]["disposition"] == "redacted"
    assert secret not in json.dumps(record, sort_keys=True)

    classified = append_audit_event(
        root,
        "operator.note",
        actor="reviewer",
        details={"note": EvidenceValue.safe("review-complete")},
        dedupe_key="operator-note-classified",
    )
    assert classified["details"]["note"]["value"] == "review-complete"
    validate_audit_chain(root)
    bundle = render_reports(root)
    for path in (bundle.json_path, bundle.markdown_path, bundle.html_path, bundle.junit_path):
        assert secret not in path.read_text("utf-8")


@pytest.mark.parametrize(
    "event_type,dedupe_key",
    [
        ("operator.note Bearer TOP-SECRET-123", "operator-note"),
        ("operator.note", "Bearer TOP-SECRET-123"),
    ],
)
def test_audit_outer_identifiers_reject_free_form_text_on_write(
    tmp_path, event_type, dedupe_key
):
    root = _finalized_package(tmp_path).run_dir
    before = (root / "audit.jsonl").read_bytes()
    with pytest.raises(ApprovalError, match="machine-safe"):
        append_audit_event(
            root,
            event_type,
            actor="reviewer",
            details={},
            dedupe_key=dedupe_key,
        )
    assert (root / "audit.jsonl").read_bytes() == before


def test_audit_actor_rejects_free_form_text_on_write(tmp_path):
    root = _finalized_package(tmp_path).run_dir
    before = (root / "audit.jsonl").read_bytes()
    with pytest.raises(ApprovalError, match="machine-safe principal"):
        append_audit_event(
            root,
            "operator.note",
            actor="Bearer TOP-SECRET-123",
            details={},
            dedupe_key="operator-note",
        )
    assert (root / "audit.jsonl").read_bytes() == before


@pytest.mark.parametrize("field", ["event_type", "dedupe_key", "actor"])
def test_imported_audit_outer_identifiers_cannot_bypass_report_privacy(tmp_path, field):
    root = _finalized_package(tmp_path).run_dir
    audit_path = root / "audit.jsonl"
    records = [json.loads(line) for line in audit_path.read_text("utf-8").splitlines()]
    assert len(records) == 1
    secret = "Bearer TOP-SECRET-123"
    records[0][field] = secret
    audit_path.write_bytes(_canonical_lines(records))

    with pytest.raises(ApprovalError, match="unsafe structural identifiers"):
        validate_audit_chain(root)

    bundle = render_reports(root)
    for path in (bundle.json_path, bundle.markdown_path, bundle.html_path, bundle.junit_path):
        assert secret not in path.read_text("utf-8")


@pytest.mark.parametrize("record_kind", ["observation", "action"])
def test_evidence_map_keys_cannot_bypass_privacy_policy(tmp_path, record_kind):
    store, step_id, attempt_id = _open_action_store(tmp_path)
    secret_key = "API_TOKEN=plaintext-secret"
    if record_kind == "observation":
        observation = ObservationRecord(
            observation_id=ObservationId.new(),
            step_attempt_id=attempt_id,
            source="fake",
            captured_at=datetime(2026, 9, 2, 5, 0, tzinfo=timezone.utc),
            capture_policy="test-evidence-v1",
            facts={secret_key: EvidenceValue.suppressed("privacy.secret")},
        )
        store.append(
            EventType.OBSERVATION_CAPTURED,
            {"observation": to_json_compatible(observation)},
        )
    else:
        action = ActionRecord(
            action_id=ActionId.new(),
            step_id=step_id,
            step_attempt_id=attempt_id,
            action_type="click",
            parameters={secret_key: EvidenceValue.suppressed("privacy.secret")},
            operation_id=ActionOperationId.new(),
        )
        store.append(
            EventType.ACTION_PROPOSED,
            {"action": to_json_compatible(action)},
        )
    _complete_action_store(store, step_id, attempt_id)
    try:
        with pytest.raises(FinalizationError, match="structural keys"):
            finalize_revision_one(store)
    finally:
        store.close()


def test_capsule_retention_must_postdate_settled_failure(tmp_path):
    run_id = RunId.new()
    step_id = StepId.new()
    attempt_id = StepAttemptId.new()
    step = StepRecord(
        step_id=step_id,
        instruction=EvidenceValue.redacted("privacy.authored_text"),
        kind="act",
    )
    run = _run_record_json(run_id)
    run["environment_type"] = "capsule"
    store = AtesEventStore(tmp_path, run_id)
    store.append(
        EventType.RUN_STARTED,
        {"run": run, "steps": [to_json_compatible(step)]},
    )
    store.append(
        EventType.ENVIRONMENT_PREPARED,
        {"environment_type": "capsule", "isolated": True},
    )
    store.append(EventType.TARGET_LAUNCHED, {"target": _target()})
    store.append(
        EventType.STEP_ATTEMPT_STARTED,
        _attempt_payload(step_id, attempt_id, "running", ended=False),
    )
    # This claim predates the failure it would otherwise be allowed to excuse.
    store.append(EventType.FAILURE_CAPSULE_RETAINED, {"retained": True})
    store.append(
        EventType.STEP_ATTEMPT_COMPLETED,
        _attempt_payload(step_id, attempt_id, "failed"),
    )
    store.append(EventType.ENVIRONMENT_RELEASED, {})
    store.append(
        EventType.RUN_MARKED_INCOMPLETE,
        {"reason": "runtime.finalization_pending", "execution_result": "fail"},
    )
    try:
        result = finalize_revision_one(store)
    finally:
        store.close()

    assert result.outcome.effective_status is RunStatus.ERROR
