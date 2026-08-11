"""Durable append-only ATES event storage.

This module owns the canonical local ``evidence.jsonl`` stream for one ATES
run. Runtime wiring, evidence manifests, report rendering, and Fleet transport
remain later layers.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .core import (
    ATES_VERSION,
    EventEnvelope,
    EventType,
    FrozenDict,
    JsonValue,
    freeze_json,
    to_json_compatible,
)
from .ids import EventId, RunId


class AtesStoreError(RuntimeError):
    """Base class for ATES event-store failures."""


class AtesStoreCorruption(AtesStoreError):
    """Persisted canonical evidence is malformed or internally inconsistent."""


class AtesStoreBusy(AtesStoreError):
    """Another authoritative writer already owns this run store."""


class AtesEventConflict(AtesStoreError):
    """An event ID or sequence conflicts with already-persisted evidence."""


class AtesAppendError(AtesStoreError):
    """An append failed after its stable event identity had been selected."""

    def __init__(self, message: str, event: "StoredEvent") -> None:
        super().__init__(message)
        self.event = event
        # A failed write/fsync can leave either no durable record or the exact
        # full record. Callers must reopen and reconcile this same identity.
        self.outcome_unknown = True


@dataclass(frozen=True)
class StoredEvent:
    """One canonical JSONL record: envelope plus immutable object payload."""

    envelope: EventEnvelope
    payload: Mapping[str, JsonValue] = field(default_factory=FrozenDict)

    def __post_init__(self) -> None:
        if not isinstance(self.envelope, EventEnvelope):
            raise ValueError("envelope must be an EventEnvelope")
        if not isinstance(self.payload, Mapping):
            raise ValueError("event payload must be a JSON object")
        frozen = freeze_json(self.payload)
        if not isinstance(frozen, Mapping):
            raise ValueError("event payload must remain a JSON object")
        object.__setattr__(self, "payload", frozen)

    @property
    def run_id(self) -> RunId:
        return self.envelope.run_id

    @property
    def event_id(self) -> EventId:
        return self.envelope.event_id

    @property
    def sequence(self) -> int:
        return self.envelope.sequence

    def to_document(self) -> dict[str, JsonValue]:
        envelope = to_json_compatible(self.envelope)
        if not isinstance(envelope, dict):
            raise ValueError("event envelope did not serialize to a JSON object")
        document: dict[str, JsonValue] = dict(envelope)
        document["payload"] = to_json_compatible(self.payload)
        return document

    def canonical_line(self) -> bytes:
        return _canonical_json_bytes(self.to_document()) + b"\n"

    @classmethod
    def from_document(cls, document: object) -> "StoredEvent":
        if not isinstance(document, Mapping):
            raise AtesStoreCorruption("canonical event line must contain a JSON object")
        try:
            snapshot = tuple(document.items())
        except (RuntimeError, TypeError, ValueError) as exc:
            raise AtesStoreCorruption(
                "canonical event object could not be snapshotted safely"
            ) from exc
        if not all(isinstance(key, str) for key, _ in snapshot):
            raise AtesStoreCorruption("canonical event object keys must be strings")
        if len({key for key, _ in snapshot}) != len(snapshot):
            raise AtesStoreCorruption("canonical event object contains duplicate fields")
        document = dict(snapshot)

        required = {
            "ates_version",
            "run_id",
            "event_id",
            "sequence",
            "event_type",
            "occurred_at",
            "payload",
        }
        keys = set(document)
        if keys != required:
            missing = sorted(required - keys)
            extra = sorted(keys - required)
            details: list[str] = []
            if missing:
                details.append(f"missing={missing}")
            if extra:
                details.append(f"extra={extra}")
            suffix = f" ({', '.join(details)})" if details else ""
            raise AtesStoreCorruption(
                f"canonical event object has unexpected fields{suffix}"
            )

        occurred_at = document["occurred_at"]
        if not isinstance(occurred_at, str):
            raise AtesStoreCorruption("occurred_at must be an ISO-8601 string")
        try:
            parsed_time = datetime.fromisoformat(occurred_at)
        except ValueError as exc:
            raise AtesStoreCorruption(
                "occurred_at is not a valid ISO-8601 timestamp"
            ) from exc

        payload = document["payload"]
        if not isinstance(payload, Mapping):
            raise AtesStoreCorruption("canonical event payload must be a JSON object")

        try:
            envelope = EventEnvelope(
                ates_version=document["ates_version"],  # type: ignore[arg-type]
                run_id=document["run_id"],  # type: ignore[arg-type]
                event_id=document["event_id"],  # type: ignore[arg-type]
                sequence=document["sequence"],  # type: ignore[arg-type]
                event_type=document["event_type"],  # type: ignore[arg-type]
                occurred_at=parsed_time,
            )
            return cls(envelope=envelope, payload=payload)  # type: ignore[arg-type]
        except (TypeError, ValueError, RecursionError) as exc:
            raise AtesStoreCorruption(f"invalid canonical event: {exc}") from exc


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError(
            f"value cannot be encoded as canonical ATES JSON: {exc}"
        ) from exc


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _object_without_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _decode_json_object(raw: bytes) -> object:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AtesStoreCorruption("canonical event line is not valid UTF-8") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise AtesStoreCorruption(
            f"canonical event line is not valid strict JSON: {exc}"
        ) from exc


def _run_directory_key(run_id: RunId) -> str:
    """Encode RunId case distinctions into a case-insensitive filesystem key.

    Lowercase/digit/hyphen suffixes keep their familiar spelling. Underscores
    are escaped as ``__`` and uppercase letters as ``_<lowercase>``. The output
    alphabet is therefore lowercase/punctuation-only and uniquely decodable,
    so case-insensitive filesystems cannot alias distinct valid RunIds.
    """
    raw = str(run_id)
    prefix = "RUN-"
    suffix = raw[len(prefix):]
    encoded: list[str] = []
    for char in suffix:
        if char == "_":
            encoded.append("__")
        elif "A" <= char <= "Z":
            encoded.append("_" + char.lower())
        else:
            encoded.append(char)
    return prefix + "".join(encoded)


def _run_authority_filename(run_id: RunId) -> str:
    """Return the parent-scoped authority filename for one canonical RunId."""
    return f".ates-authority-{_run_directory_key(run_id)}.lock"


def _windows_handle_info(path: Path, *, directory: bool, create: bool = False):
    """Open and validate a non-reparse Windows filesystem handle."""
    import ctypes
    from ctypes import wintypes

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE

    get_info = kernel32.GetFileInformationByHandle
    get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ByHandleFileInformation)]
    get_info.restype = wintypes.BOOL

    desired_access = 0 if directory else 0x80000000 | 0x40000000  # read | write
    # Keep read/write sharing for normal readers and locking, but deliberately
    # omit FILE_SHARE_DELETE so the pinned object cannot be renamed/replaced.
    share_mode = 0x00000001 | 0x00000002
    creation = 4 if create else 3  # OPEN_ALWAYS / OPEN_EXISTING
    flags = 0x00200000  # FILE_FLAG_OPEN_REPARSE_POINT
    if directory:
        flags |= 0x02000000  # FILE_FLAG_BACKUP_SEMANTICS
    else:
        flags |= 0x00000080  # FILE_ATTRIBUTE_NORMAL

    ctypes.set_last_error(0)
    handle = create_file(str(path), desired_access, share_mode, None, creation, flags, None)
    invalid = ctypes.c_void_p(-1).value
    if handle == invalid:
        error = ctypes.get_last_error()
        raise AtesStoreError(
            f"cannot open pinned filesystem object {path}: winerror {error}"
        )

    created = bool(create and ctypes.get_last_error() != 183)  # ERROR_ALREADY_EXISTS
    info = _ByHandleFileInformation()
    if not get_info(handle, ctypes.byref(info)):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        raise AtesStoreError(
            f"cannot inspect pinned filesystem object {path}: winerror {error}"
        )

    is_directory = bool(info.dwFileAttributes & 0x00000010)
    is_reparse = bool(info.dwFileAttributes & 0x00000400)
    if is_reparse:
        kernel32.CloseHandle(handle)
        label = "directory" if directory else "event-store file"
        raise AtesStoreError(
            f"{label} cannot be a symlink or reparse point: {path}"
        )
    if is_directory != directory:
        kernel32.CloseHandle(handle)
        kind = "directory" if directory else "regular file"
        raise AtesStoreError(f"filesystem object is not a {kind}: {path}")
    if not directory and info.nNumberOfLinks != 1:
        kernel32.CloseHandle(handle)
        raise AtesStoreError(
            f"event-store file must have exactly one hard link: {path}"
        )
    return kernel32, handle, created


class _PinnedDirectory:
    """A directory whose identity remains stable while child files are opened."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fd: Optional[int] = None
        self._kernel32 = None
        self._win_handle = None

        if os.name == "nt":
            kernel32, handle, _ = _windows_handle_info(self.path, directory=True)
            self._kernel32 = kernel32
            self._win_handle = handle
            return

        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(self.path, flags)
        except OSError as exc:
            raise AtesStoreError(f"cannot pin directory {self.path}: {exc}") from exc
        try:
            if not stat.S_ISDIR(os.fstat(fd).st_mode):
                raise AtesStoreError(f"pinned path is not a directory: {self.path}")
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd

    @classmethod
    def _from_posix_fd(cls, path: Path, fd: int) -> "_PinnedDirectory":
        instance = cls.__new__(cls)
        instance.path = Path(path)
        instance._fd = fd
        instance._kernel32 = None
        instance._win_handle = None
        return instance

    def ensure_child(self, name: str, label: str) -> "_PinnedDirectory":
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            raise AtesStoreError(f"invalid {label} name: {name!r}")
        candidate = self.path / name

        if os.name == "nt":
            try:
                candidate.mkdir()
            except FileExistsError:
                pass
            except OSError as exc:
                raise AtesStoreError(
                    f"cannot create {label}: {candidate}: {exc}"
                ) from exc
            # This is a no-op on Windows, but keeping the barrier unconditional
            # mirrors the POSIX retry contract below.
            self.fsync()
            return _PinnedDirectory(candidate)

        assert self._fd is not None
        try:
            os.mkdir(name, 0o700, dir_fd=self._fd)
        except FileExistsError:
            pass
        except OSError as exc:
            raise AtesStoreError(
                f"cannot create {label}: {candidate}: {exc}"
            ) from exc

        # Always re-establish the parent durability barrier. A previous mkdir
        # can have succeeded while its parent fsync failed, leaving an existing
        # child whose namespace entry has never been proven durable.
        self.fsync()

        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            child_fd = os.open(name, flags, dir_fd=self._fd)
        except OSError as exc:
            raise AtesStoreError(
                f"{label} cannot be opened as a non-link directory: {candidate}: {exc}"
            ) from exc
        try:
            if not stat.S_ISDIR(os.fstat(child_fd).st_mode):
                raise AtesStoreError(f"{label} is not a directory: {candidate}")
        except BaseException:
            os.close(child_fd)
            raise
        return _PinnedDirectory._from_posix_fd(candidate, child_fd)

    def assert_child_identity(
        self,
        name: str,
        child: "_PinnedDirectory",
        label: str,
    ) -> None:
        """Require a named child to still refer to the pinned child inode."""
        if os.name == "nt":
            # Windows directory handles are opened without FILE_SHARE_DELETE,
            # so rename/replacement is denied while the pinned handle lives.
            return
        assert self._fd is not None
        assert child._fd is not None
        try:
            named = os.stat(name, dir_fd=self._fd, follow_symlinks=False)
            pinned = os.fstat(child._fd)
        except OSError as exc:
            raise AtesStoreError(
                f"cannot verify {label} namespace identity: {self.path / name}: {exc}"
            ) from exc
        if (
            not stat.S_ISDIR(named.st_mode)
            or (named.st_dev, named.st_ino) != (pinned.st_dev, pinned.st_ino)
        ):
            raise AtesStoreError(
                f"{label} namespace no longer refers to the pinned directory: "
                f"{self.path / name}"
            )

    def assert_file_identity(self, name: str, fd: int, label: str) -> None:
        """Require a named regular file to still refer to a pinned descriptor."""
        if os.name == "nt":
            # Windows files are opened without FILE_SHARE_DELETE, so replacing
            # the canonical path is denied while the authoritative handle lives.
            return
        assert self._fd is not None
        try:
            named = os.stat(name, dir_fd=self._fd, follow_symlinks=False)
            pinned = os.fstat(fd)
        except OSError as exc:
            raise AtesStoreError(
                f"cannot verify {label} namespace identity: {self.path / name}: {exc}"
            ) from exc
        if (
            not stat.S_ISREG(named.st_mode)
            or (named.st_dev, named.st_ino) != (pinned.st_dev, pinned.st_ino)
        ):
            raise AtesStoreError(
                f"{label} namespace no longer refers to the pinned file: "
                f"{self.path / name}"
            )

    def fsync(self) -> None:
        if os.name == "nt":
            return
        assert self._fd is not None
        try:
            os.fsync(self._fd)
        except OSError as exc:
            raise AtesStoreError(f"cannot sync directory {self.path}: {exc}") from exc

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        if self._win_handle is not None:
            assert self._kernel32 is not None
            self._kernel32.CloseHandle(self._win_handle)
            self._win_handle = None

    def __del__(self) -> None:
        # Raw POSIX directory descriptors are integers and therefore do not
        # close themselves when an abandoned store becomes unreachable.
        try:
            self.close()
        except BaseException:
            pass


class _RunDirectoryChain:
    """Pinned project-to-run hierarchy retained for the store lifetime."""

    def __init__(self, project_dir: Path, run_id: RunId) -> None:
        self._directories: list[_PinnedDirectory] = []
        self._namespace_lock = None
        try:
            try:
                project_path = Path(project_dir).resolve(strict=True)
            except OSError as exc:
                raise AtesStoreError(
                    f"cannot resolve project_dir {project_dir}: {exc}"
                ) from exc
            if not project_path.is_dir():
                raise AtesStoreError(f"project_dir is not a directory: {project_path}")

            project = _PinnedDirectory(project_path)
            self._directories.append(project)
            argus = project.ensure_child(".argus", ".argus")
            self._directories.append(argus)
            runs = argus.ensure_child("runs", ".argus/runs")
            self._directories.append(runs)
            self.runs = runs
            self.run_name = _run_directory_key(run_id)

            # Acquire per-RunId authority in the stable parent namespace before
            # creating/opening the replaceable run-directory entry itself.
            self._namespace_lock = _RunNamespaceLock(runs, run_id)

            run = runs.ensure_child(self.run_name, "ATES run directory")
            self._directories.append(run)
            self.run = run
            self.assert_authoritative()
        except BaseException:
            try:
                self.close()
            except BaseException:
                pass
            raise

    def assert_authoritative(self) -> None:
        if self._namespace_lock is None:
            raise AtesStoreError("ATES run namespace authority is unavailable")
        self._namespace_lock.assert_authoritative()
        self.runs.assert_child_identity(
            self.run_name,
            self.run,
            "ATES run directory",
        )

    def close(self) -> None:
        first_error: Optional[BaseException] = None
        if self._namespace_lock is not None:
            try:
                self._namespace_lock.close()
            except BaseException as exc:
                first_error = exc
            self._namespace_lock = None
        for directory in reversed(self._directories):
            try:
                directory.close()
            except BaseException as exc:  # cleanup must continue through all handles
                if first_error is None:
                    first_error = exc
        self._directories.clear()
        if first_error is not None:
            raise first_error


def _validate_regular_file_descriptor(fd: int, path: Path) -> None:
    try:
        info = os.fstat(fd)
    except OSError as exc:
        raise AtesStoreError(f"cannot inspect ATES event-store file {path}: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise AtesStoreError(f"event-store path is not a regular file: {path}")
    if info.st_nlink != 1:
        raise AtesStoreError(
            f"event-store file must have exactly one hard link: {path}"
        )


def _raise_lock_acquisition_error(exc: OSError, owner: str) -> None:
    """Classify only genuine non-blocking lock contention as store busy."""
    if exc.errno in (errno.EACCES, errno.EAGAIN):
        raise AtesStoreBusy(
            f"another authoritative ATES writer owns {owner}"
        ) from exc
    raise AtesStoreError(
        f"cannot acquire ATES writer authority for {owner}: {exc}"
    ) from exc


def _open_regular_file(directory: _PinnedDirectory, name: str):
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise AtesStoreError(f"invalid event-store filename: {name!r}")
    path = directory.path / name

    if os.name == "nt":
        kernel32, handle, created = _windows_handle_info(
            path, directory=False, create=True
        )
        try:
            import msvcrt

            flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
            fd = msvcrt.open_osfhandle(handle, flags)
            handle = None
            try:
                _validate_regular_file_descriptor(fd, path)
                return os.fdopen(fd, "r+b", buffering=0), created
            except BaseException:
                os.close(fd)
                raise
        except BaseException:
            if handle is not None:
                kernel32.CloseHandle(handle)
            raise

    assert directory._fd is not None
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(
            name,
            flags | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=directory._fd,
        )
        created = True
    except FileExistsError:
        try:
            fd = os.open(name, flags, dir_fd=directory._fd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise AtesStoreError(
                    f"event-store file cannot be a symlink or reparse point: {path}"
                ) from exc
            raise AtesStoreError(
                f"cannot open ATES event-store file {path}: {exc}"
            ) from exc
        created = False
    except OSError as exc:
        raise AtesStoreError(
            f"cannot create ATES event-store file {path}: {exc}"
        ) from exc

    try:
        _validate_regular_file_descriptor(fd, path)
        return os.fdopen(fd, "r+b", buffering=0), created
    except BaseException:
        os.close(fd)
        raise


class _RunNamespaceLock:
    """Per-run authority anchored in the pinned ``runs`` parent namespace."""

    def __init__(self, directory: _PinnedDirectory, run_id: RunId) -> None:
        self._directory = directory
        self._name = _run_authority_filename(run_id)
        self.path = directory.path / self._name
        self._handle = None
        self._locked = False
        self._owner_pid = os.getpid()

        # Windows already pins the run directory with a non-delete-sharing
        # handle, so the POSIX parent authority is unnecessary there.
        if os.name == "nt":
            return

        handle, created = _open_regular_file(directory, self._name)
        self._handle = handle
        try:
            if created or os.fstat(handle.fileno()).st_size == 0:
                handle.seek(0)
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
                if created:
                    directory.fsync()

            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                _raise_lock_acquisition_error(exc, str(run_id))
            self._locked = True
            self.assert_authoritative()
        except BaseException:
            try:
                if self._handle is not None and not self._handle.closed:
                    self._handle.close()
            finally:
                self._handle = None
                self._locked = False
            raise

    def assert_authoritative(self) -> None:
        if os.name == "nt":
            return
        if os.getpid() != self._owner_pid:
            raise AtesStoreError("ATES run namespace authority was inherited across a fork")
        if self._handle is None or self._handle.closed or not self._locked:
            raise AtesStoreError("ATES run namespace authority is no longer held")
        assert self._directory._fd is not None
        _validate_regular_file_descriptor(self._handle.fileno(), self.path)
        try:
            named = os.stat(
                self._name,
                dir_fd=self._directory._fd,
                follow_symlinks=False,
            )
            pinned = os.fstat(self._handle.fileno())
        except OSError as exc:
            raise AtesStoreError(
                f"cannot verify ATES run namespace authority entry {self.path}: {exc}"
            ) from exc
        if (
            not stat.S_ISREG(named.st_mode)
            or (named.st_dev, named.st_ino) != (pinned.st_dev, pinned.st_ino)
        ):
            raise AtesStoreError(
                f"ATES run namespace authority entry was replaced: {self.path}"
            )

    def close(self) -> None:
        if self._handle is None or self._handle.closed:
            self._handle = None
            self._locked = False
            return
        inherited = os.getpid() != self._owner_pid
        first_error: Optional[BaseException] = None
        try:
            if self._locked and not inherited and os.name != "nt":
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except BaseException as exc:
            first_error = exc
        finally:
            self._locked = False
            try:
                self._handle.close()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
            self._handle = None
        if first_error is not None:
            raise first_error


class _WriterLock:
    """Cross-process single-authority lock retained for the store lifetime.

    On POSIX, the run-directory inode and visible marker remain additional
    authority barriers beneath the parent-scoped per-RunId namespace lock.
    """

    def __init__(self, directory: _PinnedDirectory) -> None:
        path = directory.path / ".ates-writer.lock"
        self._directory = directory
        self._handle = None
        self._locked = False
        self._directory_locked = False
        self._owner_pid = os.getpid()

        # Acquire the run-directory inode lock before touching the replaceable
        # marker pathname. This remains a second independent barrier beneath
        # the parent-scoped namespace lock.
        if os.name != "nt":
            assert directory._fd is not None
            import fcntl

            try:
                fcntl.flock(directory._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                _raise_lock_acquisition_error(exc, directory.path.name)
            self._directory_locked = True

        try:
            handle, created = _open_regular_file(directory, ".ates-writer.lock")
            self._handle = handle

            try:
                if created or os.fstat(self._handle.fileno()).st_size == 0:
                    self._handle.seek(0)
                    self._handle.write(b"\0")
                    self._handle.flush()
                    os.fsync(self._handle.fileno())
                    if created:
                        directory.fsync()
            except BaseException as exc:
                if not isinstance(exc, Exception):
                    raise
                if isinstance(exc, AtesStoreError):
                    raise
                raise AtesStoreError(
                    f"cannot initialize ATES writer lock {path}: {exc}"
                ) from exc

            try:
                if os.name == "nt":
                    import msvcrt

                    self._handle.seek(0)
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    # Retain the marker flock as an additional compatibility
                    # barrier and for the existing on-disk lock contract.
                    fcntl.flock(
                        self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                    )
            except OSError as exc:
                _raise_lock_acquisition_error(exc, directory.path.name)
            self._locked = True
        except BaseException:
            if self._handle is not None and not self._handle.closed:
                try:
                    self._handle.close()
                except BaseException:
                    pass
            self._handle = None
            self._release_directory_lock(suppress_errors=True)
            raise

    def assert_authoritative(self) -> None:
        """Fail closed if this store no longer holds its authority handles."""
        if os.getpid() != self._owner_pid:
            raise AtesStoreError("ATES writer authority was inherited across a fork")
        if self._handle is None or self._handle.closed or not self._locked:
            raise AtesStoreError("ATES writer authority is no longer held")
        if os.name != "nt" and not self._directory_locked:
            raise AtesStoreError("ATES writer directory authority is no longer held")

    def _release_directory_lock(self, *, suppress_errors: bool) -> None:
        if not self._directory_locked:
            return
        inherited = os.getpid() != self._owner_pid
        error: Optional[BaseException] = None
        try:
            if not inherited and os.name != "nt":
                import fcntl

                assert self._directory._fd is not None
                fcntl.flock(self._directory._fd, fcntl.LOCK_UN)
        except BaseException as exc:
            error = exc
        finally:
            self._directory_locked = False
        if error is not None and not suppress_errors:
            raise error

    def close(self) -> None:
        inherited = os.getpid() != self._owner_pid
        first_error: Optional[BaseException] = None

        try:
            if (
                self._handle is not None
                and not self._handle.closed
                and self._locked
                and not inherited
            ):
                if os.name == "nt":
                    import msvcrt

                    self._handle.seek(0)
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except BaseException as exc:
            first_error = exc
        finally:
            self._locked = False
            if self._handle is not None and not self._handle.closed:
                try:
                    self._handle.close()
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
            self._handle = None

        try:
            self._release_directory_lock(suppress_errors=False)
        except BaseException as exc:
            if first_error is None:
                first_error = exc

        if first_error is not None:
            raise first_error


class AtesEventStore:
    """Single-authority append-only canonical event writer for one ATES run."""

    def __init__(
        self,
        project_dir: Path,
        run_id: RunId | str,
        *,
        repair_trailing_partial: bool = False,
    ) -> None:
        if not isinstance(repair_trailing_partial, bool):
            raise ValueError("repair_trailing_partial must be a boolean")
        try:
            self.run_id = run_id if isinstance(run_id, RunId) else RunId(run_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("run_id must be a valid RunId") from exc

        self._owner_pid = os.getpid()
        self._thread_lock = threading.RLock()
        self._closed = False
        self._poisoned = False
        self._directories: Optional[_RunDirectoryChain] = None
        self._writer_lock: Optional[_WriterLock] = None
        self._file = None
        self._committed_length: Optional[int] = None

        try:
            self._directories = _RunDirectoryChain(Path(project_dir), self.run_id)
            self.run_dir = self._directories.run.path
            self.path = self.run_dir / "evidence.jsonl"
            self._writer_lock = _WriterLock(self._directories.run)
            self._file, _created = _open_regular_file(
                self._directories.run, "evidence.jsonl"
            )
            # Every successful open establishes a durability barrier before
            # history is trusted. This repairs the retry case where a prior
            # initialization created the directory entry but its fsync failed,
            # and it re-proves durability for a complete event left visible
            # after an earlier uncertain append.
            self._sync_evidence_durability(sync_directory=True)
            self._events = self._load_existing(repair_trailing_partial)
            self._directories.assert_authoritative()
            self._event_by_id = {event.event_id: event for event in self._events}
            self._next_sequence = len(self._events) + 1
        except BaseException:
            self._close_resources(suppress_errors=True)
            self._closed = True
            raise

    @property
    def next_sequence(self) -> int:
        self._ensure_owner_process()
        return self._next_sequence

    @property
    def events(self) -> tuple[StoredEvent, ...]:
        self._ensure_owner_process()
        return tuple(self._events)

    @property
    def poisoned(self) -> bool:
        self._ensure_owner_process()
        return self._poisoned

    def __enter__(self) -> "AtesEventStore":
        self._ensure_usable()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def __del__(self) -> None:
        # Abandoned stores must release raw pinned descriptors and their flocks.
        # Do not take the thread RLock here: finalization can occur after fork or
        # during interpreter teardown. Resource close methods already avoid an
        # explicit LOCK_UN when they detect an inherited child process.
        try:
            if getattr(self, "_closed", True):
                return
            self._close_resources(suppress_errors=True)
            self._closed = True
        except BaseException:
            pass

    def _close_resources(self, *, suppress_errors: bool) -> None:
        first_error: Optional[BaseException] = None
        resources = (
            self._file,
            self._writer_lock,
            self._directories,
        )
        for resource in resources:
            if resource is None:
                continue
            try:
                resource.close()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        self._file = None
        self._writer_lock = None
        self._directories = None
        if first_error is not None and not suppress_errors:
            raise first_error

    def close(self) -> None:
        if os.getpid() != self._owner_pid:
            # A forked child must never explicitly unlock the parent's flocks.
            # It may close only duplicated descriptors/handles and must not
            # touch an inherited RLock that may have been owned by another thread.
            if self._closed:
                return
            try:
                self._close_resources(suppress_errors=False)
            finally:
                self._closed = True
            return

        with self._thread_lock:
            if self._closed:
                return
            try:
                self._close_resources(suppress_errors=False)
            finally:
                self._closed = True

    def append(
        self,
        event_type: EventType | str,
        payload: Optional[Mapping[str, JsonValue]] = None,
        *,
        occurred_at: Optional[datetime] = None,
        event_id: Optional[EventId | str] = None,
    ) -> StoredEvent:
        """Create and durably append the next canonical event."""
        self._ensure_owner_process()
        with self._thread_lock:
            self._ensure_usable()
            chosen_event_id = EventId.new() if event_id is None else EventId(event_id)
            envelope = EventEnvelope(
                ates_version=ATES_VERSION,
                run_id=self.run_id,
                event_id=chosen_event_id,
                sequence=self._next_sequence,
                event_type=event_type,
                occurred_at=(
                    datetime.now(timezone.utc) if occurred_at is None else occurred_at
                ),
            )
            return self.append_event(
                StoredEvent(
                    envelope=envelope,
                    payload={} if payload is None else payload,
                )
            )

    def append_event(self, event: StoredEvent) -> StoredEvent:
        """Append a pre-identified event or replay an identical committed event."""
        self._ensure_owner_process()
        with self._thread_lock:
            self._ensure_usable()
            if not isinstance(event, StoredEvent):
                raise ValueError("event must be a StoredEvent")
            if event.run_id != self.run_id:
                raise AtesEventConflict(
                    "event run_id does not match this ATES store"
                )

            existing_id = self._event_by_id.get(event.event_id)
            if existing_id is not None:
                if existing_id.canonical_line() == event.canonical_line():
                    return self._acknowledge_replay(existing_id)
                raise AtesEventConflict(
                    "event_id already exists with different canonical content"
                )

            if event.sequence < self._next_sequence:
                existing = self._events[event.sequence - 1]
                if existing.canonical_line() == event.canonical_line():
                    return self._acknowledge_replay(existing)
                raise AtesEventConflict(
                    "event sequence already exists with different canonical content"
                )
            if event.sequence != self._next_sequence:
                raise AtesEventConflict(
                    f"event sequence must be gap-free: expected {self._next_sequence}, "
                    f"got {event.sequence}"
                )

            assert self._committed_length is not None
            try:
                self._assert_evidence_state(expected_length=self._committed_length)
            except BaseException:
                # The cached history can no longer be trusted, even though this
                # append has not started I/O. Require a reopen/revalidation.
                self._poisoned = True
                raise

            line = event.canonical_line()
            expected_after = self._committed_length + len(line)
            io_started = False
            try:
                assert self._file is not None
                assert self._directories is not None
                end = self._file.seek(0, os.SEEK_END)
                if end != self._committed_length:
                    raise AtesStoreError(
                        "ATES evidence length changed before append: "
                        f"expected {self._committed_length}, found {end}"
                    )
                io_started = True
                written = self._file.write(line)
                if written != len(line):
                    raise OSError(
                        f"short ATES append: wrote {written} of {len(line)} bytes"
                    )
                self._file.flush()
                os.fsync(self._file.fileno())
                # Re-check both authority namespaces and the canonical evidence
                # entry *after* the durability barrier. A rename/replacement or
                # truncation race cannot safely be reported as append success.
                self._directories.assert_authoritative()
                self._assert_evidence_state(expected_length=expected_after)
            except BaseException as exc:
                if io_started:
                    # Any control-flow interruption after the write can leave
                    # the durable/canonical outcome unknown. Poison first, then
                    # preserve KeyboardInterrupt/SystemExit rather than wrapping them.
                    self._poisoned = True
                if isinstance(exc, (OSError, ValueError, AtesStoreError)):
                    if io_started:
                        raise AtesAppendError(
                            "ATES append durability is unknown for "
                            f"{event.event_id}/{event.sequence}: {exc}",
                            event,
                        ) from exc
                    raise
                raise

            self._events.append(event)
            self._event_by_id[event.event_id] = event
            self._next_sequence += 1
            self._committed_length = expected_after
            return event

    def _assert_evidence_state(self, *, expected_length: Optional[int]) -> None:
        assert self._file is not None
        assert self._directories is not None
        fd = self._file.fileno()
        _validate_regular_file_descriptor(fd, self.path)
        self._directories.run.assert_file_identity(
            "evidence.jsonl",
            fd,
            "ATES evidence file",
        )
        if expected_length is not None:
            try:
                actual_length = os.fstat(fd).st_size
            except OSError as exc:
                raise AtesStoreError(
                    f"cannot inspect ATES evidence length {self.path}: {exc}"
                ) from exc
            if actual_length != expected_length:
                raise AtesStoreError(
                    "ATES evidence length changed outside the authoritative store: "
                    f"expected {expected_length}, found {actual_length}"
                )

    def _sync_evidence_durability(self, *, sync_directory: bool) -> None:
        assert self._file is not None
        assert self._directories is not None
        expected_length = self._committed_length
        self._directories.assert_authoritative()
        self._assert_evidence_state(expected_length=expected_length)
        self._file.flush()
        os.fsync(self._file.fileno())
        if sync_directory:
            self._directories.run.fsync()
        self._directories.assert_authoritative()
        self._assert_evidence_state(expected_length=expected_length)

    def _acknowledge_replay(self, event: StoredEvent) -> StoredEvent:
        try:
            # Exact replay is also a durability acknowledgement. Re-sync the
            # current evidence handle before returning success so a prior
            # uncertain fsync cannot become success merely because bytes are
            # visible in the page cache.
            self._sync_evidence_durability(sync_directory=False)
        except BaseException as exc:
            self._poisoned = True
            if isinstance(exc, (OSError, ValueError, AtesStoreError)):
                raise AtesAppendError(
                    "ATES replay durability is unknown for "
                    f"{event.event_id}/{event.sequence}: {exc}",
                    event,
                ) from exc
            raise
        return event

    def _ensure_owner_process(self) -> None:
        if os.getpid() != self._owner_pid:
            raise AtesStoreError(
                "ATES event store was inherited across a fork; close the inherited "
                "child handle and reopen a fresh store in this process"
            )

    def _ensure_usable(self) -> None:
        self._ensure_owner_process()
        if self._closed:
            raise AtesStoreError("ATES event store is closed")
        if self._poisoned:
            raise AtesStoreError(
                "ATES event store is poisoned after an uncertain append; "
                "close and reopen to reconcile"
            )
        if self._directories is None:
            raise AtesStoreError("ATES run namespace authority is unavailable")
        self._directories.assert_authoritative()
        if self._writer_lock is None:
            raise AtesStoreError("ATES writer authority is unavailable")
        self._writer_lock.assert_authoritative()

    def _read_all(self) -> bytes:
        assert self._file is not None
        self._assert_evidence_state(expected_length=None)
        self._file.seek(0)
        data = self._file.read()
        if not isinstance(data, bytes):
            raise AtesStoreCorruption("event-store read did not return bytes")
        try:
            actual_length = os.fstat(self._file.fileno()).st_size
        except OSError as exc:
            raise AtesStoreError(
                f"cannot inspect ATES evidence after read {self.path}: {exc}"
            ) from exc
        if actual_length != len(data):
            raise AtesStoreError(
                "ATES evidence length changed while reopening history: "
                f"read {len(data)} bytes, descriptor now has {actual_length}"
            )
        self._assert_evidence_state(expected_length=actual_length)
        return data

    def _load_existing(self, repair_trailing_partial: bool) -> list[StoredEvent]:
        data = self._read_all()
        if data and not data.endswith(b"\n"):
            if repair_trailing_partial is not True:
                raise AtesStoreCorruption(
                    "event store has an unterminated trailing record; "
                    "reopen with repair_trailing_partial=True to discard only that tail"
                )
            assert self._file is not None
            assert self._directories is not None
            self._directories.assert_authoritative()
            self._assert_evidence_state(expected_length=len(data))
            cut = data.rfind(b"\n") + 1
            self._file.truncate(cut)
            self._file.flush()
            os.fsync(self._file.fileno())
            self._directories.assert_authoritative()
            self._assert_evidence_state(expected_length=cut)
            data = data[:cut]

        events: list[StoredEvent] = []
        seen_ids: set[EventId] = set()
        for index, line in enumerate(data.splitlines(keepends=True), start=1):
            if line == b"\n":
                raise AtesStoreCorruption(
                    f"blank canonical event line at record {index}"
                )
            if not line.endswith(b"\n"):
                raise AtesStoreCorruption(
                    f"unterminated canonical event line at record {index}"
                )
            document = _decode_json_object(line[:-1])
            event = StoredEvent.from_document(document)
            if event.run_id != self.run_id:
                raise AtesStoreCorruption(
                    f"record {index} belongs to {event.run_id}, expected {self.run_id}"
                )
            if event.sequence != index:
                raise AtesStoreCorruption(
                    f"canonical sequence gap/conflict at record {index}: "
                    f"found sequence {event.sequence}"
                )
            if event.event_id in seen_ids:
                raise AtesStoreCorruption(
                    f"duplicate event_id {event.event_id} at record {index}"
                )
            try:
                canonical_line = event.canonical_line()
            except (TypeError, ValueError, RecursionError) as exc:
                raise AtesStoreCorruption(
                    f"record {index} cannot be canonicalized as ATES UTF-8 JSON: {exc}"
                ) from exc
            if canonical_line != line:
                raise AtesStoreCorruption(
                    f"record {index} is valid JSON but not canonical ATES JSONL bytes"
                )
            seen_ids.add(event.event_id)
            events.append(event)

        self._committed_length = len(data)
        self._assert_evidence_state(expected_length=self._committed_length)
        return events
