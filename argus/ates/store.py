"""Durable append-only ATES event storage.

This module owns the canonical local ``evidence.jsonl`` stream for one ATES
run. Runtime wiring, evidence manifests, report rendering, and Fleet transport
remain later layers.
"""

from __future__ import annotations

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


def _is_link_or_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    attrs = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse and attrs & reparse)


def _ensure_directory(parent: Path, name: str, label: str) -> Path:
    candidate = parent / name
    try:
        candidate.mkdir()
    except FileExistsError:
        pass
    except OSError as exc:
        raise AtesStoreError(f"cannot create {label}: {candidate}: {exc}") from exc

    if _is_link_or_reparse(candidate):
        raise AtesStoreError(
            f"{label} cannot be a symlink or reparse point: {candidate}"
        )
    if not candidate.is_dir():
        raise AtesStoreError(f"{label} is not a directory: {candidate}")

    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(parent)
    except ValueError as exc:
        raise AtesStoreError(
            f"{label} escapes its parent directory: {candidate}"
        ) from exc
    return resolved


def _safe_run_directory(project_dir: Path, run_id: RunId) -> Path:
    project = Path(project_dir).resolve(strict=True)
    if not project.is_dir():
        raise AtesStoreError(f"project_dir is not a directory: {project}")
    argus = _ensure_directory(project, ".argus", ".argus")
    runs = _ensure_directory(argus, "runs", ".argus/runs")
    return _ensure_directory(runs, str(run_id), "ATES run directory")


def _open_regular_file(path: Path):
    existed = path.exists()
    if existed and _is_link_or_reparse(path):
        raise AtesStoreError(
            f"event-store file cannot be a symlink or reparse point: {path}"
        )

    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise AtesStoreError(
            f"cannot open ATES event-store file {path}: {exc}"
        ) from exc

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise AtesStoreError(f"event-store path is not a regular file: {path}")
        return os.fdopen(fd, "r+b", buffering=0), not existed
    except Exception:
        os.close(fd)
        raise


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise AtesStoreError(
            f"cannot open directory for durability sync {path}: {exc}"
        ) from exc
    try:
        os.fsync(fd)
    except OSError as exc:
        raise AtesStoreError(f"cannot sync directory {path}: {exc}") from exc
    finally:
        os.close(fd)


class _WriterLock:
    """Cross-process single-authority lock retained for the store lifetime."""

    def __init__(self, path: Path) -> None:
        handle, created = _open_regular_file(path)
        self._handle = handle
        self._locked = False
        if created or os.fstat(self._handle.fileno()).st_size == 0:
            self._handle.seek(0)
            self._handle.write(b"\0")
            self._handle.flush()
            os.fsync(self._handle.fileno())
            if created:
                _fsync_directory(path.parent)

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
                f"another authoritative ATES writer owns {path.parent.name}"
            ) from exc
        self._locked = True

    def close(self) -> None:
        if self._handle.closed:
            return
        if self._locked:
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
        try:
            self.run_id = run_id if isinstance(run_id, RunId) else RunId(run_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("run_id must be a valid RunId") from exc

        self.run_dir = _safe_run_directory(Path(project_dir), self.run_id)
        self.path = self.run_dir / "evidence.jsonl"
        self._thread_lock = threading.RLock()
        self._closed = False
        self._poisoned = False
        self._writer_lock = _WriterLock(self.run_dir / ".ates-writer.lock")

        try:
            self._file, created = _open_regular_file(self.path)
            if created:
                self._file.flush()
                os.fsync(self._file.fileno())
                _fsync_directory(self.run_dir)
            self._events = self._load_existing(repair_trailing_partial)
            self._event_by_id = {event.event_id: event for event in self._events}
            self._next_sequence = len(self._events) + 1
        except Exception:
            self._writer_lock.close()
            raise

    @property
    def next_sequence(self) -> int:
        return self._next_sequence

    @property
    def events(self) -> tuple[StoredEvent, ...]:
        return tuple(self._events)

    @property
    def poisoned(self) -> bool:
        return self._poisoned

    def __enter__(self) -> "AtesEventStore":
        self._ensure_usable()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        with self._thread_lock:
            if self._closed:
                return
            try:
                self._file.close()
            finally:
                self._writer_lock.close()
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
        with self._thread_lock:
            self._ensure_usable()
            chosen_event_id = EventId.new() if event_id is None else EventId(event_id)
            envelope = EventEnvelope(
                ates_version=ATES_VERSION,
                run_id=self.run_id,
                event_id=chosen_event_id,
                sequence=self._next_sequence,
                event_type=event_type,
                occurred_at=occurred_at or datetime.now(timezone.utc),
            )
            return self.append_event(
                StoredEvent(envelope=envelope, payload=payload or {})
            )

    def append_event(self, event: StoredEvent) -> StoredEvent:
        """Append a pre-identified event or replay an identical committed event."""
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

    def _ensure_usable(self) -> None:
        if self._closed:
            raise AtesStoreError("ATES event store is closed")
        if self._poisoned:
            raise AtesStoreError(
                "ATES event store is poisoned after an uncertain append; "
                "close and reopen to reconcile"
            )

    def _read_all(self) -> bytes:
        self._file.seek(0)
        data = self._file.read()
        if not isinstance(data, bytes):
            raise AtesStoreCorruption("event-store read did not return bytes")
        return data

    def _load_existing(self, repair_trailing_partial: bool) -> list[StoredEvent]:
        data = self._read_all()
        if data and not data.endswith(b"\n"):
            if not repair_trailing_partial:
                raise AtesStoreCorruption(
                    "event store has an unterminated trailing record; "
                    "reopen with repair_trailing_partial=True to discard only that tail"
                )
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
            if event.canonical_line() != line:
                raise AtesStoreCorruption(
                    f"record {index} is valid JSON but not canonical ATES JSONL bytes"
                )
            seen_ids.add(event.event_id)
            events.append(event)
        return events
