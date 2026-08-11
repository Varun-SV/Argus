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
        except (TypeError, ValueError) as exc:
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
    except (TypeError, ValueError) as exc:
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
    except (json.JSONDecodeError, ValueError) as exc:
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
        raise AtesStoreError(f"cannot open pinned filesystem object {path}: winerror {error}")

    created = bool(create and ctypes.get_last_error() != 183)  # ERROR_ALREADY_EXISTS
    info = _ByHandleFileInformation()
    if not get_info(handle, ctypes.byref(info)):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        raise AtesStoreError(f"cannot inspect pinned filesystem object {path}: winerror {error}")

    is_directory = bool(info.dwFileAttributes & 0x00000010)
    is_reparse = bool(info.dwFileAttributes & 0x00000400)
    if is_reparse:
        kernel32.CloseHandle(handle)
        label = "directory" if directory else "event-store file"
        raise AtesStoreError(f"{label} cannot be a symlink or reparse point: {path}")
    if is_directory != directory:
        kernel32.CloseHandle(handle)
        kind = "directory" if directory else "regular file"
        raise AtesStoreError(f"filesystem object is not a {kind}: {path}")
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
        except Exception:
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
                created = True
            except FileExistsError:
                created = False
            except OSError as exc:
                raise AtesStoreError(f"cannot create {label}: {candidate}: {exc}") from exc
            if created:
                self.fsync()
            return _PinnedDirectory(candidate)

        assert self._fd is not None
        try:
            os.mkdir(name, 0o700, dir_fd=self._fd)
            created = True
        except FileExistsError:
            created = False
        except OSError as exc:
            raise AtesStoreError(f"cannot create {label}: {candidate}: {exc}") from exc
        if created:
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
        except Exception:
            os.close(child_fd)
            raise
        return _PinnedDirectory._from_posix_fd(candidate, child_fd)

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


class _RunDirectoryChain:
    """Pinned project-to-run hierarchy retained for the store lifetime."""

    def __init__(self, project_dir: Path, run_id: RunId) -> None:
        self._directories: list[_PinnedDirectory] = []
        try:
            try:
                project_path = Path(project_dir).resolve(strict=True)
            except OSError as exc:
                raise AtesStoreError(f"cannot resolve project_dir {project_dir}: {exc}") from exc
            if not project_path.is_dir():
                raise AtesStoreError(f"project_dir is not a directory: {project_path}")

            project = _PinnedDirectory(project_path)
            self._directories.append(project)
            argus = project.ensure_child(".argus", ".argus")
            self._directories.append(argus)
            runs = argus.ensure_child("runs", ".argus/runs")
            self._directories.append(runs)
            run = runs.ensure_child(_run_directory_key(run_id), "ATES run directory")
            self._directories.append(run)
            self.run = run
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        for directory in reversed(self._directories):
            directory.close()
        self._directories.clear()


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
            return os.fdopen(fd, "r+b", buffering=0), created
        except Exception:
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
        raise AtesStoreError(f"cannot create ATES event-store file {path}: {exc}") from exc

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise AtesStoreError(f"event-store path is not a regular file: {path}")
        return os.fdopen(fd, "r+b", buffering=0), created
    except Exception:
        os.close(fd)
        raise


class _WriterLock:
    """Cross-process single-authority lock retained for the store lifetime."""

    def __init__(self, directory: _PinnedDirectory) -> None:
        handle, created = _open_regular_file(directory, ".ates-writer.lock")
        self._handle = handle
        self._locked = False
        self._owner_pid = os.getpid()
        if created or os.fstat(self._handle.fileno()).st_size == 0:
            self._handle.seek(0)
            self._handle.write(b"\0")
            self._handle.flush()
            os.fsync(self._handle.fileno())
            if created:
                directory.fsync()

        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(
                    self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                )
        except (OSError, IOError) as exc:
            self._handle.close()
            raise AtesStoreBusy(
                f"another authoritative ATES writer owns {directory.path.name}"
            ) from exc
        self._locked = True

    def close(self) -> None:
        if self._handle.closed:
            return
        inherited = os.getpid() != self._owner_pid
        if self._locked and not inherited:
            try:
                if os.name == "nt":
                    import msvcrt

                    self._handle.seek(0)
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            finally:
                self._locked = False
        self._handle.close()


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

        try:
            self._directories = _RunDirectoryChain(Path(project_dir), self.run_id)
            self.run_dir = self._directories.run.path
            self.path = self.run_dir / "evidence.jsonl"
            self._writer_lock = _WriterLock(self._directories.run)
            self._file, created = _open_regular_file(
                self._directories.run, "evidence.jsonl"
            )
            if created:
                self._file.flush()
                os.fsync(self._file.fileno())
                self._directories.run.fsync()
            self._events = self._load_existing(repair_trailing_partial)
            self._event_by_id = {event.event_id: event for event in self._events}
            self._next_sequence = len(self._events) + 1
        except Exception:
            if self._file is not None:
                self._file.close()
            if self._writer_lock is not None:
                self._writer_lock.close()
            if self._directories is not None:
                self._directories.close()
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

    def close(self) -> None:
        if os.getpid() != self._owner_pid:
            # A forked child must never explicitly unlock the parent's flock.
            # It may close only its duplicated descriptors/handles.
            if self._closed:
                return
            try:
                if self._file is not None:
                    self._file.close()
            finally:
                try:
                    if self._writer_lock is not None:
                        self._writer_lock.close()
                finally:
                    if self._directories is not None:
                        self._directories.close()
                    self._closed = True
            return

        with self._thread_lock:
            if self._closed:
                return
            try:
                if self._file is not None:
                    self._file.close()
            finally:
                try:
                    if self._writer_lock is not None:
                        self._writer_lock.close()
                finally:
                    if self._directories is not None:
                        self._directories.close()
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
                    return existing_id
                raise AtesEventConflict(
                    "event_id already exists with different canonical content"
                )

            if event.sequence < self._next_sequence:
                existing = self._events[event.sequence - 1]
                if existing.canonical_line() == event.canonical_line():
                    return existing
                raise AtesEventConflict(
                    "event sequence already exists with different canonical content"
                )
            if event.sequence != self._next_sequence:
                raise AtesEventConflict(
                    f"event sequence must be gap-free: expected {self._next_sequence}, "
                    f"got {event.sequence}"
                )

            line = event.canonical_line()
            try:
                assert self._file is not None
                self._file.seek(0, os.SEEK_END)
                written = self._file.write(line)
                if written != len(line):
                    raise OSError(
                        f"short ATES append: wrote {written} of {len(line)} bytes"
                    )
                self._file.flush()
                os.fsync(self._file.fileno())
            except (OSError, ValueError) as exc:
                # Do not allocate later sequence numbers after an uncertain
                # durable-write outcome. Reopen and reconcile this exact ID.
                self._poisoned = True
                raise AtesAppendError(
                    "ATES append durability is unknown for "
                    f"{event.event_id}/{event.sequence}: {exc}",
                    event,
                ) from exc

            self._events.append(event)
            self._event_by_id[event.event_id] = event
            self._next_sequence += 1
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

    def _read_all(self) -> bytes:
        assert self._file is not None
        self._file.seek(0)
        data = self._file.read()
        if not isinstance(data, bytes):
            raise AtesStoreCorruption("event-store read did not return bytes")
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
            cut = data.rfind(b"\n") + 1
            self._file.truncate(cut)
            self._file.flush()
            os.fsync(self._file.fileno())
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
            except (TypeError, ValueError) as exc:
                raise AtesStoreCorruption(
                    f"record {index} cannot be canonicalized as ATES UTF-8 JSON: {exc}"
                ) from exc
            if canonical_line != line:
                raise AtesStoreCorruption(
                    f"record {index} is valid JSON but not canonical ATES JSONL bytes"
                )
            seen_ids.add(event.event_id)
            events.append(event)
        return events
