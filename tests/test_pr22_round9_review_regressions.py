from __future__ import annotations

from argus.ates import (
    ApprovalCredential,
    append_approval,
    complete_run_package,
    finalize_revision_one,
    revoke_approval,
    validate_approvals,
    validate_audit_chain,
)
from tests.test_ates_finalization import _open_run


def _finalized_package(tmp_path):
    store = _open_run(tmp_path)
    try:
        finalization = finalize_revision_one(store)
    finally:
        store.close()
    complete_run_package(finalization)
    return finalization


def test_reapproval_after_verified_revoke_starts_new_request_generation(tmp_path):
    finalization = _finalized_package(tmp_path)
    root = finalization.run_dir

    key = b"0123456789abcdef0123456789abcdef"
    credential = ApprovalCredential(
        key_id="round9-reviewer-key",
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
