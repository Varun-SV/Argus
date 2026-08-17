"""Pre-persistence policy and durable byte storage for ATES artifacts.

Binary evidence is classified before its first persistent payload byte. SAFE
bytes may be retained as-is, REDACTED bytes are transformed completely in
memory first, PROTECTED_REF bytes use the protected run namespace, and
SUPPRESSED evidence creates no payload file.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import FrozenSet, Iterator, Optional, Protocol, Sequence

from .core import ArtifactRecord, EvidenceDisposition, SourceCommitment, validate_artifact_path
from .ids import ArtifactId
from .store import (
    AtesEventStore,
    _PinnedDirectory,
    _open_regular_file,
    _validate_regular_file_descriptor,
    _windows_handle_info,
)

ARTIFACT_POLICY_VERSION = "ates-artifact-v1"
ARTIFACT_BYTES_PROFILE = "ates-artifact-final-bytes-v1"
PROTECTED_ARTIFACT_COMMITMENT_PROFILE = "ates-artifact-sha256-hmac-v1"
PROTECTED_ARTIFACT_VERIFICATION_REF = "secret://ates/run-artifact-hmac-key"
_ARTIFACT_HMAC_KEY_FILENAME = ".ates-artifact-hmac-key"
_ARTIFACT_HMAC_KEY_SIZE = 32
_DEFAULT_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_SAFE_POLICY_ID_RE = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_REASON_CODE_RE = re.compile(r"^[a-z][a-z0-9._-]{0,127}$")
_PROTECTED_REF_RE = re.compile(r"^protected://ates/[0-9a-f]{32}$")


def _write_all(handle, data: bytes) -> None:
    view = memoryview(data)
    offset = 0
    while offset < len(view):
        written = handle.write(view[offset:])
        if isinstance(written, bool) or not isinstance(written, int) or written <= 0:
            raise ArtifactCaptureError("artifact write made no forward progress")
        offset += written


class ArtifactCaptureError(RuntimeError):
    """Artifact bytes could not be classified or committed safely."""


class ArtifactContext(str, Enum):
    FAILURE_SCREENSHOT = "failure_screenshot"
    FINDING_SCREENSHOT = "finding_screenshot"
    CHECKPOINT_SCREENSHOT = "checkpoint_screenshot"
    COLLECTED_FILE = "collected_file"


class ArtifactSanitizer(Protocol):
    def sanitize(self, data: bytes, *, context: ArtifactContext, media_type: str) -> bytes:
        ...


@dataclass(frozen=True)
class ArtifactCaptureConfig:
    policy_id: str = ARTIFACT_POLICY_VERSION
    safe_contexts: FrozenSet[ArtifactContext] = frozenset()
    redacted_contexts: FrozenSet[ArtifactContext] = frozenset()
    suppressed_contexts: FrozenSet[ArtifactContext] = frozenset()
    max_artifact_bytes: int = _DEFAULT_MAX_ARTIFACT_BYTES

    def __post_init__(self) -> None:
        policy_id = str(self.policy_id or "").strip()
        if not _SAFE_POLICY_ID_RE.fullmatch(policy_id):
            raise ValueError("artifact policy_id must be a safe non-empty identifier")
        object.__setattr__(self, "policy_id", policy_id)
        normalized = []
        for name in ("safe_contexts", "redacted_contexts", "suppressed_contexts"):
            raw = getattr(self, name)
            if isinstance(raw, (str, bytes, bytearray)):
                raise ValueError(f"{name} must be a set of ArtifactContext values")
            try:
                values = frozenset(
                    item if isinstance(item, ArtifactContext) else ArtifactContext(item)
                    for item in raw
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} contains an unsupported artifact context") from exc
            object.__setattr__(self, name, values)
            normalized.append(values)
        if normalized[0] & normalized[1] or normalized[0] & normalized[2] or normalized[1] & normalized[2]:
            raise ValueError("artifact context classifications must not overlap")
        if isinstance(self.max_artifact_bytes, bool) or not isinstance(self.max_artifact_bytes, int):
            raise ValueError("max_artifact_bytes must be an integer")
        if self.max_artifact_bytes <= 0:
            raise ValueError("max_artifact_bytes must be positive")


class ArtifactCapturePolicy:
    def __init__(
        self,
        config: Optional[ArtifactCaptureConfig] = None,
        *,
        sanitizer: Optional[ArtifactSanitizer] = None,
    ) -> None:
        self._config = config or ArtifactCaptureConfig()
        self._sanitizer = sanitizer

    @classmethod
    def standard(cls) -> "ArtifactCapturePolicy":
        return cls(ArtifactCaptureConfig())

    @property
    def config(self) -> ArtifactCaptureConfig:
        return self._config

    @property
    def sanitizer(self) -> Optional[ArtifactSanitizer]:
        return self._sanitizer

    def snapshot(self) -> "ArtifactCapturePolicy":
        return ArtifactCapturePolicy(
            ArtifactCaptureConfig(
                policy_id=self._config.policy_id,
                safe_contexts=frozenset(self._config.safe_contexts),
                redacted_contexts=frozenset(self._config.redacted_contexts),
                suppressed_contexts=frozenset(self._config.suppressed_contexts),
                max_artifact_bytes=self._config.max_artifact_bytes,
            ),
            sanitizer=self._sanitizer,
        )

    @property
    def policy_id(self) -> str:
        descriptor = "|".join(
            [
                self._config.policy_id,
                "safe=" + ",".join(sorted(item.value for item in self._config.safe_contexts)),
                "redacted=" + ",".join(sorted(item.value for item in self._config.redacted_contexts)),
                "suppressed=" + ",".join(sorted(item.value for item in self._config.suppressed_contexts)),
                f"max={self._config.max_artifact_bytes}",
            ]
        )
        if descriptor == f"{ARTIFACT_POLICY_VERSION}|safe=|redacted=|suppressed=|max={_DEFAULT_MAX_ARTIFACT_BYTES}":
            return ARTIFACT_POLICY_VERSION
        suffix = hashlib.sha256(descriptor.encode("utf-8")).hexdigest()[:12]
        return f"{self._config.policy_id}.{suffix}"

    def disposition_for(self, context: ArtifactContext | str) -> EvidenceDisposition:
        try:
            normalized = context if isinstance(context, ArtifactContext) else ArtifactContext(context)
        except (TypeError, ValueError) as exc:
            raise ArtifactCaptureError("unsupported artifact context") from exc
        if normalized in self._config.safe_contexts:
            return EvidenceDisposition.SAFE
        if normalized in self._config.redacted_contexts:
            return EvidenceDisposition.REDACTED
        if normalized in self._config.suppressed_contexts:
            return EvidenceDisposition.SUPPRESSED
        return EvidenceDisposition.PROTECTED_REF

    def prepare_bytes(
        self,
        data: object,
        *,
        context: ArtifactContext,
        media_type: str,
    ) -> tuple[EvidenceDisposition, Optional[bytes], str]:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise ArtifactCaptureError("artifact payload must be bytes-like")
        snapshot = bytes(data)
        if len(snapshot) > self._config.max_artifact_bytes:
            return EvidenceDisposition.SUPPRESSED, None, "artifact.too_large"
        disposition = self.disposition_for(context)
        if disposition is EvidenceDisposition.SUPPRESSED:
            return disposition, None, "artifact.policy_suppressed"
        if disposition is EvidenceDisposition.REDACTED:
            sanitizer = self._sanitizer
            if sanitizer is None:
                raise ArtifactCaptureError("redacted artifact capture requires an in-memory sanitizer")
            try:
                transformed = sanitizer.sanitize(snapshot, context=context, media_type=media_type)
            except Exception as exc:
                raise ArtifactCaptureError("artifact sanitizer failed") from exc
            if not isinstance(transformed, (bytes, bytearray, memoryview)):
                raise ArtifactCaptureError("artifact sanitizer must return bytes-like data")
            sanitized = bytes(transformed)
            if len(sanitized) > self._config.max_artifact_bytes:
                return EvidenceDisposition.SUPPRESSED, None, "artifact.sanitized_too_large"
            return disposition, sanitized, "artifact.redacted"
        if disposition is EvidenceDisposition.SAFE:
            return disposition, snapshot, "artifact.safe"
        return disposition, snapshot, "artifact.protected"


@dataclass(frozen=True)
class ArtifactSuppression:
    artifact_id: ArtifactId
    context: ArtifactContext
    kind: str
    capture_policy: str
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.artifact_id, ArtifactId):
            raise ValueError("artifact suppression requires an ArtifactId")
        if not isinstance(self.context, ArtifactContext):
            raise ValueError("artifact suppression requires an ArtifactContext")
        if self.kind not in {"screenshot", "collected_file"}:
            raise ValueError("artifact suppression kind is unsupported")
        policy = str(self.capture_policy or "")
        if not _SAFE_POLICY_ID_RE.fullmatch(policy) and not re.fullmatch(
            r"^[a-z][a-z0-9._-]{0,63}\.[0-9a-f]{12}$", policy
        ):
            raise ValueError("artifact suppression capture_policy is invalid")
        if not _REASON_CODE_RE.fullmatch(str(self.reason or "")):
            raise ValueError("artifact suppression reason must be a safe reason code")


@dataclass(frozen=True)
class ArtifactReservation:
    artifact_id: ArtifactId
    context: ArtifactContext
    kind: str
    media_type: str
    relative_path: str
    protected_ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.artifact_id, ArtifactId):
            raise ValueError("artifact reservation requires an ArtifactId")
        if self.context is not ArtifactContext.COLLECTED_FILE:
            raise ValueError("artifact reservations currently support collected files only")
        if self.kind != "collected_file" or self.media_type != "application/octet-stream":
            raise ValueError("artifact reservation kind/media type is invalid")
        canonical = validate_artifact_path("artifacts/" + str(self.relative_path))
        if canonical != "artifacts/" + str(self.relative_path):
            raise ValueError("artifact reservation path must be canonical")
        if not _PROTECTED_REF_RE.fullmatch(str(self.protected_ref or "")):
            raise ValueError("artifact reservation protected_ref is invalid")

    @property
    def artifact_path(self) -> str:
        return "artifacts/" + self.relative_path


@dataclass(frozen=True)
class ArtifactCaptureResult:
    record: Optional[ArtifactRecord] = None
    suppression: Optional[ArtifactSuppression] = None

    def __post_init__(self) -> None:
        if (self.record is None) == (self.suppression is None):
            raise ValueError("artifact capture result must contain exactly one outcome")


class _AtesArtifactTree:
    def __init__(self, store: AtesEventStore, relatives: Sequence[str]) -> None:
        self.store = store
        self.path: Optional[Path] = None
        self._pins: list[_PinnedDirectory] = []
        self._parents: dict[str, _PinnedDirectory] = {}
        self._edges: list[tuple[_PinnedDirectory, str, _PinnedDirectory, str]] = []
        self._closed = False
        self._open(tuple(relatives))

    @staticmethod
    def _validate_relative(relative: str) -> str:
        canonical = validate_artifact_path("artifacts/" + str(relative))
        return canonical[len("artifacts/"):]

    def _open(self, relatives: tuple[str, ...]) -> None:
        self.store._ensure_usable()
        directories = self.store._directories
        if directories is None:
            raise ArtifactCaptureError("ATES event-store directory authority is unavailable")
        directories.assert_authoritative()
        run_pin = directories.run
        try:
            artifacts = run_pin.ensure_child("artifacts", "ATES artifact directory")
            self._pins.append(artifacts)
            self._edges.append((run_pin, "artifacts", artifacts, "ATES artifact directory"))
            self.path = artifacts.path
            self._parents[""] = artifacts
            needed: set[str] = set()
            for raw in relatives:
                relative = self._validate_relative(raw)
                prefix: list[str] = []
                for part in relative.split("/")[:-1]:
                    prefix.append(part)
                    needed.add("/".join(prefix))
            for key in sorted(needed, key=lambda item: (item.count("/"), item)):
                parts = key.split("/")
                parent = self._parents["/".join(parts[:-1])]
                child = parent.ensure_child(parts[-1], "ATES artifact subdirectory")
                self._pins.append(child)
                self._parents[key] = child
                self._edges.append((parent, parts[-1], child, "ATES artifact subdirectory"))
            self.assert_authoritative()
        except BaseException:
            self.close(suppress_errors=True)
            raise

    def assert_authoritative(self) -> None:
        self.store._ensure_usable()
        directories = self.store._directories
        if directories is None:
            raise ArtifactCaptureError("ATES event-store directory authority is unavailable")
        directories.assert_authoritative()
        for parent, name, child, label in self._edges:
            parent.assert_child_identity(name, child, label)
        directories.assert_authoritative()

    def _parent(self, relative: str) -> tuple[_PinnedDirectory, str, str]:
        normalized = self._validate_relative(relative)
        parts = normalized.split("/")
        pin = self._parents.get("/".join(parts[:-1]))
        if pin is None:
            raise ArtifactCaptureError(f"artifact parent was not pinned: {normalized}")
        return pin, parts[-1], normalized

    def lexical_path(self, relative: str) -> Path:
        if self.path is None:
            raise ArtifactCaptureError("artifact tree is closed")
        normalized = self._validate_relative(relative)
        return self.path.joinpath(*normalized.split("/"))

    def open_temp_file(self, relative: str):
        parent, final_name, normalized = self._parent(relative)
        temp_name = f".{final_name}.argus-{uuid.uuid4().hex}.part"
        handle, created = _open_regular_file(parent, temp_name)
        if not created:
            handle.close()
            raise ArtifactCaptureError(f"artifact temporary file already exists: {normalized}")
        return handle, temp_name, self.lexical_path(normalized)

    @staticmethod
    def _rollback_published(parent: _PinnedDirectory, final_name: str) -> None:
        if os.name == "nt":
            (parent.path / final_name).unlink()
        else:
            assert parent._fd is not None
            os.unlink(final_name, dir_fd=parent._fd)
        parent.fsync()

    def commit_temp(self, relative: str, temp_name: str) -> None:
        parent, final_name, normalized = self._parent(relative)
        self.assert_authoritative()
        final_published = False
        if os.name == "nt":
            try:
                os.rename(parent.path / temp_name, parent.path / final_name)
                final_published = True
            except OSError as exc:
                raise ArtifactCaptureError(
                    f"artifact commit failed inside pinned directory: {normalized}: {exc}"
                ) from exc
        else:
            assert parent._fd is not None
            linked = False
            try:
                os.link(
                    temp_name,
                    final_name,
                    src_dir_fd=parent._fd,
                    dst_dir_fd=parent._fd,
                    follow_symlinks=False,
                )
                linked = True
                os.unlink(temp_name, dir_fd=parent._fd)
                final_published = True
            except OSError as exc:
                cleanup_error: Optional[OSError] = None
                if linked:
                    try:
                        os.unlink(final_name, dir_fd=parent._fd)
                    except OSError as cleanup_exc:
                        cleanup_error = cleanup_exc
                if cleanup_error is not None:
                    raise ArtifactCaptureError(
                        "artifact publication became ambiguous and rollback of the final link failed"
                    ) from cleanup_error
                raise ArtifactCaptureError(
                    f"artifact commit failed inside pinned directory: {normalized}: {exc}"
                ) from exc
        try:
            parent.fsync()
            self.assert_authoritative()
        except BaseException as exc:
            cleanup_error: Optional[BaseException] = None
            if final_published:
                try:
                    self._rollback_published(parent, final_name)
                except BaseException as rollback_exc:
                    cleanup_error = rollback_exc
            if cleanup_error is not None:
                raise ArtifactCaptureError(
                    "artifact publication durability became ambiguous and rollback failed"
                ) from cleanup_error
            raise ArtifactCaptureError(
                "artifact publication durability/authority barrier failed and was rolled back"
            ) from exc

    def remove_temp(self, relative: str, temp_name: str) -> None:
        try:
            parent, _final_name, _normalized = self._parent(relative)
        except Exception:
            return
        try:
            if os.name == "nt":
                (parent.path / temp_name).unlink(missing_ok=True)
            else:
                assert parent._fd is not None
                os.unlink(temp_name, dir_fd=parent._fd)
        except OSError:
            pass

    def unlink_relative(self, relative: str) -> None:
        parent, final_name, _normalized = self._parent(relative)
        self._rollback_published(parent, final_name)

    def _open_existing(self, relative: str):
        parent, final_name, normalized = self._parent(relative)
        path = parent.path / final_name
        if os.name == "nt":
            kernel32, raw_handle, _created = _windows_handle_info(path, directory=False, create=False)
            handle_to_close = raw_handle
            try:
                import msvcrt

                fd = msvcrt.open_osfhandle(raw_handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
                handle_to_close = None
                try:
                    _validate_regular_file_descriptor(fd, path)
                    return os.fdopen(fd, "rb", buffering=0)
                except BaseException:
                    os.close(fd)
                    raise
            finally:
                if handle_to_close is not None:
                    kernel32.CloseHandle(handle_to_close)
        assert parent._fd is not None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(final_name, flags, dir_fd=parent._fd)
        except OSError as exc:
            raise ArtifactCaptureError(f"cannot open retained artifact {normalized}: {exc}") from exc
        try:
            _validate_regular_file_descriptor(fd, path)
            return os.fdopen(fd, "rb", buffering=0)
        except BaseException:
            os.close(fd)
            raise

    def digest_existing(self, relative: str) -> tuple[int, str]:
        parent, final_name, normalized = self._parent(relative)
        self.assert_authoritative()
        digest = hashlib.sha256()
        with self._open_existing(normalized) as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ArtifactCaptureError("retained artifact must be a singly-linked regular file")
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            parent.assert_file_identity(final_name, handle.fileno(), "ATES artifact")
            size = int(info.st_size)
        self.assert_authoritative()
        return size, digest.hexdigest()

    def close(self, *, suppress_errors: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        first_error: Optional[BaseException] = None
        try:
            self.assert_authoritative()
        except BaseException as exc:
            first_error = exc
        for pin in reversed(self._pins):
            try:
                pin.close()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        self._pins.clear()
        self._parents.clear()
        self._edges.clear()
        if first_error is not None and not suppress_errors:
            raise first_error


class AtesArtifactRepository:
    def __init__(self, store: AtesEventStore, policy: Optional[ArtifactCapturePolicy] = None) -> None:
        if not isinstance(store, AtesEventStore):
            raise ValueError("artifact repository requires an AtesEventStore")
        self.store = store
        self.policy = (policy or ArtifactCapturePolicy.standard()).snapshot()
        self._reservations: dict[ArtifactId, ArtifactReservation] = {}
        self._artifact_hmac_key: Optional[bytes] = None

    @contextmanager
    def open_tree(self, relatives: Sequence[str]) -> Iterator[_AtesArtifactTree]:
        tree = _AtesArtifactTree(self.store, relatives)
        body_error: Optional[BaseException] = None
        try:
            yield tree
        except BaseException as exc:
            body_error = exc
            raise
        finally:
            try:
                tree.close(suppress_errors=body_error is not None)
            except BaseException:
                if body_error is None:
                    raise

    def _protected_commitment_key(self) -> bytes:
        if self._artifact_hmac_key is not None:
            return self._artifact_hmac_key
        self.store._ensure_usable()
        directories = self.store._directories
        if directories is None:
            raise ArtifactCaptureError("ATES run authority is unavailable for artifact commitment key")
        directories.assert_authoritative()
        handle, created = _open_regular_file(directories.run, _ARTIFACT_HMAC_KEY_FILENAME)
        initializing = bool(created)
        try:
            with handle:
                info = os.fstat(handle.fileno())
                if created or info.st_size == 0:
                    initializing = True
                    key = secrets.token_bytes(_ARTIFACT_HMAC_KEY_SIZE)
                    handle.seek(0)
                    _write_all(handle, key)
                    handle.flush()
                    os.fsync(handle.fileno())
                    if created:
                        directories.run.fsync()
                else:
                    if info.st_size != _ARTIFACT_HMAC_KEY_SIZE:
                        raise ArtifactCaptureError("artifact commitment key has invalid size")
                    handle.seek(0)
                    key = handle.read(_ARTIFACT_HMAC_KEY_SIZE)
                    if len(key) != _ARTIFACT_HMAC_KEY_SIZE:
                        raise ArtifactCaptureError("artifact commitment key is incomplete")
        except BaseException as exc:
            if initializing:
                try:
                    _AtesArtifactTree._rollback_published(
                        directories.run, _ARTIFACT_HMAC_KEY_FILENAME
                    )
                except FileNotFoundError:
                    pass
                except BaseException as cleanup_exc:
                    raise ArtifactCaptureError(
                        "artifact commitment key initialization failed and cleanup was incomplete"
                    ) from cleanup_exc
            raise exc
        directories.assert_authoritative()
        self._artifact_hmac_key = key
        return key

    @staticmethod
    def _kind(value: str) -> str:
        kind = str(value or "").strip().lower()
        if kind not in {"screenshot", "collected_file"}:
            raise ArtifactCaptureError("unsupported artifact kind")
        return kind

    @staticmethod
    def _media_type(value: str, kind: str) -> str:
        media_type = str(value or "").strip().lower()
        allowed = {"screenshot": {"image/png"}, "collected_file": {"application/octet-stream"}}
        if media_type not in allowed[kind]:
            raise ArtifactCaptureError("unsupported artifact media type")
        return media_type

    @staticmethod
    def _extension(kind: str) -> str:
        return ".png" if kind == "screenshot" else ".bin"

    def _relative_for(
        self,
        artifact_id: ArtifactId,
        *,
        kind: str,
        disposition: EvidenceDisposition,
    ) -> str:
        namespace = "protected" if disposition is EvidenceDisposition.PROTECTED_REF else "retained"
        relative = f"{namespace}/{kind}/{artifact_id}{self._extension(kind)}"
        validate_artifact_path("artifacts/" + relative)
        return relative

    def _commitment(self, digest: str, disposition: EvidenceDisposition) -> SourceCommitment:
        if disposition is EvidenceDisposition.PROTECTED_REF:
            try:
                digest_bytes = bytes.fromhex(digest)
            except ValueError as exc:
                raise ArtifactCaptureError("protected artifact SHA-256 is invalid") from exc
            value = hmac.new(
                self._protected_commitment_key(), digest_bytes, hashlib.sha256
            ).hexdigest()
            return SourceCommitment(
                method="hmac-sha256",
                value="hmac:" + value,
                canonicalization_profile=PROTECTED_ARTIFACT_COMMITMENT_PROFILE,
                verification_ref=PROTECTED_ARTIFACT_VERIFICATION_REF,
            )
        return SourceCommitment(
            method="sha256",
            value="sha256:" + digest,
            canonicalization_profile=ARTIFACT_BYTES_PROFILE,
        )

    def _record(
        self,
        *,
        artifact_id: ArtifactId,
        context: ArtifactContext,
        kind: str,
        relative: str,
        disposition: EvidenceDisposition,
        size: int,
        digest: str,
        protected_ref: Optional[str] = None,
    ) -> ArtifactRecord:
        kwargs = {}
        if disposition is EvidenceDisposition.PROTECTED_REF:
            kwargs = {
                "protected_ref": protected_ref or f"protected://ates/{secrets.token_hex(16)}",
                "access_policy": "ates.host-filesystem-protected-v1",
                "retention_policy": "ates.run-retention-v1",
                "authorization_ref": "auth://ates/host-filesystem",
            }
        return ArtifactRecord(
            artifact_id=artifact_id,
            kind=kind,
            path="artifacts/" + relative,
            sensitivity=context.value,
            capture_policy=self.policy.policy_id,
            content_digest=self._commitment(digest, disposition),
            size_bytes=size,
            protection_state=disposition,
            **kwargs,
        )

    def capture_bytes(
        self,
        data: object,
        *,
        context: ArtifactContext,
        kind: str,
        media_type: str,
    ) -> ArtifactCaptureResult:
        kind = self._kind(kind)
        media_type = self._media_type(media_type, kind)
        artifact_id = ArtifactId.new()
        disposition, prepared, reason = self.policy.prepare_bytes(data, context=context, media_type=media_type)
        if disposition is EvidenceDisposition.SUPPRESSED or prepared is None:
            return ArtifactCaptureResult(
                suppression=ArtifactSuppression(
                    artifact_id=artifact_id,
                    context=context,
                    kind=kind,
                    capture_policy=self.policy.policy_id,
                    reason=reason,
                )
            )
        relative = self._relative_for(artifact_id, kind=kind, disposition=disposition)
        temp_name: Optional[str] = None
        published = False
        with self.open_tree((relative,)) as tree:
            handle, temp_name, _destination = tree.open_temp_file(relative)
            try:
                with handle:
                    _write_all(handle, prepared)
                    handle.flush()
                    os.fsync(handle.fileno())
                tree.commit_temp(relative, temp_name)
                published = True
                temp_name = None
                size, digest = tree.digest_existing(relative)
            except BaseException as exc:
                cleanup_error: Optional[BaseException] = None
                if temp_name is not None:
                    tree.remove_temp(relative, temp_name)
                if published:
                    try:
                        tree.unlink_relative(relative)
                    except BaseException as rollback_exc:
                        cleanup_error = rollback_exc
                if cleanup_error is not None:
                    raise ArtifactCaptureError(
                        "unregistered artifact publication failed verification and cleanup"
                    ) from cleanup_error
                raise exc
        protected_ref = (
            f"protected://ates/{secrets.token_hex(16)}"
            if disposition is EvidenceDisposition.PROTECTED_REF
            else None
        )
        try:
            record = self._record(
                artifact_id=artifact_id,
                context=context,
                kind=kind,
                relative=relative,
                disposition=disposition,
                size=size,
                digest=digest,
                protected_ref=protected_ref,
            )
        except BaseException as exc:
            with self.open_tree((relative,)) as tree:
                try:
                    tree.unlink_relative(relative)
                except BaseException as cleanup_exc:
                    raise ArtifactCaptureError(
                        "artifact commitment failed and unregistered payload cleanup failed"
                    ) from cleanup_exc
            raise exc
        return ArtifactCaptureResult(record=record)

    def reserve_protected_collection(self, count: int) -> tuple[ArtifactReservation, ...]:
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("collection reservation count must be a non-negative integer")
        if self.policy.disposition_for(ArtifactContext.COLLECTED_FILE) is not EvidenceDisposition.PROTECTED_REF:
            raise ArtifactCaptureError(
                "runtime mapped collection currently requires protected collected-file policy"
            )
        reservations = []
        for _ in range(count):
            artifact_id = ArtifactId.new()
            if artifact_id in self._reservations:
                raise ArtifactCaptureError("artifact reservation identity collision")
            relative = self._relative_for(
                artifact_id, kind="collected_file", disposition=EvidenceDisposition.PROTECTED_REF
            )
            reservation = ArtifactReservation(
                artifact_id=artifact_id,
                context=ArtifactContext.COLLECTED_FILE,
                kind="collected_file",
                media_type="application/octet-stream",
                relative_path=relative,
                protected_ref=f"protected://ates/{secrets.token_hex(16)}",
            )
            self._reservations[artifact_id] = reservation
            reservations.append(reservation)
        return tuple(reservations)

    def finalize_reserved(
        self,
        tree: _AtesArtifactTree,
        reservation: ArtifactReservation,
        *,
        expected_size: Optional[int] = None,
        expected_sha256: Optional[str] = None,
    ) -> ArtifactRecord:
        if not isinstance(reservation, ArtifactReservation):
            raise ValueError("reservation must be an ArtifactReservation")
        issued = self._reservations.get(reservation.artifact_id)
        if issued is None or issued != reservation:
            raise ArtifactCaptureError(
                "artifact reservation was not issued by this repository or was already finalized"
            )
        size, digest = tree.digest_existing(reservation.relative_path)
        if expected_size is not None and size != int(expected_size):
            raise ArtifactCaptureError("collected artifact size changed before ATES registration")
        if expected_sha256 is not None:
            expected = str(expected_sha256).strip().lower()
            if expected and digest != expected:
                raise ArtifactCaptureError("collected artifact digest changed before ATES registration")
        if size > self.policy.config.max_artifact_bytes:
            raise ArtifactCaptureError("collected artifact exceeds ATES artifact size policy")
        record = self._record(
            artifact_id=reservation.artifact_id,
            context=reservation.context,
            kind=reservation.kind,
            relative=reservation.relative_path,
            disposition=EvidenceDisposition.PROTECTED_REF,
            size=size,
            digest=digest,
            protected_ref=reservation.protected_ref,
        )
        del self._reservations[reservation.artifact_id]
        return record


__all__ = [
    "ARTIFACT_BYTES_PROFILE",
    "ARTIFACT_POLICY_VERSION",
    "PROTECTED_ARTIFACT_COMMITMENT_PROFILE",
    "PROTECTED_ARTIFACT_VERIFICATION_REF",
    "ArtifactCaptureConfig",
    "ArtifactCaptureError",
    "ArtifactCapturePolicy",
    "ArtifactCaptureResult",
    "ArtifactContext",
    "ArtifactReservation",
    "ArtifactSanitizer",
    "ArtifactSuppression",
    "AtesArtifactRepository",
]
