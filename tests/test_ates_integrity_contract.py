"""Regression coverage for ATES terminal, namespace, and authority invariants."""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

import argus.ates.audit as audit_module
import argus.ates.authority_guards as authority_guards
import argus.ates.finalization as finalization_module
import argus.ates.reports as report_module
from argus.ates import (
    AtesEventStore,
    EventType,
    EvidenceValue,
    FinalizationError,
    FindingId,
    FindingRecord,
    ObservationId,
    ObservationRecord,
    RunId,
    StepAttemptId,
    StepId,
    append_approval,
    finalize_revision_one,
    render_reports,
    to_json_compatible,
    validate_audit_chain,
    verify_finalized_run,
)
from tests.ates_test_support import (
    _approval_credential,
    _canonical_lines,
    _finalized_package,
)
from tests.test_ates_finalization import _run_record_json


def _target():
    return to_json_compatible(EvidenceValue.redacted("privacy.target_value"))


def _canonical_store(tmp_path, *, step_kind="step", marker=True):
    run_id = RunId.new()
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
                    "kind": step_kind,
                    "instruction": to_json_compatible(
                        EvidenceValue.redacted("privacy.authored_text")
                    ),
                }
            ],
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
                "started_at": "2026-09-02T12:00:00+00:00",
                "ended_at": None,
                "retry_reason": None,
            }
        },
    )
    return store, step_id, attempt_id


def _finish_store(store, step_id, attempt_id, *, marker=True):
    store.append(
        EventType.STEP_ATTEMPT_COMPLETED,
        {
            "attempt": {
                "step_attempt_id": str(attempt_id),
                "step_id": str(step_id),
                "attempt": 1,
                "status": "passed",
                "started_at": "2026-09-02T12:00:00+00:00",
                "ended_at": "2026-09-02T12:00:01+00:00",
                "retry_reason": None,
            }
        },
    )
    store.append(EventType.TARGET_CLOSED, {})
    store.append(EventType.ENVIRONMENT_RELEASED, {})
    if marker:
        store.append(
            EventType.RUN_MARKED_INCOMPLETE,
            {"reason": "runtime.finalization_pending", "execution_result": "pass"},
        )


def test_full_finalization_requires_unique_terminal_producer_handoff(tmp_path):
    store, step_id, attempt_id = _canonical_store(tmp_path, marker=False)
    _finish_store(store, step_id, attempt_id, marker=False)
    store.append(
        EventType.RUN_MARKED_INCOMPLETE,
        {"reason": "runtime.finalization_pending", "execution_result": "pass"},
    )
    store.append(EventType.SEQUENCE_TOMBSTONE, {"reason": "producer.post_handoff"})
    try:
        with pytest.raises(FinalizationError, match="final pre-completion producer event"):
            finalize_revision_one(store)
    finally:
        store.close()


def test_full_finalization_rejects_duplicate_terminal_handoffs(tmp_path):
    store, step_id, attempt_id = _canonical_store(tmp_path, marker=False)
    _finish_store(store, step_id, attempt_id, marker=False)
    marker = {"reason": "runtime.finalization_pending", "execution_result": "pass"}
    store.append(EventType.RUN_MARKED_INCOMPLETE, marker)
    store.append(EventType.RUN_MARKED_INCOMPLETE, marker)
    try:
        with pytest.raises(FinalizationError, match="exactly one RUN_MARKED_INCOMPLETE"):
            finalize_revision_one(store)
    finally:
        store.close()


def test_scripted_step_kind_is_closed_to_runtime_vocabulary(tmp_path):
    store, step_id, attempt_id = _canonical_store(
        tmp_path, step_kind="assertion_but_not_assert"
    )
    _finish_store(store, step_id, attempt_id)
    try:
        with pytest.raises(FinalizationError):
            finalize_revision_one(store)
    finally:
        store.close()


@pytest.mark.parametrize("channel", ["observation_source", "finding_classification"])
def test_report_visible_structural_fields_reject_free_form_plaintext(tmp_path, channel):
    store, step_id, attempt_id = _canonical_store(tmp_path, marker=False)
    observation_id = ObservationId.new()
    observation = ObservationRecord(
        observation_id=observation_id,
        step_attempt_id=attempt_id,
        source=("Bearer TOP-SECRET-OBS" if channel == "observation_source" else "fake"),
        captured_at=datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc),
        capture_policy="test-evidence-v1",
        facts={},
    )
    store.append(
        EventType.OBSERVATION_CAPTURED,
        {"observation": to_json_compatible(observation)},
    )
    if channel == "finding_classification":
        finding = FindingRecord(
            finding_id=FindingId.new(),
            title=EvidenceValue.redacted("privacy.finding_title"),
            description=EvidenceValue.redacted("privacy.finding_description"),
            evidence_refs=(str(observation_id),),
            classification_source="Bearer TOP-SECRET-SOURCE",
            classification="unclassified",
        )
        store.append(
            EventType.FINDING_RECORDED,
            {"finding": to_json_compatible(finding)},
        )
    _finish_store(store, step_id, attempt_id)
    try:
        with pytest.raises(FinalizationError):
            finalize_revision_one(store)
    finally:
        store.close()


@pytest.mark.parametrize("field", ["revision", "evidence_revision"])
def test_finalization_revision_fields_reject_json_booleans(tmp_path, field):
    result = _finalized_package(tmp_path).finalization
    document = to_json_compatible(result.outcome)
    document[field] = True
    with pytest.raises(FinalizationError):
        finalization_module._outcome(document)


def test_matching_approval_candidate_cannot_invent_generation_history(tmp_path):
    root = _finalized_package(tmp_path).run_dir
    key, credential, _resolver = _approval_credential()
    approval = append_approval(
        root,
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
    )
    approval_path = root / "approvals.jsonl"
    audit_path = root / "audit.jsonl"
    approvals = [
        json.loads(line) for line in approval_path.read_text("utf-8").splitlines()
    ]
    approvals[0]["request_generation_after_approval_id"] = (
        "APPROVAL-00000000000000000000000000000000"
    )
    approvals[0]["authentication"]["signature"] = audit_module._sign_record(
        approvals[0], key
    )
    approval_path.write_bytes(_canonical_lines(approvals))

    audits = [json.loads(line) for line in audit_path.read_text("utf-8").splitlines()]
    audits = [
        row
        for row in audits
        if not (
            row.get("event_type") == "approval.changed"
            and isinstance(row.get("details"), dict)
            and row["details"].get("approval_id") == approval["approval_id"]
        )
    ]
    audit_path.write_bytes(_canonical_lines(audits))
    validate_audit_chain(root)
    approvals_before = approval_path.read_bytes()
    audit_before = audit_path.read_bytes()

    with pytest.raises(Exception):
        append_approval(
            root,
            actor=credential.actor,
            role="test_reviewer",
            key_id=credential.key_id,
            authentication_key=key,
        )

    assert approval_path.read_bytes() == approvals_before
    assert audit_path.read_bytes() == audit_before


@pytest.mark.skipif(os.name == "nt", reason="POSIX rename/restore ABA regression")
def test_final_verifier_rejects_aba_run_directory_replacement(tmp_path, monkeypatch):
    first = _finalized_package(tmp_path).run_dir
    second = _finalized_package(tmp_path).run_dir
    displaced = first.parent / (first.name + ".displaced")
    real = authority_guards._preflight_bound_members_with_snapshot
    swapped = False

    def replace_before_bound_preflight(root, snapshot, finalization, fio, store_module):
        nonlocal swapped
        if not swapped and Path(root) == first:
            swapped = True
            first.rename(displaced)
            second.rename(first)
            try:
                return real(root, snapshot, finalization, fio, store_module)
            finally:
                first.rename(second)
                displaced.rename(first)
        return real(root, snapshot, finalization, fio, store_module)

    monkeypatch.setattr(
        authority_guards,
        "_preflight_bound_members_with_snapshot",
        replace_before_bound_preflight,
    )
    with pytest.raises(FinalizationError, match="namespace identity changed"):
        verify_finalized_run(first)
    assert first.exists() and second.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX rename/restore ABA regression")
def test_report_render_rejects_replacement_even_when_clone_has_same_run_id(
    tmp_path, monkeypatch
):
    root = _finalized_package(tmp_path).run_dir
    replacement = root.parent / (root.name + ".replacement")
    displaced = root.parent / (root.name + ".displaced")
    shutil.copytree(root, replacement)
    real_model = report_module._model
    attempted = False

    def aba_model(run_root, resolver):
        nonlocal attempted
        if not attempted:
            attempted = True
            root.rename(displaced)
            replacement.rename(root)
            try:
                return real_model(run_root, resolver)
            finally:
                root.rename(replacement)
                displaced.rename(root)
        return real_model(run_root, resolver)

    monkeypatch.setattr(report_module, "_model", aba_model)
    with pytest.raises((FinalizationError, report_module.ReportError)):
        render_reports(root)
    assert attempted
    assert root.exists()
