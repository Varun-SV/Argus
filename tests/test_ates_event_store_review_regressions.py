from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from argus.ates import EventType, RunId
from argus.ates.store import (
    AtesEventStore,
    AtesStoreBusy,
    AtesStoreCorruption,
    AtesStoreError,
)
import argus.ates.store as store_module


NOW = datetime(2026, 8, 11, 12, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize("payload", [[], (), "", 0, False])
def test_append_rejects_falsy_non_object_payloads(tmp_path: Path, payload: object):
    with AtesEventStore(tmp_path, RunId("RUN-falsy-payload")) as store:
        with pytest.raises(ValueError, match="payload must be a JSON object"):
            store.append(
                EventType.RUN_STARTED,
                payload,  # type: ignore[arg-type]
                occurred_at=NOW,
            )


def test_append_does_not_default_a_falsy_invalid_timestamp(tmp_path: Path):
    with AtesEventStore(tmp_path, RunId("RUN-falsy-time")) as store:
        with pytest.raises(ValueError, match="timezone-aware datetime"):
            store.append(
                EventType.RUN_STARTED,
                occurred_at=0,  # type: ignore[arg-type]
            )


@pytest.mark.parametrize("repair", ["false", 1, 0, None])
def test_trailing_repair_requires_a_real_boolean(tmp_path: Path, repair: object):
    with pytest.raises(ValueError, match="repair_trailing_partial must be a boolean"):
        AtesEventStore(
            tmp_path,
            RunId("RUN-repair-flag"),
            repair_trailing_partial=repair,  # type: ignore[arg-type]
        )
    assert not (tmp_path / ".argus").exists()


def test_case_distinct_run_ids_use_distinct_filesystem_keys(tmp_path: Path):
    lower = AtesEventStore(tmp_path, RunId("RUN-abc"))
    upper = AtesEventStore(tmp_path, RunId("RUN-ABC"))
    try:
        assert lower.run_dir != upper.run_dir
        assert lower.run_dir.name == "RUN-abc"
        assert upper.run_dir.name == "RUN-_a_b_c"
        lower_event = lower.append(EventType.RUN_STARTED, occurred_at=NOW)
        upper_event = upper.append(EventType.RUN_STARTED, occurred_at=NOW)
        assert lower_event.run_id == RunId("RUN-abc")
        assert upper_event.run_id == RunId("RUN-ABC")
    finally:
        upper.close()
        lower.close()


def test_new_directory_entries_sync_each_parent_before_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    calls: list[Path] = []
    original = store_module._PinnedDirectory.fsync

    def tracked(directory: store_module._PinnedDirectory) -> None:
        calls.append(directory.path)
        original(directory)

    monkeypatch.setattr(store_module._PinnedDirectory, "fsync", tracked)
    with AtesEventStore(tmp_path, RunId("RUN-parent-sync")):
        pass

    project = tmp_path.resolve()
    assert project in calls
    assert project / ".argus" in calls
    assert project / ".argus" / "runs" in calls


def test_initialization_failure_closes_open_evidence_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    run_id = RunId("RUN-init-cleanup")
    run_dir = tmp_path / ".argus" / "runs" / str(run_id)
    run_dir.mkdir(parents=True)
    (run_dir / "evidence.jsonl").write_bytes(b"not-json\n")

    opened = {}
    original = store_module._open_regular_file

    def tracked(directory, name):
        handle, created = original(directory, name)
        if name == "evidence.jsonl":
            opened["handle"] = handle
        return handle, created

    monkeypatch.setattr(store_module, "_open_regular_file", tracked)
    with pytest.raises(AtesStoreCorruption, match="strict JSON"):
        AtesEventStore(tmp_path, run_id)

    assert opened["handle"].closed is True


def test_writer_lock_initialization_failure_closes_its_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    opened = {}
    original_open = store_module._open_regular_file
    real_fsync = store_module.os.fsync

    def tracked_open(directory, name):
        handle, created = original_open(directory, name)
        if name == ".ates-writer.lock":
            opened["handle"] = handle
        return handle, created

    def fail_lock_fsync(fd: int) -> None:
        handle = opened.get("handle")
        if handle is not None and not handle.closed and fd == handle.fileno():
            raise OSError("simulated lock fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(store_module, "_open_regular_file", tracked_open)
    monkeypatch.setattr(store_module.os, "fsync", fail_lock_fsync)

    with pytest.raises(AtesStoreError, match="initialize ATES writer lock"):
        AtesEventStore(tmp_path, RunId("RUN-lock-init-cleanup"))

    assert opened["handle"].closed is True


def test_unencodable_persisted_text_is_store_corruption(tmp_path: Path):
    run_id = RunId("RUN-surrogate")
    with AtesEventStore(tmp_path, run_id) as store:
        path = store.path

    document = {
        "ates_version": "0.1",
        "event_id": "EVT-surrogate",
        "event_type": "RUN_STARTED",
        "occurred_at": NOW.isoformat(),
        "payload": {"bad": "\ud800"},
        "run_id": str(run_id),
        "sequence": 1,
    }
    path.write_bytes(
        json.dumps(
            document,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )

    with pytest.raises(
        AtesStoreCorruption,
        match="record 1 cannot be canonicalized as ATES UTF-8 JSON",
    ):
        AtesEventStore(tmp_path, run_id)


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-relative open regression")
def test_pinned_directory_open_ignores_a_replacement_path(tmp_path: Path):
    original_path = tmp_path / "run"
    moved_path = tmp_path / "run-moved"
    outside = tmp_path / "outside"
    original_path.mkdir()
    outside.mkdir()

    pinned = store_module._PinnedDirectory(original_path)
    try:
        original_path.rename(moved_path)
        original_path.symlink_to(outside, target_is_directory=True)
        handle, created = store_module._open_regular_file(pinned, "probe.jsonl")
        try:
            assert created is True
            handle.write(b"safe\n")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
    finally:
        pinned.close()

    assert (moved_path / "probe.jsonl").read_bytes() == b"safe\n"
    assert not (outside / "probe.jsonl").exists()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork is unavailable")
def test_inherited_store_is_rejected_without_unlocking_parent(tmp_path: Path):
    run_id = RunId("RUN-fork")
    store = AtesEventStore(tmp_path, run_id)
    try:
        pid = os.fork()
        if pid == 0:  # pragma: no cover - assertions are reported by exit status
            exit_code = 1
            try:
                try:
                    store.append(EventType.RUN_STARTED, occurred_at=NOW)
                except AtesStoreError as exc:
                    if "inherited across a fork" not in str(exc):
                        os._exit(2)
                else:
                    os._exit(3)
                store.close()
                exit_code = 0
            finally:
                os._exit(exit_code)

        _, status = os.waitpid(pid, 0)
        assert os.WIFEXITED(status)
        assert os.WEXITSTATUS(status) == 0

        # Child close must not issue LOCK_UN on the shared open-file description.
        with pytest.raises(AtesStoreBusy, match="authoritative ATES writer"):
            AtesEventStore(tmp_path, run_id)

        event = store.append(EventType.RUN_STARTED, occurred_at=NOW)
        assert event.sequence == 1
    finally:
        store.close()
