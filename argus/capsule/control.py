"""Durable Capsule identity, lifecycle, and control-generation state.

This module deliberately persists no authentication secret. The host registry
records ownership/fencing metadata; the guest state records only the committed
Capsule identity and generation high-watermark.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from enum import Enum
import json
import os
from pathlib import Path
import re
import stat
import threading
from typing import Iterator
from uuid import uuid4

from argus.capsule.base import CapsuleError
from argus.capsule.files import validate_session_id


_CAPSULE_ID_RE = re.compile(r"^cap-[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ENVIRONMENT_ID_RE = re.compile(r"^env-sha256-[0-9a-f]{64}$")
_PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_RECORD_VERSION = "argus-capsule-control-v1"
_GUEST_STATE_VERSION = "argus-guest-control-v1"


class CapsuleExecutionMode(str, Enum):
    ISOLATED = "isolated"
    SHARED_USER = "shared_user"


class CapsuleLifecycleState(str, Enum):
    ALLOCATED = "allocated"
    BOOTING = "booting"
    BOOTSTRAPPING = "bootstrapping"
    ACTIVE = "active"
    QUARANTINING = "quarantining"
    QUARANTINED = "quarantined"
    RECONNECTING = "reconnecting"
    DESTROYING = "destroying"
    DESTROYED = "destroyed"
    RECOVERY_REQUIRED = "recovery_required"
    CONTROL_STATE_CORRUPT = "control_state_corrupt"


def new_capsule_id() -> str:
    return "cap-" + uuid4().hex


def validate_capsule_id(value: str) -> str:
    capsule_id = str(value or "").strip()
    if not _CAPSULE_ID_RE.fullmatch(capsule_id):
        raise CapsuleError("Capsule ID must use canonical cap-<32 lowercase hex> form")
    return capsule_id


def _mode(value: str | CapsuleExecutionMode) -> CapsuleExecutionMode:
    try:
        return CapsuleExecutionMode(str(getattr(value, "value", value)).strip().lower())
    except ValueError as exc:
        raise CapsuleError("Capsule execution mode must be isolated or shared_user") from exc


def _state(value: str | CapsuleLifecycleState) -> CapsuleLifecycleState:
    try:
        return CapsuleLifecycleState(str(getattr(value, "value", value)).strip().lower())
    except ValueError as exc:
        raise CapsuleError("Capsule lifecycle state is unsupported") from exc


def _nonempty(value: str, field: str, *, limit: int = 2048) -> str:
    text = str(value or "").strip()
    if not text or len(text) > limit or any(ord(ch) < 32 for ch in text):
        raise CapsuleError(f"{field} must be non-empty printable text")
    return text


def _canonical_session(value: str) -> str:
    raw = str(value or "")
    session_id = validate_session_id(raw)
    if session_id != raw:
        raise CapsuleError("Capsule session ID must already be canonical")
    return session_id


@dataclass(frozen=True)
class CapsuleControlRecord:
    capsule_id: str
    provider: str
    provider_resource_identity: str
    mutable_disk_identity: str
    environment_id: str
    base_image_sha256: str
    highest_reserved_generation: int = 0
    last_committed_generation_known_by_host: int = 0
    lifecycle_state: str = CapsuleLifecycleState.ALLOCATED.value
    effective_execution_mode: str = CapsuleExecutionMode.ISOLATED.value
    network_policy_identity: str = "host_only"
    created_at: str = ""
    updated_at: str = ""
    record_version: str = _RECORD_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "capsule_id", validate_capsule_id(self.capsule_id))
        provider = str(self.provider or "").strip().lower()
        if not _PROVIDER_ID_RE.fullmatch(provider):
            raise CapsuleError("Capsule provider identity is invalid")
        object.__setattr__(self, "provider", provider)
        for name in ("provider_resource_identity", "mutable_disk_identity"):
            object.__setattr__(self, name, _nonempty(getattr(self, name), name))
        if not _ENVIRONMENT_ID_RE.fullmatch(str(self.environment_id or "")):
            raise CapsuleError("Capsule environment_id is invalid")
        if not _SHA256_RE.fullmatch(str(self.base_image_sha256 or "")):
            raise CapsuleError("Capsule base_image_sha256 is invalid")
        for name in (
            "highest_reserved_generation",
            "last_committed_generation_known_by_host",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise CapsuleError(f"{name} must be a non-negative integer")
        if self.last_committed_generation_known_by_host > self.highest_reserved_generation:
            raise CapsuleError("committed generation cannot exceed reserved generation")
        object.__setattr__(self, "lifecycle_state", _state(self.lifecycle_state).value)
        object.__setattr__(
            self, "effective_execution_mode", _mode(self.effective_execution_mode).value
        )
        object.__setattr__(
            self,
            "network_policy_identity",
            _nonempty(self.network_policy_identity, "network_policy_identity", limit=256),
        )
        if self.record_version != _RECORD_VERSION:
            raise CapsuleError("unsupported Capsule control record version")
        if self.created_at:
            _nonempty(self.created_at, "created_at", limit=128)
        if self.updated_at:
            _nonempty(self.updated_at, "updated_at", limit=128)

    @classmethod
    def from_mapping(cls, raw: dict) -> "CapsuleControlRecord":
        if not isinstance(raw, dict):
            raise CapsuleError("Capsule control record must be an object")
        allowed = set(cls.__dataclass_fields__)
        unknown = sorted(set(raw) - allowed)
        missing = sorted(
            name
            for name, field in cls.__dataclass_fields__.items()
            if field.default is field.default_factory and name not in raw
        )
        if unknown:
            raise CapsuleError(
                "unknown Capsule control record field(s): " + ", ".join(unknown)
            )
        # Dataclass defaults cover versioned optional fields; constructor still
        # enforces every security-sensitive required identity.
        try:
            return cls(**raw)
        except TypeError as exc:
            raise CapsuleError("Capsule control record shape is invalid") from exc

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class GuestControlState:
    capsule_id: str = ""
    highest_committed_generation: int = 0
    last_session_id: str = ""
    last_execution_mode: str = CapsuleExecutionMode.ISOLATED.value
    schema_version: str = _GUEST_STATE_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != _GUEST_STATE_VERSION:
            raise CapsuleError("unsupported guest control-state version")
        if self.capsule_id:
            object.__setattr__(self, "capsule_id", validate_capsule_id(self.capsule_id))
        if (
            isinstance(self.highest_committed_generation, bool)
            or not isinstance(self.highest_committed_generation, int)
            or self.highest_committed_generation < 0
        ):
            raise CapsuleError("guest generation high-watermark is invalid")
        if bool(self.capsule_id) != bool(self.highest_committed_generation):
            raise CapsuleError(
                "guest Capsule identity and generation high-watermark must initialize together"
            )
        if self.last_session_id:
            object.__setattr__(
                self, "last_session_id", _canonical_session(self.last_session_id)
            )
        if self.highest_committed_generation and not self.last_session_id:
            raise CapsuleError("initialized guest control state requires a session ID")
        object.__setattr__(
            self, "last_execution_mode", _mode(self.last_execution_mode).value
        )

    @classmethod
    def from_mapping(cls, raw: dict) -> "GuestControlState":
        if not isinstance(raw, dict):
            raise CapsuleError("guest control state must be an object")
        allowed = set(cls.__dataclass_fields__)
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise CapsuleError(
                "unknown guest control-state field(s): " + ", ".join(unknown)
            )
        try:
            return cls(**raw)
        except TypeError as exc:
            raise CapsuleError("guest control-state shape is invalid") from exc

    def to_dict(self) -> dict:
        return asdict(self)


_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.Lock] = {}


def _process_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PROCESS_LOCKS[key] = lock
        return lock


def _ensure_private_dir(path: Path) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise CapsuleError(f"Capsule control root is not a directory: {path}")
    else:
        path.mkdir(parents=True, mode=0o700)
    if os.name == "posix":
        path.chmod(0o700)


def _atomic_json(path: Path, data: dict) -> None:
    _ensure_private_dir(path.parent)
    temporary = path.parent / f".{path.name}.tmp-{uuid4().hex}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    fd = None
    try:
        fd = os.open(temporary, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as stream:
            json.dump(data, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.close(fd)
        fd = None
        os.replace(temporary, path)
        if os.name == "posix":
            path.chmod(0o600)
            try:
                directory_fd = os.open(
                    path.parent,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                # The file fsync + atomic replace remain authoritative on
                # platforms/filesystems that do not support directory fsync.
                pass
    finally:
        if fd is not None:
            os.close(fd)
        temporary.unlink(missing_ok=True)


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    _ensure_private_dir(path.parent)
    process_lock = _process_lock(path)
    with process_lock:
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise CapsuleError("Capsule control lock must be a regular file")
            if os.name == "posix" and stat.S_IMODE(info.st_mode) & 0o077:
                os.fchmod(fd, 0o600)
            if os.name == "nt":
                import msvcrt

                if info.st_size == 0:
                    os.write(fd, b"0")
                    os.fsync(fd)
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


class CapsuleControlRegistry:
    """Host-side durable Capsule ownership and generation authority."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        _ensure_private_dir(self.root)

    def _record_path(self, capsule_id: str) -> Path:
        return self.root / (validate_capsule_id(capsule_id) + ".json")

    def _lock_path(self, capsule_id: str) -> Path:
        return self.root / (validate_capsule_id(capsule_id) + ".lock")

    def _read_unlocked(self, capsule_id: str) -> CapsuleControlRecord:
        path = self._record_path(capsule_id)
        if path.is_symlink():
            raise CapsuleError("Capsule control record cannot be a symlink")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise CapsuleError(f"Capsule control record is missing: {capsule_id}") from exc
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CapsuleError("Capsule control record is unreadable or corrupt") from exc
        record = CapsuleControlRecord.from_mapping(raw)
        if record.capsule_id != capsule_id:
            raise CapsuleError("Capsule control record identity mismatch")
        return record

    def create(self, record: CapsuleControlRecord) -> None:
        path = self._record_path(record.capsule_id)
        with _file_lock(self._lock_path(record.capsule_id)):
            if path.exists() or path.is_symlink():
                raise CapsuleError("Capsule control record already exists")
            _atomic_json(path, record.to_dict())

    def load(self, capsule_id: str) -> CapsuleControlRecord:
        with _file_lock(self._lock_path(capsule_id)):
            return self._read_unlocked(validate_capsule_id(capsule_id))

    def reserve_generation(
        self,
        capsule_id: str,
        *,
        lifecycle_state: str = CapsuleLifecycleState.BOOTSTRAPPING.value,
    ) -> tuple[CapsuleControlRecord, int]:
        capsule_id = validate_capsule_id(capsule_id)
        with _file_lock(self._lock_path(capsule_id)):
            current = self._read_unlocked(capsule_id)
            generation = current.highest_reserved_generation + 1
            updated = replace(
                current,
                highest_reserved_generation=generation,
                lifecycle_state=_state(lifecycle_state).value,
            )
            _atomic_json(self._record_path(capsule_id), updated.to_dict())
            return updated, generation

    def commit_generation(
        self,
        capsule_id: str,
        generation: int,
        *,
        execution_mode: str,
        lifecycle_state: str = CapsuleLifecycleState.ACTIVE.value,
    ) -> CapsuleControlRecord:
        capsule_id = validate_capsule_id(capsule_id)
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise CapsuleError("committed control generation must be positive")
        with _file_lock(self._lock_path(capsule_id)):
            current = self._read_unlocked(capsule_id)
            if generation > current.highest_reserved_generation:
                raise CapsuleError("cannot commit an unreserved control generation")
            if generation <= current.last_committed_generation_known_by_host:
                raise CapsuleError("stale Capsule control generation cannot be committed")
            updated = replace(
                current,
                last_committed_generation_known_by_host=generation,
                lifecycle_state=_state(lifecycle_state).value,
                effective_execution_mode=_mode(execution_mode).value,
            )
            _atomic_json(self._record_path(capsule_id), updated.to_dict())
            return updated

    def transition(
        self,
        capsule_id: str,
        lifecycle_state: str,
    ) -> CapsuleControlRecord:
        capsule_id = validate_capsule_id(capsule_id)
        with _file_lock(self._lock_path(capsule_id)):
            current = self._read_unlocked(capsule_id)
            updated = replace(
                current,
                lifecycle_state=_state(lifecycle_state).value,
            )
            _atomic_json(self._record_path(capsule_id), updated.to_dict())
            return updated


class GuestControlStateStore:
    """Guest-side generation high-watermark with atomic stale-generation fencing."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        _ensure_private_dir(self.path.parent)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def _read_unlocked(self) -> GuestControlState:
        if not self.path.exists():
            return GuestControlState()
        if self.path.is_symlink():
            raise CapsuleError("guest control-state file cannot be a symlink")
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CapsuleError("guest control state is unreadable or corrupt") from exc
        return GuestControlState.from_mapping(raw)

    def load(self) -> GuestControlState:
        with _file_lock(self.lock_path):
            return self._read_unlocked()

    def commit_generation(
        self,
        *,
        capsule_id: str,
        generation: int,
        session_id: str,
        execution_mode: str,
    ) -> GuestControlState:
        capsule_id = validate_capsule_id(capsule_id)
        session_id = _canonical_session(session_id)
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise CapsuleError("guest control generation must be positive")
        mode = _mode(execution_mode)
        with _file_lock(self.lock_path):
            current = self._read_unlocked()
            if current.capsule_id and current.capsule_id != capsule_id:
                raise CapsuleError("bootstrap Capsule ID contradicts initialized guest state")
            if generation <= current.highest_committed_generation:
                raise CapsuleError("stale guest control generation was rejected")
            updated = GuestControlState(
                capsule_id=capsule_id,
                highest_committed_generation=generation,
                last_session_id=session_id,
                last_execution_mode=mode.value,
            )
            _atomic_json(self.path, updated.to_dict())
            return updated
