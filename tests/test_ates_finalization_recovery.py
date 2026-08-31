"""ATES finalization recovery regression coverage."""
from __future__ import annotations

import json
import os

import pytest

import argus.ates.finalization as finalization_module
import argus.ates.finalization_io as finalization_io
from argus.ates import (
    AtesAppendError,
    AtesEventStore,
    EventType,
    FinalizationError,
    RunId,
    finalize_revision_one,
    recover_revision_one,
    verify_finalized_run,
)
from argus.ates.store import _run_directory_key
from tests.ates_test_support import _crash_after_evidence_manifest, _open_run_with_id
from tests.test_ates_finalization import _open_run

def test_run_json_status_edit_cannot_upgrade_failed_run(tmp_path):
    store = _open_run(tmp_path, step_status="failed")
    try:
        result = finalize_revision_one(store)
    finally:
        store.close()
    binding = json.loads(result.binding_path.read_text("utf-8"))
    binding["finalization"]["effective_status"] = "passed"
    result.binding_path.write_text(json.dumps(binding), encoding="utf-8")
    with pytest.raises(finalization_module.FinalizationError):
        verify_finalized_run(result.run_dir)


def test_recovery_after_evidence_manifest_only(tmp_path, monkeypatch):
    store = _open_run(tmp_path)
    run_id = store.run_id
    real_publish = finalization_module._publish

    def fail_package(directory, name, data):
        if name == "package-manifest-0001.json":
            raise RuntimeError("forced crash after evidence manifest")
        return real_publish(directory, name, data)

    monkeypatch.setattr(finalization_module, "_publish", fail_package)
    try:
        with pytest.raises(RuntimeError, match="forced crash"):
            finalize_revision_one(store)
    finally:
        store.close()
    monkeypatch.setattr(finalization_module, "_publish", real_publish)
    recovered = recover_revision_one(tmp_path, run_id)
    assert recovered.binding_path.exists()
    assert recovered.outcome.revision == 1


def test_recovery_after_package_before_completion(tmp_path, monkeypatch):
    store = _open_run(tmp_path)
    run_id = store.run_id

    def crash_before_completion(_event):
        raise RuntimeError("forced crash before completion")

    monkeypatch.setattr(store, "append_event", crash_before_completion)
    try:
        with pytest.raises(RuntimeError, match="forced crash"):
            finalize_revision_one(store)
    finally:
        store.close()
    recovered = recover_revision_one(tmp_path, run_id)
    assert recovered.binding_path.exists()
    assert recovered.outcome.revision == 1


def test_recovery_reconciles_durable_ambiguous_completion(tmp_path, monkeypatch):
    store = _open_run(tmp_path)
    run_id = store.run_id
    real_append = store.append_event

    def durable_then_ambiguous(event):
        real_append(event)
        raise AtesAppendError("forced ambiguous durable completion", event)

    monkeypatch.setattr(store, "append_event", durable_then_ambiguous)
    try:
        with pytest.raises(AtesAppendError):
            finalize_revision_one(store)
    finally:
        store.close()
    recovered = recover_revision_one(tmp_path, run_id)
    assert recovered.binding_path.exists()
    with AtesEventStore(tmp_path, run_id) as reopened:
        assert sum(e.envelope.event_type is EventType.RUN_COMPLETED for e in reopened.events) == 1


def test_recovery_after_completion_before_binding(tmp_path, monkeypatch):
    store = _open_run(tmp_path)
    run_id = store.run_id
    real_publish = finalization_module._publish

    def fail_binding(directory, name, data):
        if name == "run.json":
            raise RuntimeError("forced crash before binding")
        return real_publish(directory, name, data)

    monkeypatch.setattr(finalization_module, "_publish", fail_binding)
    try:
        with pytest.raises(RuntimeError, match="forced crash"):
            finalize_revision_one(store)
    finally:
        store.close()
    monkeypatch.setattr(finalization_module, "_publish", real_publish)
    recovered = recover_revision_one(tmp_path, run_id)
    assert recovered.binding_path.exists()


def test_recovery_is_idempotent_after_binding(tmp_path):
    store = _open_run(tmp_path)
    run_id = store.run_id
    try:
        first = finalize_revision_one(store)
    finally:
        store.close()
    second = recover_revision_one(tmp_path, run_id)
    assert second.outcome.finalization_id == first.outcome.finalization_id
    assert second.outcome.effective_status == first.outcome.effective_status


def test_recovery_of_absent_run_is_read_only(tmp_path):
    run_id = RunId.new()
    assert not (tmp_path / ".argus").exists()

    with pytest.raises(FinalizationError, match="absent ATES run"):
        recover_revision_one(tmp_path, run_id)

    assert not (tmp_path / ".argus").exists()


def test_pretty_printed_manifest_is_not_bound_verified(tmp_path):
    store = _open_run(tmp_path)
    try:
        result = finalize_revision_one(store)
    finally:
        store.close()

    manifest = json.loads(result.evidence_manifest_path.read_text("utf-8"))
    result.evidence_manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=False),
        encoding="utf-8",
    )
    with pytest.raises(FinalizationError, match="canonical persisted representation|digest"):
        verify_finalized_run(result.run_dir)


@pytest.mark.skipif(os.name == "nt", reason="POSIX hard-link substitution regression")
def test_publisher_rejects_hardlinked_final_member(tmp_path, monkeypatch):
    store = _open_run(tmp_path)
    real_publish = finalization_io._publish_no_overwrite

    def hardlinked_publish(directory, name, data):
        if name != "manifest-0001.json":
            return real_publish(directory, name, data)
        attacker = directory.path / ".attacker-source"
        attacker.write_bytes(data)
        os.link(attacker, directory.path / name)
        directory.fsync()
        return directory.path / name

    monkeypatch.setattr(
        finalization_io,
        "_publish_no_overwrite",
        hardlinked_publish,
    )
    try:
        with pytest.raises(FinalizationError):
            finalize_revision_one(store)
    finally:
        store.close()


@pytest.mark.skipif(os.name == "nt", reason="Windows pinned handle blocks directory replacement")
def test_publisher_rejects_manifests_directory_replacement(tmp_path, monkeypatch):
    store = _open_run(tmp_path)
    real_publish = finalization_io._publish_no_overwrite
    replaced = False

    def replacing_publish(directory, name, data):
        nonlocal replaced
        path = real_publish(directory, name, data)
        if name == "manifest-0001.json" and not replaced:
            replaced = True
            original = directory.path
            moved = original.with_name("manifests-displaced")
            os.rename(original, moved)
            original.mkdir()
        return path

    monkeypatch.setattr(
        finalization_io,
        "_publish_no_overwrite",
        replacing_publish,
    )
    try:
        with pytest.raises(
            FinalizationError,
            match="namespace|rollback is incomplete or ambiguous",
        ):
            finalize_revision_one(store)
    finally:
        store.close()


@pytest.mark.parametrize(
    ("member_name", "package_should_exist"),
    [
        ("manifest-0001.json", False),
        ("package-manifest-0001.json", True),
    ],
)
def test_recovery_rejects_reformatted_existing_member_before_mutation(
    tmp_path, monkeypatch, member_name, package_should_exist
):
    store = _open_run(tmp_path)
    run_id = store.run_id
    real_publish = finalization_module._publish

    def publish_then_crash(directory, name, data):
        path = real_publish(directory, name, data)
        if name == member_name:
            raise RuntimeError(f"forced crash after {member_name}")
        return path

    monkeypatch.setattr(finalization_module, "_publish", publish_then_crash)
    try:
        with pytest.raises(RuntimeError, match="forced crash"):
            finalize_revision_one(store)
    finally:
        store.close()

    run_dir = tmp_path / ".argus" / "runs" / str(run_id)
    manifests = run_dir / "manifests"
    target = manifests / member_name
    assert target.exists()
    assert (manifests / "package-manifest-0001.json").exists() is package_should_exist
    assert not (run_dir / "run.json").exists()

    with AtesEventStore(tmp_path, run_id) as reopened:
        before_count = len(reopened.events)
        assert not any(
            event.envelope.event_type is EventType.RUN_COMPLETED
            for event in reopened.events
        )

    parsed = json.loads(target.read_text("utf-8"))
    target.write_text(
        json.dumps(parsed, indent=2, sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(finalization_module, "_publish", real_publish)

    with pytest.raises(
        FinalizationError,
        match="canonical persisted representation|bytes differ from regenerated candidate",
    ):
        recover_revision_one(tmp_path, run_id)

    assert not (run_dir / "run.json").exists()
    with AtesEventStore(tmp_path, run_id) as reopened:
        assert len(reopened.events) == before_count
        assert not any(
            event.envelope.event_type is EventType.RUN_COMPLETED
            for event in reopened.events
        )
    if member_name == "manifest-0001.json":
        assert not (manifests / "package-manifest-0001.json").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX durable rollback regression")
def test_postpublication_verification_failure_surfaces_rollback_ambiguity(
    tmp_path, monkeypatch
):
    store = _open_run(tmp_path)
    real_pinned_bytes = finalization_io._pinned_bytes
    real_unlink = os.unlink
    injected_verification_failure = False

    def corrupt_postpublish_read(directory, name, label):
        nonlocal injected_verification_failure
        data = real_pinned_bytes(directory, name, label)
        if name == "manifest-0001.json" and not injected_verification_failure:
            injected_verification_failure = True
            return data + b"x"
        return data

    def fail_final_unlink(path, *args, **kwargs):
        if path == "manifest-0001.json" and kwargs.get("dir_fd") is not None:
            raise OSError("forced rollback unlink failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(
        finalization_io, "_pinned_bytes", corrupt_postpublish_read
    )
    monkeypatch.setattr(os, "unlink", fail_final_unlink)
    try:
        with pytest.raises(
            FinalizationError,
            match="rollback is incomplete or ambiguous",
        ):
            finalize_revision_one(store)
    finally:
        store.close()


def test_recovery_repairs_only_partial_completion_tail_before_reconciling(
    tmp_path,
    monkeypatch,
):
    run_id = RunId.new()
    store = _open_run_with_id(tmp_path, run_id)
    _crash_after_evidence_manifest(store, monkeypatch)

    run_dir = tmp_path / ".argus" / "runs" / _run_directory_key(run_id)
    manifest_path = run_dir / "manifests" / "manifest-0001.json"
    evidence_path = run_dir / "evidence.jsonl"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    _outcome, completion = finalization_module._candidate_from_manifest(
        manifest,
        run_id,
    )

    canonical_before = evidence_path.read_bytes()
    completion_line = completion.canonical_line()
    partial = completion_line[: max(1, len(completion_line) // 2)]
    assert not partial.endswith(b"\n")
    with evidence_path.open("ab") as handle:
        handle.write(partial)
        handle.flush()

    assert evidence_path.read_bytes() == canonical_before + partial

    recovered = recover_revision_one(tmp_path, run_id)
    assert recovered.binding_path.exists()
    assert recovered.package_manifest_path.exists()
    assert evidence_path.read_bytes() == canonical_before + completion_line

    with AtesEventStore(tmp_path, run_id) as reopened:
        completed = [
            event
            for event in reopened.events
            if event.envelope.event_type is EventType.RUN_COMPLETED
        ]
        assert len(completed) == 1
        assert completed[0].canonical_line() == completion_line


def test_bound_recovery_rejects_partial_tail_without_repairing_evidence(tmp_path):
    store = _open_run(tmp_path)
    run_id = store.run_id
    try:
        result = finalize_revision_one(store)
    finally:
        store.close()

    evidence = result.run_dir / "evidence.jsonl"
    canonical = evidence.read_bytes()
    partial_tail = b'{"post_finalization_tamper":true'
    assert not partial_tail.endswith(b"\n")
    with evidence.open("ab") as handle:
        handle.write(partial_tail)
        handle.flush()

    tampered = canonical + partial_tail
    assert evidence.read_bytes() == tampered
    assert result.binding_path.exists()

    with pytest.raises(
        FinalizationError,
        match="authoritative run state|verification|canonical evidence|trailing",
    ):
        recover_revision_one(tmp_path, run_id)

    # A bound package is immutable from the recovery API. Recovery must not
    # truncate/heal post-finalization corruption back to manifest-bound bytes.
    assert evidence.read_bytes() == tampered
    assert result.binding_path.exists()
