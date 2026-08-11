from __future__ import annotations

import errno
import gc
import os
import weakref
from datetime import datetime, timezone
from pathlib import Path

import pytest

from argus.ates import EventType, RunId
from argus.ates.store import (
    AtesAppendError,
    AtesEventStore,
    AtesStoreBusy,
    AtesStoreError,
)
import argus.ates.store as store_module


NOW = datetime(2026, 8, 11, 18, 0, tzinfo=timezone.utc)


@pytest.mark.skipif(os.name == "nt", reason="Windows denies evidence rename while open")
def test_evidence_replacement_during_append_is_ambiguous_and_poisons(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    run_id = RunId("RUN-evidence-swap-append")
    store = AtesEventStore(tmp_path, run_id)
    assert store._file is not None
    evidence_fd = store._file.fileno()
    moved = store.path.with_name("evidence-moved.jsonl")
    real_fsync = store_module.os.fsync
    swapped = False

    def fsync_then_replace(fd: int) -> None:
        nonlocal swapped
        real_fsync(fd)
        if fd == evidence_fd and not swapped:
            swapped = True
            store.path.rename(moved)
            store.path.write_bytes(b"")

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(store_module.os, "fsync", fsync_then_replace)
            with pytest.raises(AtesAppendError) as caught:
                store.append(
                    EventType.RUN_STARTED,
                    {"swap": "append"},
                    occurred_at=NOW,
                    event_id="EVT-evidence-swap-append",
                )

        assert swapped is True
        assert caught.value.event.event_id == "EVT-evidence-swap-append"
        assert caught.value.outcome_unknown is True
        assert store.poisoned is True
        assert store.path.read_bytes() == b""
        assert b"EVT-evidence-swap-append" in moved.read_bytes()

        store.path.unlink()
        moved.rename(store.path)
    finally:
        if moved.exists():
            if store.path.exists():
                store.path.unlink()
            moved.rename(store.path)
        store.close()

    with AtesEventStore(tmp_path, run_id) as reopened:
        assert reopened.next_sequence == 2
        assert [str(event.event_id) for event in reopened.events] == [
            "EVT-evidence-swap-append"
        ]


@pytest.mark.skipif(os.name == "nt", reason="Windows denies evidence rename while open")
def test_evidence_replacement_during_replay_is_ambiguous_and_poisons(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    run_id = RunId("RUN-evidence-swap-replay")
    with AtesEventStore(tmp_path, run_id) as first:
        event = first.append(
            EventType.RUN_STARTED,
            occurred_at=NOW,
            event_id="EVT-evidence-swap-replay",
        )

    store = AtesEventStore(tmp_path, run_id)
    assert store._file is not None
    evidence_fd = store._file.fileno()
    moved = store.path.with_name("evidence-moved.jsonl")
    canonical_bytes = store.path.read_bytes()
    real_fsync = store_module.os.fsync
    swapped = False

    def fsync_then_replace(fd: int) -> None:
        nonlocal swapped
        real_fsync(fd)
        if fd == evidence_fd and not swapped:
            swapped = True
            store.path.rename(moved)
            store.path.write_bytes(canonical_bytes)

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(store_module.os, "fsync", fsync_then_replace)
            with pytest.raises(AtesAppendError) as caught:
                store.append_event(event)

        assert swapped is True
        assert caught.value.event == event
        assert caught.value.outcome_unknown is True
        assert store.poisoned is True

        store.path.unlink()
        moved.rename(store.path)
    finally:
        if moved.exists():
            if store.path.exists():
                store.path.unlink()
            moved.rename(store.path)
        store.close()

    with AtesEventStore(tmp_path, run_id) as reopened:
        assert reopened.next_sequence == 2


@pytest.mark.skipif(os.name == "nt", reason="Windows denies evidence rename while open")
def test_evidence_replacement_during_initial_open_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    run_id = RunId("RUN-evidence-swap-open")
    original_open = store_module._open_regular_file
    real_fsync = store_module.os.fsync
    evidence_handle = None
    evidence_path: Path | None = None
    moved: Path | None = None
    swapped = False

    def tracked_open(directory, name):
        nonlocal evidence_handle, evidence_path, moved
        handle, created = original_open(directory, name)
        if name == "evidence.jsonl":
            evidence_handle = handle
            evidence_path = directory.path / name
            moved = directory.path / "evidence-moved.jsonl"
        return handle, created

    def fsync_then_replace(fd: int) -> None:
        nonlocal swapped
        real_fsync(fd)
        if (
            evidence_handle is not None
            and not evidence_handle.closed
            and fd == evidence_handle.fileno()
            and not swapped
        ):
            assert evidence_path is not None
            assert moved is not None
            swapped = True
            evidence_path.rename(moved)
            evidence_path.write_bytes(b"")

    with monkeypatch.context() as scoped:
        scoped.setattr(store_module, "_open_regular_file", tracked_open)
        scoped.setattr(store_module.os, "fsync", fsync_then_replace)
        with pytest.raises(AtesStoreError, match="namespace no longer refers"):
            AtesEventStore(tmp_path, run_id)

    assert swapped is True
    assert evidence_path is not None
    assert moved is not None
    evidence_path.unlink()
    moved.rename(evidence_path)

    with AtesEventStore(tmp_path, run_id) as reopened:
        assert reopened.next_sequence == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock error classification")
def test_noncontention_flock_failure_is_operational_store_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import fcntl

    real_flock = fcntl.flock
    failed = False

    def fail_first_acquire(fd: int, operation: int) -> None:
        nonlocal failed
        if not failed and operation & fcntl.LOCK_EX and operation & fcntl.LOCK_NB:
            failed = True
            raise OSError(errno.ENOTSUP, "simulated unsupported flock")
        real_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", fail_first_acquire)
    with pytest.raises(AtesStoreError, match="cannot acquire ATES writer authority") as caught:
        AtesEventStore(tmp_path, RunId("RUN-flock-operational-error"))

    assert not isinstance(caught.value, AtesStoreBusy)
    assert failed is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX raw descriptor/flock lifecycle")
def test_abandoned_store_collection_releases_writer_authority(tmp_path: Path):
    run_id = RunId("RUN-abandoned-finalizer")
    store = AtesEventStore(tmp_path, run_id)
    reference = weakref.ref(store)
    run_dir = store.run_dir

    del store
    gc.collect()

    assert reference() is None
    assert run_dir.is_dir()
    with AtesEventStore(tmp_path, run_id) as reopened:
        assert reopened.next_sequence == 1


def test_external_truncate_before_append_poisons_cached_history(tmp_path: Path):
    run_id = RunId("RUN-external-truncate-before")
    store = AtesEventStore(tmp_path, run_id)
    original_bytes = b""
    try:
        first = store.append(
            EventType.RUN_STARTED,
            occurred_at=NOW,
            event_id="EVT-before-truncate",
        )
        assert first.sequence == 1
        original_bytes = store.path.read_bytes()
        assert original_bytes

        with store.path.open("r+b", buffering=0) as external:
            external.truncate(0)
            external.flush()
            os.fsync(external.fileno())

        with pytest.raises(AtesStoreError, match="evidence length changed"):
            store.append(
                EventType.TARGET_LAUNCHED,
                occurred_at=NOW,
                event_id="EVT-after-truncate",
            )

        assert store.poisoned is True
        assert store.next_sequence == 2
        store.path.write_bytes(original_bytes)
    finally:
        if original_bytes and store.path.exists() and store.path.stat().st_size == 0:
            store.path.write_bytes(original_bytes)
        store.close()

    with AtesEventStore(tmp_path, run_id) as reopened:
        assert reopened.next_sequence == 2
        assert [str(event.event_id) for event in reopened.events] == [
            "EVT-before-truncate"
        ]


def test_truncate_after_append_fsync_is_ambiguous_and_poisons(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    run_id = RunId("RUN-external-truncate-after-fsync")
    store = AtesEventStore(tmp_path, run_id)
    original_bytes = b""
    assert store._file is not None
    evidence_fd = store._file.fileno()
    real_fsync = store_module.os.fsync
    truncated = False

    try:
        store.append(
            EventType.RUN_STARTED,
            occurred_at=NOW,
            event_id="EVT-stable-before-fsync-truncate",
        )
        original_bytes = store.path.read_bytes()

        def fsync_then_truncate(fd: int) -> None:
            nonlocal truncated
            real_fsync(fd)
            if fd == evidence_fd and not truncated:
                truncated = True
                os.truncate(store.path, 0)

        with monkeypatch.context() as scoped:
            scoped.setattr(store_module.os, "fsync", fsync_then_truncate)
            with pytest.raises(AtesAppendError) as caught:
                store.append(
                    EventType.TARGET_LAUNCHED,
                    occurred_at=NOW,
                    event_id="EVT-fsync-then-truncate",
                )

        assert truncated is True
        assert caught.value.event.event_id == "EVT-fsync-then-truncate"
        assert caught.value.outcome_unknown is True
        assert store.poisoned is True

        store.path.write_bytes(original_bytes)
    finally:
        if original_bytes and store.path.exists() and store.path.stat().st_size == 0:
            store.path.write_bytes(original_bytes)
        store.close()

    with AtesEventStore(tmp_path, run_id) as reopened:
        assert reopened.next_sequence == 2
        assert [str(event.event_id) for event in reopened.events] == [
            "EVT-stable-before-fsync-truncate"
        ]
