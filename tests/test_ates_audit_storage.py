"""ATES audit storage regression coverage."""
from __future__ import annotations

import json
import multiprocessing
import os
import shutil
from copy import deepcopy

import pytest

import argus.ates.audit as audit_module
from argus.ates import (
    ApprovalError,
    append_approval,
    append_audit_event,
    finalize_revision_one,
    render_reports,
    validate_approvals,
    validate_audit_chain,
)
from tests.ates_test_support import (
    _approval_credential,
    _audit_worker,
    _canonical_lines,
    _finalize_and_recover,
    _finalized_package,
)
from tests.test_ates_finalization import _open_run

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

    # Write exact canonical JSONL bytes. Path.write_text translates newlines on
    # Windows, which would correctly trip the representation check before this
    # test reaches the schema-field validator it is intended to exercise.
    path.write_bytes(
        (
            json.dumps(
                record,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")
    )

    with pytest.raises(ApprovalError, match=f"audit record 1.*{mutation}"):
        validate_audit_chain(root)


def test_audit_chain_rejects_and_reports_omit_unexpected_fields(tmp_path):
    root = _finalized_package(tmp_path).run_dir
    path = root / "audit.jsonl"
    records = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
    assert len(records) == 1
    records[0]["debug_note"] = "API_TOKEN=plaintext-secret"
    path.write_bytes(_canonical_lines(records))

    with pytest.raises(ApprovalError, match="unexpected=debug_note"):
        validate_audit_chain(root)

    bundle = render_reports(root)
    for report_path in (
        bundle.json_path,
        bundle.markdown_path,
        bundle.html_path,
        bundle.junit_path,
    ):
        assert "API_TOKEN=plaintext-secret" not in report_path.read_text("utf-8")


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
    duplicate["previous_record_digest"] = audit_module._audit_digest(prior)
    records.append(duplicate)
    path.write_bytes(_canonical_lines(records))

    with pytest.raises(ApprovalError, match="duplicate dedupe_key"):
        validate_audit_chain(root)


@pytest.mark.parametrize(
    "stored,retried",
    [
        ({"enabled": True}, {"enabled": 1}),
        ({"enabled": 1}, {"enabled": True}),
        ({"enabled": False}, {"enabled": 0}),
        ({"items": [{"enabled": True}]}, {"items": [{"enabled": 1}]}),
    ],
)
def test_audit_dedupe_distinguishes_json_boolean_and_number(tmp_path, stored, retried):
    root = _finalized_package(tmp_path).run_dir
    append_audit_event(
        root, "policy.changed", actor="reviewer", details=stored, dedupe_key="policy-change"
    )
    before = (root / "audit.jsonl").read_bytes()
    with pytest.raises(ApprovalError, match="audit dedupe conflict"):
        append_audit_event(
            root, "policy.changed", actor="reviewer", details=retried, dedupe_key="policy-change"
        )
    assert (root / "audit.jsonl").read_bytes() == before


def test_audit_dedupe_accepts_equivalent_reordered_json_objects(tmp_path):
    root = _finalized_package(tmp_path).run_dir
    original = append_audit_event(
        root,
        "policy.changed",
        actor="reviewer",
        details={"enabled": True, "policy": {"a": 1, "b": [False, None]}},
        dedupe_key="policy-change",
    )
    before = (root / "audit.jsonl").read_bytes()
    retried = append_audit_event(
        root,
        "policy.changed",
        actor="reviewer",
        details={"policy": {"b": [False, None], "a": 1}, "enabled": True},
        dedupe_key="policy-change",
    )
    assert retried == original
    assert (root / "audit.jsonl").read_bytes() == before


def test_audit_dedupe_and_append_reject_corrupt_existing_hash_chain(tmp_path):
    root = _finalized_package(tmp_path).run_dir
    path = root / "audit.jsonl"
    records = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
    existing = records[0]
    records[0]["previous_record_digest"] = "sha256:" + "0" * 64
    path.write_bytes(_canonical_lines(records))
    before = path.read_bytes()

    with pytest.raises(ApprovalError, match="breaks the append hash chain"):
        append_audit_event(
            root,
            existing["event_type"],
            actor=existing["actor"],
            details=existing["details"],
            dedupe_key=existing["dedupe_key"],
        )
    assert path.read_bytes() == before

    with pytest.raises(ApprovalError, match="breaks the append hash chain"):
        append_audit_event(
            root,
            "restore.completed",
            actor="reviewer",
            details={"restored": True},
            dedupe_key="restore-completed",
        )

    assert path.read_bytes() == before


def test_approval_append_rejects_corrupt_existing_audit_chain_without_mutation(tmp_path):
    root = _finalized_package(tmp_path).run_dir
    audit_path = root / "audit.jsonl"
    approval_path = root / "approvals.jsonl"
    records = [json.loads(line) for line in audit_path.read_text("utf-8").splitlines()]
    records[0]["previous_record_digest"] = "sha256:" + "0" * 64
    audit_path.write_bytes(_canonical_lines(records))
    audit_before = audit_path.read_bytes()
    approvals_before = approval_path.read_bytes()
    key, credential, _resolver = _approval_credential()

    with pytest.raises(ApprovalError, match="breaks the append hash chain"):
        append_approval(
            root,
            actor=credential.actor,
            role="test_reviewer",
            key_id=credential.key_id,
            authentication_key=key,
        )

    assert audit_path.read_bytes() == audit_before
    assert approval_path.read_bytes() == approvals_before


@pytest.mark.skipif(os.name == "nt", reason="open directory handles prevent replacement")
def test_audit_transaction_rejects_run_directory_replacement(tmp_path, monkeypatch):
    root = _finalized_package(tmp_path).run_dir
    before = (root / "audit.jsonl").read_bytes()
    displaced = root.with_name(root.name + ".displaced")
    original_read = audit_module._read_jsonl
    replaced = False

    def replace_before_read(root_arg, name):
        nonlocal replaced
        if not replaced and name == "audit.jsonl":
            replaced = True
            root.rename(displaced)
            shutil.copytree(displaced, root)
        return original_read(root_arg, name)

    monkeypatch.setattr(audit_module, "_read_jsonl", replace_before_read)

    with pytest.raises(ApprovalError, match="namespace|authoritative"):
        append_audit_event(
            root,
            "restore.completed",
            actor="reviewer",
            details={"restored": True},
            dedupe_key="restore-completed",
        )

    assert replaced is True
    assert (root / "audit.jsonl").read_bytes() == before
    assert all(
        record.get("event_type") != "restore.completed"
        for record in validate_audit_chain(root)
    )


def test_concurrent_audit_transactions_keep_one_valid_hash_chain(tmp_path):
    result = _finalize_and_recover(_open_run(tmp_path), tmp_path)
    root = result.run_dir
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Event()
    queue = ctx.Queue()
    workers = [
        ctx.Process(target=_audit_worker, args=(str(root), index, start, queue))
        for index in range(4)
    ]
    for worker in workers:
        worker.start()
    start.set()
    messages = [queue.get(timeout=15) for _ in workers]
    for worker in workers:
        worker.join(timeout=15)
        assert worker.exitcode == 0
    assert all(kind == "ok" for kind, _value in messages), messages

    records = validate_audit_chain(root)
    concurrent = [
        record for record in records if record.get("event_type") == "round6.concurrent"
    ]
    assert len(concurrent) == 4
    assert len({record["dedupe_key"] for record in concurrent}) == 4
