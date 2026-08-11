from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from argus.ates import EventType, RunId
from argus.ates.store import (
    AtesAppendError,
    AtesEventStore,
    AtesStoreCorruption,
    AtesStoreError,
)
import argus.ates.store as store_module


NOW = datetime(2026, 8, 11, 13, 0, tzinfo=timezone.utc)


def _run_dir(project: Path, run_id: RunId) -> Path:
    return (
        project
        / ".argus"
        / "runs"
        / store_module._run_directory_key(run_id)
    )


def test_hard_linked_evidence_file_is_rejected(tmp_path: Path):
    first_id = RunId("RUN-hardlink-one")
    second_id = RunId("RUN-hardlink-two")
    first_dir = _run_dir(tmp_path, first_id)
    second_dir = _run_dir(tmp_path, second_id)
    first_dir.mkdir(parents=True)
    second_dir.mkdir(parents=True)
    first_evidence = first_dir / "evidence.jsonl"
    second_evidence = second_dir / "evidence.jsonl"
    first_evidence.write_bytes(b"")
    try:
        os.link(first_evidence, second_evidence)
    except (OSError, NotImplementedError):
        pytest.skip("hard-link creation is unavailable on this filesystem")

    assert os.stat(first_evidence).st_nlink >= 2
    with pytest.raises(AtesStoreError, match="exactly one hard link"):
        AtesEventStore(tmp_path, first_id)
    with pytest.raises(AtesStoreError, match="exactly one hard link"):
        AtesEventStore(tmp_path, second_id)


def test_hard_link_added_after_open_is_rejected_before_append(tmp_path: Path):
    run_id = RunId("RUN-hardlink-late")
    store = AtesEventStore(tmp_path, run_id)
    alias = tmp_path / "late-alias.jsonl"
    try:
        try:
            os.link(store.path, alias)
        except (OSError, NotImplementedError):
            pytest.skip("hard-link creation is unavailable while the store is open")

        with pytest.raises(AtesStoreError, match="exactly one hard link"):
            store.append(EventType.RUN_STARTED, occurred_at=NOW)
        assert store.next_sequence == 1
        assert store.path.read_bytes() == b""
    finally:
        store.close()


def test_decode_recursion_is_classified_as_store_corruption(
    monkeypatch: pytest.MonkeyPatch,
):
    def fail_decode(*args, **kwargs):
        raise RecursionError("simulated decoder recursion")

    monkeypatch.setattr(store_module.json, "loads", fail_decode)
    with pytest.raises(AtesStoreCorruption, match="strict JSON"):
        store_module._decode_json_object(b"{}")


def test_deeply_nested_persisted_json_is_store_corruption(tmp_path: Path):
    run_id = RunId("RUN-deep-json")
    with AtesEventStore(tmp_path, run_id) as store:
        path = store.path

    # Different CPython versions can hit their recursion boundary in the JSON
    # decoder, immutable payload freezing, or canonical re-serialization. The
    # persisted-store contract is the same at every stage: fail as corruption.
    nested = '{"x":' * 3000 + "0" + "}" * 3000
    document = (
        "{"
        '"ates_version":"0.1",'
        '"event_id":"EVT-deep",'
        '"event_type":"RUN_STARTED",'
        f'"occurred_at":{json.dumps(NOW.isoformat())},'
        f'"payload":{nested},'
        f'"run_id":{json.dumps(str(run_id))},'
        '"sequence":1'
        "}\n"
    )
    path.write_bytes(document.encode("utf-8"))

    with pytest.raises(AtesStoreCorruption):
        AtesEventStore(tmp_path, run_id)


def test_keyboard_interrupt_after_write_poisons_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store = AtesEventStore(tmp_path, RunId("RUN-interrupt-append"))
    assert store._file is not None
    evidence_fd = store._file.fileno()
    real_fsync = store_module.os.fsync

    def interrupt_evidence_fsync(fd: int) -> None:
        if fd == evidence_fd:
            raise KeyboardInterrupt()
        real_fsync(fd)

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(store_module.os, "fsync", interrupt_evidence_fsync)
            with pytest.raises(KeyboardInterrupt):
                store.append(
                    EventType.RUN_STARTED,
                    {"interrupted": True},
                    occurred_at=NOW,
                    event_id="EVT-interrupted",
                )

        assert store.poisoned is True
        with pytest.raises(AtesStoreError, match="poisoned"):
            store.append(EventType.TARGET_LAUNCHED, occurred_at=NOW)
    finally:
        store.close()


def test_retry_after_initial_evidence_fsync_failure_resyncs_run_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    run_id = RunId("RUN-init-retry-sync")
    opened: dict[str, object] = {}
    original_open = store_module._open_regular_file
    real_fsync = store_module.os.fsync

    def tracked_open(directory, name):
        handle, created = original_open(directory, name)
        if name == "evidence.jsonl":
            opened["evidence"] = handle
        return handle, created

    def fail_evidence_fsync(fd: int) -> None:
        handle = opened.get("evidence")
        if handle is not None and not handle.closed and fd == handle.fileno():
            raise OSError("simulated initial evidence fsync failure")
        real_fsync(fd)

    with monkeypatch.context() as scoped:
        scoped.setattr(store_module, "_open_regular_file", tracked_open)
        scoped.setattr(store_module.os, "fsync", fail_evidence_fsync)
        with pytest.raises(OSError, match="initial evidence fsync failure"):
            AtesEventStore(tmp_path, run_id)

    evidence = _run_dir(tmp_path, run_id) / "evidence.jsonl"
    assert evidence.exists()

    sync_calls: list[Path] = []
    original_dir_fsync = store_module._PinnedDirectory.fsync

    def tracked_dir_fsync(directory: store_module._PinnedDirectory) -> None:
        sync_calls.append(directory.path)
        original_dir_fsync(directory)

    monkeypatch.setattr(store_module._PinnedDirectory, "fsync", tracked_dir_fsync)
    with AtesEventStore(tmp_path, run_id) as reopened:
        assert reopened.next_sequence == 1

    assert _run_dir(tmp_path, run_id) in sync_calls


def test_exact_replay_fsyncs_before_acknowledging_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    run_id = RunId("RUN-replay-sync")
    with AtesEventStore(tmp_path, run_id) as store:
        event = store.append(
            EventType.RUN_STARTED,
            {"stable": True},
            occurred_at=NOW,
            event_id="EVT-replay-sync",
        )

    with AtesEventStore(tmp_path, run_id) as reopened:
        assert reopened._file is not None
        evidence_fd = reopened._file.fileno()
        real_fsync = store_module.os.fsync
        calls = 0

        def tracked_fsync(fd: int) -> None:
            nonlocal calls
            if fd == evidence_fd:
                calls += 1
            real_fsync(fd)

        monkeypatch.setattr(store_module.os, "fsync", tracked_fsync)
        assert reopened.append_event(event) == event
        assert calls >= 1


def test_failed_replay_resync_keeps_durability_unknown_and_poisons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    run_id = RunId("RUN-replay-fsync-failure")
    with AtesEventStore(tmp_path, run_id) as store:
        event = store.append(
            EventType.RUN_STARTED,
            occurred_at=NOW,
            event_id="EVT-replay-failure",
        )

    reopened = AtesEventStore(tmp_path, run_id)
    assert reopened._file is not None
    evidence_fd = reopened._file.fileno()
    real_fsync = store_module.os.fsync

    def fail_replay_fsync(fd: int) -> None:
        if fd == evidence_fd:
            raise OSError("simulated replay fsync failure")
        real_fsync(fd)

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(store_module.os, "fsync", fail_replay_fsync)
            with pytest.raises(AtesAppendError) as caught:
                reopened.append_event(event)
        assert caught.value.event == event
        assert caught.value.outcome_unknown is True
        assert reopened.poisoned is True
    finally:
        reopened.close()


def test_keyboard_interrupt_during_replay_resync_poisons_and_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    run_id = RunId("RUN-replay-interrupt")
    with AtesEventStore(tmp_path, run_id) as store:
        event = store.append(
            EventType.RUN_STARTED,
            occurred_at=NOW,
            event_id="EVT-replay-interrupt",
        )

    reopened = AtesEventStore(tmp_path, run_id)
    assert reopened._file is not None
    evidence_fd = reopened._file.fileno()
    real_fsync = store_module.os.fsync

    def interrupt_replay_fsync(fd: int) -> None:
        if fd == evidence_fd:
            raise KeyboardInterrupt()
        real_fsync(fd)

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(store_module.os, "fsync", interrupt_replay_fsync)
            with pytest.raises(KeyboardInterrupt):
                reopened.append_event(event)
        assert reopened.poisoned is True
    finally:
        reopened.close()
