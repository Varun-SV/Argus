"""Cross-cutting transaction authority for ATES derived state and recovery.

This module closes two transaction gaps without creating review-round-specific
implementation layers:

* report snapshots and detached approval/audit commits share a cross-process
  barrier, so a successful report render is linearizable with ledger commits;
* revision-one recovery retains exact manifest/package file identity from the
  last validation through mutation and revalidates after ambiguous appends.
"""
from __future__ import annotations

import errno
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Optional

from .core import EventType
from .ids import RunId
from .status import derive_run_status
from .store import (
    AtesAppendError,
    AtesEventStore,
    AtesStoreBusy,
    AtesStoreError,
    _PinnedDirectory,
    _open_regular_file,
    _validate_regular_file_descriptor,
    _windows_handle_info,
)

_BARRIER_NAME = ".ates-derived-state.lock"
_BARRIER_WAIT_SECONDS = 5.0
_BARRIER_RETRY_SECONDS = 0.01
_ACTIVE_BARRIER: ContextVar[Optional[tuple[Path, str]]] = ContextVar(
    "ates_derived_state_barrier", default=None
)


class _DerivedStateBarrier:
    """A file-only lock shared by reports and detached-ledger transactions.

    Unlike ``_WriterLock`` this deliberately does not flock the run-directory
    descriptor. Report verification legitimately opens ``AtesEventStore``
    while this barrier is held, so reusing the run writer lock would deadlock.
    """

    def __init__(self, run_pin: _PinnedDirectory) -> None:
        self._run_pin = run_pin
        self._handle = None
        self._locked = False
        self._owner_pid = os.getpid()
        deadline = time.monotonic() + _BARRIER_WAIT_SECONDS

        handle, created = _open_regular_file(run_pin, _BARRIER_NAME)
        self._handle = handle
        try:
            if created or os.fstat(handle.fileno()).st_size == 0:
                handle.seek(0)
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
                if created:
                    run_pin.fsync()

            while True:
                try:
                    if os.name == "nt":
                        import msvcrt

                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._locked = True
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        raise AtesStoreError(
                            f"cannot acquire derived-state transaction barrier: {exc}"
                        ) from exc
                    if time.monotonic() >= deadline:
                        raise AtesStoreBusy(
                            "timed out waiting for derived-state transaction barrier"
                        ) from exc
                    time.sleep(_BARRIER_RETRY_SECONDS)
            self.assert_authoritative()
        except BaseException:
            try:
                self.close()
            except BaseException:
                pass
            raise

    def assert_authoritative(self) -> None:
        if os.getpid() != self._owner_pid:
            raise AtesStoreError(
                "derived-state transaction barrier was inherited across a fork"
            )
        if self._handle is None or self._handle.closed or not self._locked:
            raise AtesStoreError("derived-state transaction barrier is no longer held")
        _validate_regular_file_descriptor(
            self._handle.fileno(), self._run_pin.path / _BARRIER_NAME
        )
        self._run_pin.assert_file_identity(
            _BARRIER_NAME,
            self._handle.fileno(),
            "derived-state transaction barrier",
        )

    def close(self) -> None:
        handle = self._handle
        if handle is None or handle.closed:
            self._handle = None
            self._locked = False
            return
        inherited = os.getpid() != self._owner_pid
        first_error: Optional[BaseException] = None
        try:
            if self._locked and not inherited:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except BaseException as exc:
            first_error = exc
        finally:
            self._locked = False
            try:
                handle.close()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
            self._handle = None
        if first_error is not None:
            raise first_error


class _HeldRead:
    """Retain a no-follow regular-file handle across recovery mutation."""

    def __init__(self, directory: _PinnedDirectory, name: str, label: str) -> None:
        self.directory = directory
        self.name = name
        self.label = label
        self.path = directory.path / name
        self.fd: Optional[int] = None
        self._kernel32 = None
        self._raw_handle = None
        try:
            if os.name == "nt":
                kernel32, raw, _ = _windows_handle_info(
                    self.path, directory=False, create=False
                )
                self._kernel32 = kernel32
                self._raw_handle = raw
                import msvcrt

                self.fd = msvcrt.open_osfhandle(
                    raw, os.O_RDONLY | getattr(os, "O_BINARY", 0)
                )
                self._raw_handle = None
            else:
                if directory._fd is None:
                    raise AtesStoreError(f"pinned authority unavailable for {label}")
                self.fd = os.open(
                    name,
                    os.O_RDONLY
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=directory._fd,
                )
            assert self.fd is not None
            _validate_regular_file_descriptor(self.fd, self.path)
            self.assert_authoritative()
        except BaseException:
            self.close()
            raise

    def read(self) -> bytes:
        if self.fd is None:
            raise AtesStoreError(f"{self.label} authority is no longer held")
        os.lseek(self.fd, 0, os.SEEK_SET)
        with os.fdopen(self.fd, "rb", buffering=0, closefd=False) as handle:
            data = handle.read()
        self.assert_authoritative()
        return data

    def assert_authoritative(self) -> None:
        if self.fd is None:
            raise AtesStoreError(f"{self.label} authority is no longer held")
        _validate_regular_file_descriptor(self.fd, self.path)
        self.directory.assert_file_identity(self.name, self.fd, self.label)

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        if self._raw_handle is not None and self._kernel32 is not None:
            try:
                self._kernel32.CloseHandle(self._raw_handle)
            except BaseException:
                pass
            self._raw_handle = None


def _directory_identity(pin: _PinnedDirectory) -> tuple[int, int]:
    if os.name == "nt":
        info = os.stat(pin.path, follow_symlinks=False)
    else:
        if pin._fd is None:
            raise AtesStoreError("pinned run authority is unavailable")
        info = os.fstat(pin._fd)
    return info.st_dev, info.st_ino


def _assert_recovery_authority(
    store: AtesEventStore,
    manifests: _PinnedDirectory,
    manifest: _HeldRead,
    package: Optional[_HeldRead],
) -> None:
    directories = store._directories
    if directories is None:
        raise AtesStoreError("run authority unavailable during recovery")
    directories.assert_authoritative()
    directories.run.assert_child_identity(
        "manifests", manifests, "ATES manifests directory"
    )
    manifest.assert_authoritative()
    if package is not None:
        package.assert_authoritative()


def _canonical_event_bytes(events) -> bytes:
    return b"".join(event.canonical_line() for event in events)


def install() -> None:
    """Install transaction guards after trust and namespace authority guards."""
    from . import audit
    from . import finalization
    from . import finalization_io as fio
    from . import reports

    if getattr(audit, "_ates_transaction_guards_installed", False):
        return

    base_ledger_transaction = audit._ledger_transaction
    base_report_transaction = reports._report_transaction

    @contextmanager
    def guarded_ledger_transaction(root, *, run_id, expected_identity):
        root = Path(root)
        active = _ACTIVE_BARRIER.get()
        if active is not None:
            raise audit.ApprovalError(
                "detached-ledger mutation cannot nest inside a derived-state transaction"
            )

        run_pin = None
        barrier = None
        token = None
        try:
            run_pin = _PinnedDirectory(root)
            if _directory_identity(run_pin) != expected_identity:
                raise audit.ApprovalError(
                    "detached-ledger run directory changed before transaction barrier"
                )
            barrier = _DerivedStateBarrier(run_pin)
            token = _ACTIVE_BARRIER.set((root, "ledger"))
            # Barrier-before-writer is intentional. Reports also take the
            # barrier before opening AtesEventStore, preventing lock inversion.
            with base_ledger_transaction(
                root, run_id=run_id, expected_identity=expected_identity
            ) as authority:
                yield authority
                barrier.assert_authoritative()
                if _directory_identity(run_pin) != expected_identity:
                    raise audit.ApprovalError(
                        "detached-ledger run directory changed during transaction"
                    )
        except audit.ApprovalError:
            raise
        except (OSError, AtesStoreBusy, AtesStoreError) as exc:
            raise audit.ApprovalError(
                "detached-ledger derived-state transaction authority failed"
            ) from exc
        finally:
            if token is not None:
                _ACTIVE_BARRIER.reset(token)
            if barrier is not None:
                try:
                    barrier.close()
                except BaseException:
                    pass
            if run_pin is not None:
                try:
                    run_pin.close()
                except BaseException:
                    pass

    @contextmanager
    def guarded_report_transaction(root):
        active = _ACTIVE_BARRIER.get()
        if active is not None:
            raise reports.ReportError(
                "report transaction cannot nest inside another derived-state transaction"
            )
        with base_report_transaction(root) as authority:
            run_pin, _reports_pin, _reports_lock = authority
            barrier = None
            token = None
            try:
                barrier = _DerivedStateBarrier(run_pin)
                token = _ACTIVE_BARRIER.set((Path(root), "report"))
                yield authority
                barrier.assert_authoritative()
            except reports.ReportError:
                raise
            except (OSError, AtesStoreBusy, AtesStoreError) as exc:
                raise reports.ReportError(
                    "report derived-state transaction authority failed"
                ) from exc
            finally:
                if token is not None:
                    _ACTIVE_BARRIER.reset(token)
                if barrier is not None:
                    try:
                        barrier.close()
                    except BaseException:
                        pass

    def guarded_recover_unbound_revision(project_dir, run_id):
        try:
            rid = run_id if isinstance(run_id, RunId) else RunId(run_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("run_id must be a valid RunId") from exc
        project = Path(project_dir).resolve(strict=True)
        store = AtesEventStore(project, rid, repair_trailing_partial=True)
        root = store.run_dir
        manifests = None
        manifest_hold = None
        package_hold = None
        try:
            directories = store._directories
            if directories is None:
                raise finalization.FinalizationError(
                    "run authority unavailable during recovery"
                )
            directories.assert_authoritative()
            run_pin = directories.run

            if fio._entry_exists(run_pin, "run.json"):
                pass
            elif not fio._entry_exists(run_pin, "manifests"):
                if any(
                    event.envelope.event_type is EventType.RUN_COMPLETED
                    for event in store.events
                ):
                    raise finalization.FinalizationError(
                        "recovery state is missing evidence manifest"
                    )
                finalization.finalize_revision_one(store)
            else:
                manifests = _PinnedDirectory(root / "manifests")
                run_pin.assert_child_identity(
                    "manifests", manifests, "ATES manifests directory"
                )
                manifest_name = "manifest-0001.json"
                package_name = "package-manifest-0001.json"
                if not fio._entry_exists(manifests, manifest_name):
                    if (
                        fio._entry_exists(manifests, package_name)
                        or any(
                            event.envelope.event_type is EventType.RUN_COMPLETED
                            for event in store.events
                        )
                    ):
                        raise finalization.FinalizationError(
                            "recovery state is missing evidence manifest"
                        )
                    manifests.close()
                    manifests = None
                    finalization.finalize_revision_one(store)
                else:
                    manifest_hold = _HeldRead(
                        manifests, manifest_name, "recovery evidence manifest"
                    )
                    manifest_raw = manifest_hold.read()
                    manifest = fio._strict_json_object(
                        manifest_raw, "recovery evidence manifest"
                    )
                    outcome, completion = finalization._candidate_from_manifest(
                        manifest, rid
                    )
                    finals = [
                        event
                        for event in store.events
                        if event.envelope.event_type is EventType.RUN_COMPLETED
                    ]
                    if finals:
                        if (
                            len(finals) != 1
                            or store.events[-1].canonical_line()
                            != completion.canonical_line()
                        ):
                            raise finalization.FinalizationError(
                                "existing completion differs from recovery candidate"
                            )
                        pre = store.events[:-1]
                    else:
                        pre = store.events
                    if completion.sequence != len(pre) + 1:
                        raise finalization.FinalizationError(
                            "recovery completion sequence is inconsistent"
                        )
                    state = finalization._derive(pre, rid)
                    if (
                        outcome.status_policy_version
                        != finalization.STATUS_POLICY_VERSION
                        or derive_run_status(state.status_inputs)
                        is not outcome.effective_status
                    ):
                        raise finalization.FinalizationError(
                            "recovery outcome differs from canonical derivation"
                        )
                    artifacts = finalization._artifacts(store, state.artifacts)
                    expected_manifest, expected_package, expected_evidence = (
                        finalization._documents(
                            pre, completion, outcome, artifacts
                        )
                    )
                    manifest_bytes = finalization._json(expected_manifest)
                    package_bytes = finalization._json(expected_package)
                    if manifest_raw != manifest_bytes:
                        raise finalization.FinalizationError(
                            "recovery evidence manifest bytes differ from regenerated candidate"
                        )
                    if store._read_all() != _canonical_event_bytes(store.events):
                        raise finalization.FinalizationError(
                            "recovery event bytes differ from the authoritative event prefix"
                        )

                    if fio._entry_exists(manifests, package_name):
                        package_hold = _HeldRead(
                            manifests, package_name, "recovery package manifest"
                        )
                        package_raw = package_hold.read()
                        fio._strict_json_object(
                            package_raw, "recovery package manifest"
                        )
                        if package_raw != package_bytes:
                            raise finalization.FinalizationError(
                                "recovery package manifest bytes differ from regenerated candidate"
                            )

                    _assert_recovery_authority(
                        store, manifests, manifest_hold, package_hold
                    )
                    if package_hold is None:
                        finalization._publish(
                            manifests, package_name, package_bytes
                        )
                        package_hold = _HeldRead(
                            manifests, package_name, "recovery package manifest"
                        )
                        if package_hold.read() != package_bytes:
                            raise finalization.FinalizationError(
                                "published recovery package manifest differs from candidate"
                            )

                    _assert_recovery_authority(
                        store, manifests, manifest_hold, package_hold
                    )
                    if not finals:
                        try:
                            store.append_event(completion)
                        except AtesAppendError:
                            # An append error is ambiguous. Drop the old store,
                            # re-open canonical authority, then revalidate every
                            # candidate byte before the next mutation (binding).
                            store.close()
                            store = finalization._reopen(project, rid, completion)
                            root = store.run_dir
                            if manifest_hold is not None:
                                manifest_hold.close()
                                manifest_hold = None
                            if package_hold is not None:
                                package_hold.close()
                                package_hold = None
                            if manifests is not None:
                                manifests.close()
                            directories = store._directories
                            if directories is None:
                                raise finalization.FinalizationError(
                                    "run authority unavailable after recovery append"
                                )
                            manifests = _PinnedDirectory(root / "manifests")
                            directories.run.assert_child_identity(
                                "manifests", manifests, "ATES manifests directory"
                            )
                            manifest_hold = _HeldRead(
                                manifests,
                                manifest_name,
                                "recovery evidence manifest",
                            )
                            package_hold = _HeldRead(
                                manifests,
                                package_name,
                                "recovery package manifest",
                            )
                            if manifest_hold.read() != manifest_bytes:
                                raise finalization.FinalizationError(
                                    "recovery evidence manifest changed during append reconciliation"
                                )
                            if package_hold.read() != package_bytes:
                                raise finalization.FinalizationError(
                                    "recovery package manifest changed during append reconciliation"
                                )

                    if store._read_all() != expected_evidence:
                        raise finalization.FinalizationError(
                            "recovered evidence differs from manifest-bound candidate"
                        )
                    _assert_recovery_authority(
                        store, manifests, manifest_hold, package_hold
                    )
                    directories = store._directories
                    if directories is None:
                        raise finalization.FinalizationError(
                            "run authority unavailable for recovered binding"
                        )
                    finalization._publish(
                        directories.run,
                        "run.json",
                        finalization._json(
                            finalization._binding(
                                outcome,
                                completion,
                                manifest_bytes,
                                package_bytes,
                            )
                        ),
                    )
                    _assert_recovery_authority(
                        store, manifests, manifest_hold, package_hold
                    )
        except finalization.FinalizationError:
            raise
        except (OSError, AtesStoreError, ValueError) as exc:
            raise finalization.FinalizationError(
                "recovery transaction authority failed"
            ) from exc
        finally:
            for held in (package_hold, manifest_hold):
                if held is not None:
                    try:
                        held.close()
                    except BaseException:
                        pass
            if manifests is not None:
                try:
                    manifests.close()
                except BaseException:
                    pass
            try:
                store.close()
            except BaseException:
                pass

        # Verification happens only after writer handles are released, which
        # mirrors the canonical recovery contract and avoids self-locking.
        return finalization.verify_finalized_run(root)

    audit._ledger_transaction = guarded_ledger_transaction
    reports._report_transaction = guarded_report_transaction
    finalization._recover_unbound_revision = guarded_recover_unbound_revision
    audit._ates_transaction_guards_installed = True


__all__ = ["install"]
