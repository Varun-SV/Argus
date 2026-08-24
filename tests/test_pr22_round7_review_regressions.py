from __future__ import annotations

import json

import pytest

from argus.ates import (
    ApprovalCredential,
    ApprovalError,
    PackageCompletionError,
    append_approval,
    append_audit_event,
    complete_run_package,
    ensure_detached_ledgers,
    finalize_revision_one,
    validate_approvals,
    validate_audit_chain,
)
from argus.ates import audit_round2
from tests.test_ates_finalization import _open_run


def _finalize_without_package(store):
    try:
        return finalize_revision_one(store)
    finally:
        store.close()


def test_finalization_audit_dedupe_collision_fails_package_completion(tmp_path):
    finalization = _finalize_without_package(_open_run(tmp_path))
    root = finalization.run_dir
    ensure_detached_ledgers(root)
    dedupe_key = f"finalization:{finalization.outcome.finalization_id}"

    poisoned = append_audit_event(
        root,
        "unrelated.event",
        actor="unrelated-actor",
        dedupe_key=dedupe_key,
        occurred_at=finalization.outcome.finalized_at,
        details={"purpose": "occupy another operation's dedupe key"},
    )

    with pytest.raises(PackageCompletionError, match="audit dedupe conflict"):
        complete_run_package(finalization)

    records = validate_audit_chain(root)
    same_key = [record for record in records if record.get("dedupe_key") == dedupe_key]
    assert same_key == [poisoned]
    assert same_key[0]["event_type"] == "unrelated.event"
    assert not any(
        record.get("event_type") == "finalization.bound"
        and record.get("dedupe_key") == dedupe_key
        for record in records
    )
    assert not (root / "reports" / "report.json").exists()


def test_committed_but_unacknowledged_approval_retry_returns_original(tmp_path, monkeypatch):
    finalization = _finalize_without_package(_open_run(tmp_path))
    complete_run_package(finalization)
    root = finalization.run_dir

    key = b"0123456789abcdef0123456789abcdef"
    credential = ApprovalCredential(
        key_id="round7-reviewer-key",
        key=key,
        actor="reviewer@example.invalid",
        roles=("test_reviewer",),
    )
    resolver = lambda key_id: credential if key_id == credential.key_id else None

    original = audit_round2._append_approval_audit
    injected = {"done": False}

    def append_then_raise(root_arg, pin, lock, approval, audit_records):
        record = original(root_arg, pin, lock, approval, audit_records)
        if not injected["done"]:
            injected["done"] = True
            raise ApprovalError("injected post-commit acknowledgement failure")
        return record

    monkeypatch.setattr(audit_round2, "_append_approval_audit", append_then_raise)
    with pytest.raises(ApprovalError, match="post-commit acknowledgement failure"):
        append_approval(
            root,
            actor=credential.actor,
            role="test_reviewer",
            key_id=credential.key_id,
            authentication_key=key,
        )

    approval_lines = (root / "approvals.jsonl").read_text("utf-8").splitlines()
    assert len(approval_lines) == 1
    committed = json.loads(approval_lines[0])
    original_id = committed["approval_id"]
    assert committed["request_id"].startswith("APRREQ-")

    # Both files are already durable despite the raised acknowledgement error,
    # so the first record is effective and retry must not append another one.
    committed_state = validate_approvals(root, key_resolver=resolver)
    assert [item.record["approval_id"] for item in committed_state.verified_approvals] == [
        original_id
    ]

    monkeypatch.setattr(audit_round2, "_append_approval_audit", original)
    retried = append_approval(
        root,
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
    )
    assert retried["approval_id"] == original_id
    assert retried["request_id"] == committed["request_id"]
    assert len((root / "approvals.jsonl").read_text("utf-8").splitlines()) == 1

    records = validate_audit_chain(root)
    approval_audits = [
        record
        for record in records
        if record.get("event_type") == "approval.changed"
        and isinstance(record.get("details"), dict)
        and record["details"].get("approval_id") == original_id
    ]
    assert len(approval_audits) == 1

    final_state = validate_approvals(root, key_resolver=resolver)
    assert [item.record["approval_id"] for item in final_state.verified_approvals] == [
        original_id
    ]
