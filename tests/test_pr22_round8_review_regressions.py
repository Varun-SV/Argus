from __future__ import annotations

import json
import os

import pytest

from argus.ates import (
    ApprovalCredential,
    ApprovalError,
    FinalizationTrustState,
    append_approval,
    append_audit_event,
    complete_run_package,
    finalize_revision_one,
    render_reports,
    revoke_approval,
    validate_approvals,
    validate_audit_chain,
    verify_report_bundle,
)
from argus.ates import audit_round2
from tests.test_ates_finalization import _open_run


def _finalize_and_complete(tmp_path):
    store = _open_run(tmp_path)
    try:
        finalization = finalize_revision_one(store)
    finally:
        store.close()
    package = complete_run_package(finalization)
    return finalization, package


def _credential():
    key = b"0123456789abcdef0123456789abcdef"
    credential = ApprovalCredential(
        key_id="round8-reviewer-key",
        key=key,
        actor="reviewer@example.invalid",
        roles=("test_reviewer",),
    )
    resolver = lambda key_id: credential if key_id == credential.key_id else None
    return key, credential, resolver


def _report_model(root):
    return json.loads((root / "reports" / "report.json").read_text("utf-8"))


def _assert_report_is_self_described_as_derived(root):
    model = _report_model(root)
    assert model["report_trust_state"] == FinalizationTrustState.UNVERIFIED_DERIVED.value
    snapshot = model["source"]["detached_ledger_snapshot"]
    assert snapshot == model["detached_ledger_snapshot"]
    by_path = {item["path"]: item for item in snapshot["members"]}
    assert set(by_path) == {"approvals.jsonl", "audit.jsonl"}
    for name, meta in by_path.items():
        raw = (root / name).read_bytes()
        assert meta["size_bytes"] == len(raw)
        assert meta["sha256"].startswith("sha256:")
    return model


def test_detached_ledger_mutations_never_leave_reports_self_attesting_freshness(tmp_path):
    _finalization, package = _finalize_and_complete(tmp_path)
    root = package.finalization.run_dir
    key, credential, resolver = _credential()

    initial = _assert_report_is_self_described_as_derived(root)
    initial_bytes = (root / "reports" / "report.json").read_bytes()

    approval = append_approval(
        root,
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
    )
    # Detached-ledger commits do not rewrite a report behind the reader's back;
    # the existing file remains explicitly derived and exposes its old snapshot.
    assert (root / "reports" / "report.json").read_bytes() == initial_bytes
    stale_after_approval = _report_model(root)
    assert stale_after_approval["report_trust_state"] == FinalizationTrustState.UNVERIFIED_DERIVED.value
    assert stale_after_approval["detached_ledger_snapshot"] == initial["detached_ledger_snapshot"]
    assert verify_report_bundle(root, approval_key_resolver=resolver).trust_state is FinalizationTrustState.INVALID

    render_reports(root, approval_key_resolver=resolver)
    assert verify_report_bundle(root, approval_key_resolver=resolver).trust_state is FinalizationTrustState.REGENERATED_VERIFIED
    before_revoke = (root / "reports" / "report.json").read_bytes()
    _assert_report_is_self_described_as_derived(root)

    revoke_approval(
        root,
        approval["approval_id"],
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
    )
    assert (root / "reports" / "report.json").read_bytes() == before_revoke
    assert _report_model(root)["report_trust_state"] == FinalizationTrustState.UNVERIFIED_DERIVED.value
    assert verify_report_bundle(root, approval_key_resolver=resolver).trust_state is FinalizationTrustState.INVALID

    render_reports(root, approval_key_resolver=resolver)
    assert verify_report_bundle(root, approval_key_resolver=resolver).trust_state is FinalizationTrustState.REGENERATED_VERIFIED
    before_audit = (root / "reports" / "report.json").read_bytes()

    append_audit_event(
        root,
        "round8.detached-change",
        actor="round8-test",
        details={"reason": "prove report snapshot staleness is visible"},
        dedupe_key="round8:detached-change",
    )
    assert (root / "reports" / "report.json").read_bytes() == before_audit
    assert _report_model(root)["report_trust_state"] == FinalizationTrustState.UNVERIFIED_DERIVED.value
    assert verify_report_bundle(root, approval_key_resolver=resolver).trust_state is FinalizationTrustState.INVALID


def _partial_append_then_raise(target_name: str, original):
    injected = {"done": False}

    def writer(pin, lock, name, line):
        if name == target_name and not injected["done"]:
            injected["done"] = True
            lock.assert_authoritative()
            handle, _created = audit_round2._open_regular_file(pin, name)
            try:
                handle.seek(0, os.SEEK_END)
                # Canonical ledger records contain no embedded newlines, so this
                # is guaranteed to leave an unterminated final record.
                partial = line[: max(1, (len(line) - 1) // 2)]
                assert partial and not partial.endswith(b"\n")
                assert handle.write(partial) == len(partial)
                handle.flush()
                os.fsync(handle.fileno())
                pin.assert_file_identity(name, handle.fileno(), f"injected partial {name}")
            finally:
                handle.close()
            raise ApprovalError(f"injected partial {name} append")
        return original(pin, lock, name, line)

    return writer


def test_partial_approval_line_is_repaired_only_by_writer_retry(tmp_path, monkeypatch):
    _finalization, package = _finalize_and_complete(tmp_path)
    root = package.finalization.run_dir
    key, credential, resolver = _credential()

    original = audit_round2._append_line_held
    monkeypatch.setattr(
        audit_round2,
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

    monkeypatch.setattr(audit_round2, "_append_line_held", original)
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
    key, credential, resolver = _credential()

    original = audit_round2._append_line_held
    monkeypatch.setattr(
        audit_round2,
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

    monkeypatch.setattr(audit_round2, "_append_line_held", original)
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
