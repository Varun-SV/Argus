from __future__ import annotations

import os
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


NOW = datetime(2026, 8, 11, 14, 30, tzinfo=timezone.utc)


pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX namespace durability/authority regressions",
)


def test_existing_child_resyncs_parent_after_prior_fsync_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    parent = store_module._PinnedDirectory(tmp_path.resolve())
    child_path = tmp_path / "child"
    original_fsync = store_module._PinnedDirectory.fsync
    failed = False

    def fail_first_parent_sync(directory: store_module._PinnedDirectory) -> None:
        nonlocal failed
        if directory is parent and not failed:
            failed = True
            raise AtesStoreError("simulated parent fsync failure")
        original_fsync(directory)

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(
                store_module._PinnedDirectory,
                "fsync",
                fail_first_parent_sync,
            )
            with pytest.raises(AtesStoreError, match="simulated parent fsync failure"):
                parent.ensure_child("child", "test child")

        # mkdir succeeded before the durability barrier failed.
        assert child_path.is_dir()

        sync_calls = 0

        def track_parent_sync(directory: store_module._PinnedDirectory) -> None:
            nonlocal sync_calls
            if directory is parent:
                sync_calls += 1
            original_fsync(directory)

        with monkeypatch.context() as scoped:
            scoped.setattr(
                store_module._PinnedDirectory,
                "fsync",
                track_parent_sync,
            )
            child = parent.ensure_child("child", "test child")
            child.close()

        # The retry must re-prove the already-existing namespace entry rather
        # than treating FileExistsError as evidence that it was durable before.
        assert sync_calls == 1
    finally:
        parent.close()


def test_replaced_run_directory_cannot_create_second_authority(tmp_path: Path):
    run_id = RunId("RUN-directory-replacement")
    first = AtesEventStore(tmp_path, run_id)
    canonical = first.run_dir
    moved = canonical.with_name(canonical.name + "-moved")

    try:
        first_event = first.append(
            EventType.RUN_STARTED,
            {"before_swap": True},
            occurred_at=NOW,
            event_id="EVT-before-directory-swap",
        )
        assert first_event.sequence == 1

        canonical.rename(moved)
        canonical.mkdir()

        # The per-RunId authority lives in the pinned runs/ parent, so opening
        # the replacement directory cannot mint a second conforming writer.
        with pytest.raises(AtesStoreBusy, match="authoritative ATES writer"):
            AtesEventStore(tmp_path, run_id)

        # The original writer also fails closed instead of silently continuing
        # to append into the renamed, no-longer-canonical directory inode.
        with pytest.raises(AtesStoreError, match="namespace no longer refers"):
            first.append(
                EventType.TARGET_LAUNCHED,
                occurred_at=NOW,
                event_id="EVT-after-directory-swap",
            )
        assert first.poisoned is False

        # Restoring the canonical namespace lets the still-authoritative writer
        # continue without having written anything during the lost namespace.
        canonical.rmdir()
        moved.rename(canonical)
        second_event = first.append(
            EventType.TARGET_LAUNCHED,
            occurred_at=NOW,
            event_id="EVT-after-directory-restore",
        )
        assert second_event.sequence == 2
    finally:
        if canonical.exists() and moved.exists():
            canonical.rmdir()
            moved.rename(canonical)
        elif not canonical.exists() and moved.exists():
            moved.rename(canonical)
        first.close()

    with AtesEventStore(tmp_path, run_id) as reopened:
        assert reopened.next_sequence == 3
        assert [str(event.event_id) for event in reopened.events] == [
            "EVT-before-directory-swap",
            "EVT-after-directory-restore",
        ]


def test_renamed_run_directory_is_not_recreated_by_rejected_second_writer(
    tmp_path: Path,
):
    run_id = RunId("RUN-directory-missing")
    first = AtesEventStore(tmp_path, run_id)
    canonical = first.run_dir
    moved = canonical.with_name(canonical.name + "-moved")

    try:
        canonical.rename(moved)
        assert canonical.exists() is False

        with pytest.raises(AtesStoreBusy, match="authoritative ATES writer"):
            AtesEventStore(tmp_path, run_id)

        # Authority is acquired in runs/ before ensure_child(run_id), so the
        # rejected writer cannot leave a fresh empty canonical directory behind.
        assert canonical.exists() is False
    finally:
        if not canonical.exists() and moved.exists():
            moved.rename(canonical)
        first.close()


def test_namespace_swap_during_append_poisons_instead_of_returning_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    run_id = RunId("RUN-directory-swap-during-append")
    store = AtesEventStore(tmp_path, run_id)
    canonical = store.run_dir
    moved = canonical.with_name(canonical.name + "-moved")
    assert store._file is not None
    evidence_fd = store._file.fileno()
    real_fsync = store_module.os.fsync
    swapped = False

    def fsync_then_swap(fd: int) -> None:
        nonlocal swapped
        real_fsync(fd)
        if fd == evidence_fd and not swapped:
            swapped = True
            canonical.rename(moved)
            canonical.mkdir()

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(store_module.os, "fsync", fsync_then_swap)
            with pytest.raises(AtesAppendError) as caught:
                store.append(
                    EventType.RUN_STARTED,
                    {"swap_during_fsync": True},
                    occurred_at=NOW,
                    event_id="EVT-swap-during-fsync",
                )

        assert swapped is True
        assert caught.value.event.event_id == "EVT-swap-during-fsync"
        assert caught.value.outcome_unknown is True
        assert store.poisoned is True

        # Even after the visible directory was replaced, the parent-scoped
        # authority still excludes a second writer while reconciliation is open.
        with pytest.raises(AtesStoreBusy, match="authoritative ATES writer"):
            AtesEventStore(tmp_path, run_id)

        canonical.rmdir()
        moved.rename(canonical)
    finally:
        if canonical.exists() and moved.exists():
            canonical.rmdir()
            moved.rename(canonical)
        elif not canonical.exists() and moved.exists():
            moved.rename(canonical)
        store.close()

    # The stable event identity can now be reconciled from the restored stream.
    with AtesEventStore(tmp_path, run_id) as reopened:
        assert reopened.next_sequence == 2
        assert [str(event.event_id) for event in reopened.events] == [
            "EVT-swap-during-fsync"
        ]


def test_replacing_parent_authority_alone_still_cannot_mint_second_writer(
    tmp_path: Path,
):
    run_id = RunId("RUN-parent-authority-replace")
    first = AtesEventStore(tmp_path, run_id)
    runs = first.run_dir.parent
    authority = runs / store_module._run_authority_filename(run_id)
    replacement = runs / ".replacement-authority.lock"

    try:
        replacement.write_bytes(b"\0")
        os.replace(replacement, authority)

        # The new parent authority inode can be locked, but the unchanged run
        # directory inode remains independently locked by the first writer.
        with pytest.raises(AtesStoreBusy, match="authoritative ATES writer"):
            AtesEventStore(tmp_path, run_id)

        # The first writer independently detects that its parent authority name
        # no longer refers to the held lock object and therefore fails closed.
        with pytest.raises(AtesStoreError, match="authority entry was replaced"):
            first.append(
                EventType.RUN_STARTED,
                occurred_at=NOW,
                event_id="EVT-parent-authority-replaced",
            )
        assert first.poisoned is False
    finally:
        first.close()
