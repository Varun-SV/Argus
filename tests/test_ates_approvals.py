"""ATES approvals regression coverage."""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone

import pytest

import argus.ates.audit as audit_module
from argus.ates import (
    ApprovalCredential,
    ApprovalError,
    EvidenceValue,
    VerificationStatus,
    append_approval,
    complete_run_package,
    revoke_approval,
    validate_approvals,
    validate_audit_chain,
)
from tests.ates_test_support import (
    _approval_credential,
    _canonical_lines,
    _finalize_and_complete,
    _finalize_and_recover,
    _finalize_without_package,
    _finalized_package,
    _invalidate_supersession,
    _partial_append_then_raise,
)
from tests.test_ates_finalization import _open_run

def test_invalid_superseder_cannot_advance_approval_generation(tmp_path):
    finalization = _finalized_package(tmp_path)
    root = finalization.run_dir
    key, credential, resolver = _approval_credential()

    first = append_approval(
        root,
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
    )

    # Same claimed identity/key_id, but the revoke is signed by the wrong key.
    wrong_key = b"fedcba9876543210fedcba9876543210"
    revoke_approval(
        root,
        first["approval_id"],
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=wrong_key,
    )

    retried = append_approval(
        root,
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
    )
    assert retried["approval_id"] == first["approval_id"]
    assert retried["request_id"] == first["request_id"]

    state = validate_approvals(root, key_resolver=resolver)
    assert [item.record["approval_id"] for item in state.verified_approvals] == [
        first["approval_id"]
    ]
    # The invalid revoke remains visible for audit, but it cannot manufacture a
    # second effective approval generation.
    assert len((root / "approvals.jsonl").read_text("utf-8").splitlines()) == 2


def test_explicit_approval_timestamps_are_distinct_request_identity(tmp_path):
    finalization = _finalized_package(tmp_path)
    root = finalization.run_dir
    key, credential, resolver = _approval_credential()
    first_when = datetime(2026, 8, 28, 1, 2, 3, tzinfo=timezone.utc)
    second_when = datetime(2026, 8, 28, 1, 2, 4, tzinfo=timezone.utc)

    first = append_approval(
        root,
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
        occurred_at=first_when,
    )
    second = append_approval(
        root,
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
        occurred_at=second_when,
    )

    assert second["approval_id"] != first["approval_id"]
    assert second["request_id"] != first["request_id"]
    assert first["occurred_at"] == first_when.isoformat()
    assert second["occurred_at"] == second_when.isoformat()

    retried_second = append_approval(
        root,
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
        occurred_at=second_when,
    )
    assert retried_second["approval_id"] == second["approval_id"]

    state = validate_approvals(root, key_resolver=resolver)
    assert [item.record["approval_id"] for item in state.verified_approvals] == [
        first["approval_id"],
        second["approval_id"],
    ]


def test_signed_audit_bound_approval_without_timestamp_is_not_effective(tmp_path):
    finalization = _finalized_package(tmp_path)
    root = finalization.run_dir
    key, credential, resolver = _approval_credential()
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
    malformed["authentication"]["signature"] = audit_module._sign_record(malformed, key)
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
    matching[0]["details"]["approval_record_digest"] = audit_module._approval_digest(
        malformed
    )
    audit_path.write_bytes(_canonical_lines(audit_records))

    # The malformed row is still correctly signed by the trusted reviewer and
    # has an exact approval.changed digest binding; chronology alone must keep
    # it from becoming effective.
    auth_status, _ = audit_module._authentication_status(malformed, resolver)
    assert auth_status is VerificationStatus.VERIFIED
    state = validate_approvals(root, key_resolver=resolver)
    assert len(state.records) == 1
    assert state.records[0].verification_status is VerificationStatus.INVALID
    assert "occurred_at" in (state.records[0].reason or "")
    assert state.effective_approval_ids == ()


def test_structurally_stale_superseder_cannot_advance_request_generation(tmp_path):
    finalization = _finalized_package(tmp_path)
    root = finalization.run_dir
    key, credential, resolver = _approval_credential()

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
    malformed["authentication"]["signature"] = audit_module._sign_record(malformed, key)
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
    matching[0]["details"]["approval_record_digest"] = audit_module._approval_digest(
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


def test_signed_audit_bound_approval_with_plaintext_reason_and_extension_is_invalid(tmp_path):
    finalization = _finalized_package(tmp_path)
    root = finalization.run_dir
    key = b"0123456789abcdef0123456789abcdef"
    credential = ApprovalCredential(
        key_id="test-reviewer-key",
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
    malformed["authentication"]["signature"] = audit_module._sign_record(malformed, key)
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
    matching[0]["details"]["approval_record_digest"] = audit_module._approval_digest(malformed)
    audit_path.write_bytes(_canonical_lines(audits))

    state = validate_approvals(root, key_resolver=resolver)
    record = next(item for item in state.records if item.record.get("approval_id") == approval["approval_id"])
    assert record.effective is False
    assert record.verification_status.value == "invalid"


@pytest.mark.parametrize("relationship", ["supersession", "generation"])
@pytest.mark.parametrize("invalid_target", ["missing", "self"])
def test_invalid_supersession_cannot_become_approval_history(
    tmp_path, relationship, invalid_target
):
    root = _finalized_package(tmp_path).run_dir
    key, credential, resolver = _approval_credential()
    kwargs = dict(
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
    )
    first = append_approval(root, **kwargs)
    if relationship == "generation":
        parent = revoke_approval(root, first["approval_id"], **kwargs)
        dependent = append_approval(root, **kwargs)
        assert dependent["request_generation_after_approval_id"] == parent["approval_id"]
    else:
        parent = first
        dependent = append_approval(
            root,
            supersedes_approval_id=parent["approval_id"],
            reason=EvidenceValue.safe("updated review"),
            **kwargs,
        )
    before = validate_approvals(root, key_resolver=resolver)
    assert dependent["approval_id"] in before.effective_approval_ids

    target = "APR-" + "0" * 32 if invalid_target == "missing" else parent["approval_id"]
    _invalidate_supersession(root, parent["approval_id"], target, key)

    after = validate_approvals(root, key_resolver=resolver)
    invalid_ids = {parent["approval_id"], dependent["approval_id"]}
    invalid_rows = [row for row in after.records if row.record["approval_id"] in invalid_ids]
    assert len(invalid_rows) == 2
    assert all(row.verification_status is VerificationStatus.INVALID for row in invalid_rows)
    assert all(not row.effective for row in invalid_rows)
    assert not invalid_ids.intersection(after.effective_approval_ids)


def test_failed_approval_audit_append_leaves_no_effective_approval_and_retry_repairs(
    tmp_path, monkeypatch
):
    result = _finalize_and_recover(_open_run(tmp_path), tmp_path)
    root = result.run_dir
    key = b"0123456789abcdef0123456789abcdef"
    credential = ApprovalCredential(
        key_id="test-reviewer-key",
        key=key,
        actor="reviewer@example.invalid",
        roles=("test_reviewer",),
    )
    resolver = lambda key_id: credential if key_id == credential.key_id else None

    original = audit_module._append_line_held
    failed = {"done": False}

    def fail_required_audit(pin, lock, name, line):
        if name == "audit.jsonl" and not failed["done"]:
            failed["done"] = True
            raise ApprovalError("injected audit append failure")
        return original(pin, lock, name, line)

    monkeypatch.setattr(audit_module, "_append_line_held", fail_required_audit)
    with pytest.raises(ApprovalError, match="injected audit append failure"):
        append_approval(
            root,
            actor=credential.actor,
            role="test_reviewer",
            key_id=credential.key_id,
            authentication_key=key,
        )

    pending_lines = (root / "approvals.jsonl").read_text("utf-8").splitlines()
    assert len(pending_lines) == 1
    pending_id = json.loads(pending_lines[0])["approval_id"]
    pending_state = validate_approvals(root, key_resolver=resolver)
    assert pending_state.verified_approvals == ()
    assert len(pending_state.records) == 1
    assert pending_state.records[0].effective is False
    assert "pending" in (pending_state.records[0].reason or "")

    monkeypatch.setattr(audit_module, "_append_line_held", original)
    repaired = append_approval(
        root,
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
    )
    assert repaired["approval_id"] == pending_id
    assert len((root / "approvals.jsonl").read_text("utf-8").splitlines()) == 1

    repaired_state = validate_approvals(root, key_resolver=resolver)
    assert [
        item.record["approval_id"] for item in repaired_state.verified_approvals
    ] == [pending_id]
    audit = validate_audit_chain(root)
    matching = [
        item
        for item in audit
        if item.get("event_type") == "approval.changed"
        and isinstance(item.get("details"), dict)
        and item["details"].get("approval_id") == pending_id
    ]
    assert len(matching) == 1


def test_committed_but_unacknowledged_approval_retry_returns_original(tmp_path, monkeypatch):
    finalization = _finalize_without_package(_open_run(tmp_path))
    complete_run_package(finalization)
    root = finalization.run_dir

    key = b"0123456789abcdef0123456789abcdef"
    credential = ApprovalCredential(
        key_id="test-reviewer-key",
        key=key,
        actor="reviewer@example.invalid",
        roles=("test_reviewer",),
    )
    resolver = lambda key_id: credential if key_id == credential.key_id else None

    original = audit_module._append_approval_audit
    injected = {"done": False}

    def append_then_raise(root_arg, pin, lock, approval, audit_records):
        record = original(root_arg, pin, lock, approval, audit_records)
        if not injected["done"]:
            injected["done"] = True
            raise ApprovalError("injected post-commit acknowledgement failure")
        return record

    monkeypatch.setattr(audit_module, "_append_approval_audit", append_then_raise)
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

    monkeypatch.setattr(audit_module, "_append_approval_audit", original)
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


def test_partial_approval_line_is_repaired_only_by_writer_retry(tmp_path, monkeypatch):
    _finalization, package = _finalize_and_complete(tmp_path)
    root = package.finalization.run_dir
    key, credential, resolver = _approval_credential()

    original = audit_module._append_line_held
    monkeypatch.setattr(
        audit_module,
        "_append_line_held",
        _partial_append_then_raise("approvals.jsonl", original),
    )
    with pytest.raises(ApprovalError, match="partial approvals.jsonl"):
        append_approval(
            root,
            actor=credential.actor,
            role="test_reviewer",
            key_id=credential.key_id,
            authentication_key=key,
        )

    assert not (root / "approvals.jsonl").read_bytes().endswith(b"\n")
    # Read-only validation is intentionally strict and must not heal bytes.
    with pytest.raises(ApprovalError):
        validate_approvals(root, key_resolver=resolver)

    monkeypatch.setattr(audit_module, "_append_line_held", original)
    repaired = append_approval(
        root,
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
    )
    assert repaired["request_id"].startswith("APRREQ-")
    raw = (root / "approvals.jsonl").read_bytes()
    assert raw.endswith(b"\n")
    assert len(raw.splitlines()) == 1
    state = validate_approvals(root, key_resolver=resolver)
    assert [item.record["approval_id"] for item in state.verified_approvals] == [
        repaired["approval_id"]
    ]


def test_partial_audit_line_repairs_pending_approval_and_preserves_request_identity(tmp_path, monkeypatch):
    _finalization, package = _finalize_and_complete(tmp_path)
    root = package.finalization.run_dir
    key, credential, resolver = _approval_credential()

    original = audit_module._append_line_held
    monkeypatch.setattr(
        audit_module,
        "_append_line_held",
        _partial_append_then_raise("audit.jsonl", original),
    )
    with pytest.raises(ApprovalError, match="partial audit.jsonl"):
        append_approval(
            root,
            actor=credential.actor,
            role="test_reviewer",
            key_id=credential.key_id,
            authentication_key=key,
        )

    approvals = (root / "approvals.jsonl").read_text("utf-8").splitlines()
    assert len(approvals) == 1
    durable = json.loads(approvals[0])
    original_id = durable["approval_id"]
    original_request_id = durable["request_id"]
    assert not (root / "audit.jsonl").read_bytes().endswith(b"\n")
    with pytest.raises(ApprovalError):
        validate_audit_chain(root)

    monkeypatch.setattr(audit_module, "_append_line_held", original)
    repaired = append_approval(
        root,
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
    )
    assert repaired["approval_id"] == original_id
    assert repaired["request_id"] == original_request_id
    assert len((root / "approvals.jsonl").read_text("utf-8").splitlines()) == 1

    audits = validate_audit_chain(root)
    matching = [
        item
        for item in audits
        if item.get("event_type") == "approval.changed"
        and isinstance(item.get("details"), dict)
        and item["details"].get("approval_id") == original_id
    ]
    assert len(matching) == 1
    state = validate_approvals(root, key_resolver=resolver)
    assert [item.record["approval_id"] for item in state.verified_approvals] == [
        original_id
    ]


def test_reapproval_after_verified_revoke_starts_new_request_generation(tmp_path):
    finalization = _finalized_package(tmp_path)
    root = finalization.run_dir

    key = b"0123456789abcdef0123456789abcdef"
    credential = ApprovalCredential(
        key_id="test-reviewer-key",
        key=key,
        actor="reviewer@example.invalid",
        roles=("test_reviewer",),
    )
    resolver = lambda key_id: credential if key_id == credential.key_id else None

    first = append_approval(
        root,
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
    )
    first_state = validate_approvals(root, key_resolver=resolver)
    assert [item.record["approval_id"] for item in first_state.verified_approvals] == [
        first["approval_id"]
    ]

    revoked = revoke_approval(
        root,
        first["approval_id"],
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
    )
    revoked_state = validate_approvals(root, key_resolver=resolver)
    assert revoked_state.verified_approvals == ()

    second = append_approval(
        root,
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
    )
    assert second["approval_id"] != first["approval_id"]
    assert second["request_id"] != first["request_id"]
    assert second["request_generation_after_approval_id"] == revoked["approval_id"]

    second_state = validate_approvals(root, key_resolver=resolver)
    assert [item.record["approval_id"] for item in second_state.verified_approvals] == [
        second["approval_id"]
    ]

    # A retry inside the new generation must still converge to the same durable
    # request rather than manufacturing a third approval.
    approval_bytes_before_retry = (root / "approvals.jsonl").read_bytes()
    audit_bytes_before_retry = (root / "audit.jsonl").read_bytes()
    retried = append_approval(
        root,
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
    )
    assert retried["approval_id"] == second["approval_id"]
    assert retried["request_id"] == second["request_id"]
    assert (root / "approvals.jsonl").read_bytes() == approval_bytes_before_retry
    assert (root / "audit.jsonl").read_bytes() == audit_bytes_before_retry

    approvals = (root / "approvals.jsonl").read_text("utf-8").splitlines()
    assert len(approvals) == 3
    audit = validate_audit_chain(root)
    approval_changes = [item for item in audit if item.get("event_type") == "approval.changed"]
    assert len(approval_changes) == 3


def test_authenticated_approval_supersession_and_revocation_are_append_only(tmp_path):
    result = _finalize_and_recover(_open_run(tmp_path), tmp_path)
    root = result.run_dir
    key = b"0123456789abcdef0123456789abcdef"
    credential = ApprovalCredential(
        key_id="reviewer-key-1",
        key=key,
        actor="reviewer@example.invalid",
        roles=("test_reviewer",),
    )
    resolver = lambda key_id: credential if key_id == credential.key_id else None

    first = append_approval(
        root,
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
    )
    ledger = root / "approvals.jsonl"
    first_bytes = ledger.read_bytes()
    state = validate_approvals(root, key_resolver=resolver)
    assert [item.record["approval_id"] for item in state.verified_approvals] == [first["approval_id"]]

    second = append_approval(
        root,
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
        supersedes_approval_id=first["approval_id"],
    )
    assert ledger.read_bytes().startswith(first_bytes)
    state = validate_approvals(root, key_resolver=resolver)
    assert [item.record["approval_id"] for item in state.verified_approvals] == [second["approval_id"]]

    before_revoke = ledger.read_bytes()
    revoke_approval(
        root,
        second["approval_id"],
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
    )
    assert ledger.read_bytes().startswith(before_revoke)
    state = validate_approvals(root, key_resolver=resolver)
    assert state.verified_approvals == ()
    assert len(state.records) == 3

    without_trust = validate_approvals(root)
    assert without_trust.verified_approvals == ()
    assert all(item.verification_status.value == "unverified" for item in without_trust.records)

    wrong_credential = ApprovalCredential(
        key_id="reviewer-key-1",
        key=b"fedcba9876543210fedcba9876543210",
        actor="reviewer@example.invalid",
        roles=("test_reviewer",),
    )
    invalid = validate_approvals(root, key_resolver=lambda _key_id: wrong_credential)
    assert invalid.verified_approvals == ()
    assert all(item.verification_status.value == "invalid" for item in invalid.records)

    audit = validate_audit_chain(root)
    assert sum(item["event_type"] == "approval.changed" for item in audit) == 3


def test_approval_validation_does_not_create_missing_ledger(tmp_path):
    result = _finalize_and_recover(_open_run(tmp_path), tmp_path)
    approvals = result.run_dir / "approvals.jsonl"
    approvals.unlink()
    assert not approvals.exists()
    validated = validate_approvals(result.run_dir)
    assert validated.records == ()
    assert not approvals.exists()
