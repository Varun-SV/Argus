from __future__ import annotations

import errno
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from argus.ates import EventType, RunId
from argus.ates.store import AtesEventStore, AtesStoreBusy, AtesStoreError
import argus.ates.store as store_module


NOW = datetime(2026, 8, 11, 13, 15, tzinfo=timezone.utc)


pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX flock authority regressions",
)


def test_replacing_writer_marker_cannot_create_second_authority(tmp_path: Path):
    run_id = RunId("RUN-lock-marker-replace")
    first = AtesEventStore(tmp_path, run_id)
    marker = first.run_dir / ".ates-writer.lock"
    replacement = first.run_dir / ".replacement-writer.lock"

    try:
        replacement.write_bytes(b"\0")
        os.replace(replacement, marker)

        with pytest.raises(AtesStoreBusy, match="authoritative ATES writer"):
            AtesEventStore(tmp_path, run_id)

        event = first.append(
            EventType.RUN_STARTED,
            {"marker_replaced": True},
            occurred_at=NOW,
            event_id="EVT-marker-replaced",
        )
        assert event.sequence == 1
    finally:
        first.close()

    with AtesEventStore(tmp_path, run_id) as reopened:
        assert [event.event_id for event in reopened.events] == [
            event.event_id
        ]
        assert reopened.next_sequence == 2


def test_unlinking_writer_marker_cannot_create_second_authority(tmp_path: Path):
    run_id = RunId("RUN-lock-marker-unlink")
    first = AtesEventStore(tmp_path, run_id)
    marker = first.run_dir / ".ates-writer.lock"

    try:
        marker.unlink()
        assert marker.exists() is False

        with pytest.raises(AtesStoreBusy, match="authoritative ATES writer"):
            AtesEventStore(tmp_path, run_id)

        # The failed second writer must be rejected before it can recreate the
        # replaceable marker pathname.
        assert marker.exists() is False

        event = first.append(
            EventType.RUN_STARTED,
            {"marker_unlinked": True},
            occurred_at=NOW,
            event_id="EVT-marker-unlinked",
        )
        assert event.sequence == 1
    finally:
        first.close()

    # Once the original authority is gone, a normal writer may recreate the
    # marker and recover the already-committed evidence stream.
    with AtesEventStore(tmp_path, run_id) as reopened:
        assert marker.exists() is True
        assert reopened.next_sequence == 2
        assert len(reopened.events) == 1


def test_run_directory_inode_is_the_posix_authority_anchor(tmp_path: Path):
    import fcntl

    with AtesEventStore(tmp_path, RunId("RUN-directory-authority")) as store:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        other_fd = os.open(store.run_dir, flags)
        try:
            with pytest.raises(OSError) as caught:
                fcntl.flock(other_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert caught.value.errno in (errno.EACCES, errno.EAGAIN)
        finally:
            os.close(other_fd)


def test_failed_writer_initialization_releases_directory_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    run_id = RunId("RUN-directory-authority-cleanup")
    original_open = store_module._open_regular_file

    def fail_marker_open(directory, name):
        if name == ".ates-writer.lock":
            raise AtesStoreError("simulated marker open failure")
        return original_open(directory, name)

    with monkeypatch.context() as scoped:
        scoped.setattr(store_module, "_open_regular_file", fail_marker_open)
        with pytest.raises(AtesStoreError, match="simulated marker open failure"):
            AtesEventStore(tmp_path, run_id)

    # If the failed constructor leaked the directory flock, this second open
    # would remain busy in the same process.
    with AtesEventStore(tmp_path, run_id) as reopened:
        assert reopened.next_sequence == 1
