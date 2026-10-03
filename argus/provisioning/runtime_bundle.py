"""Offline Argus guest-runtime bundle format and verification.

The environment definition pins the SHA-256 of the complete ZIP bundle. The
bundle manifest additionally commits to the exact payload members through
content_sha256. The manifest itself is excluded from that inner digest so it
can carry the digest without a circular hash dependency.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import stat
from typing import Any, Iterable, Mapping
from uuid import uuid4
import zipfile

from argus.provisioning.integrity import VerifiedFile, verify_regular_file
from argus.provisioning.model import (
    BOOTSTRAP_SERVICE_POLICY_VERSION, RUNTIME_INSTALLATION_POLICY_VERSION,
    GuestRuntimeIdentity, ProvisioningError,
)


BUNDLE_MANIFEST_NAME = "argus-runtime-manifest.json"
BUNDLE_FORMAT_VERSION = "argus-guest-runtime-bundle-v1"
_MAX_MANIFEST_BYTES = 256 * 1024
_MAX_MEMBERS = 4096
_MAX_UNCOMPRESSED_BYTES = 4 * 1024**3


def _safe_member_name(value: str, field: str = "runtime bundle member") -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ProvisioningError(f"{field} must be a non-empty POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ProvisioningError(f"{field} must stay within the runtime bundle")
    if ":" in path.parts[0]:
        raise ProvisioningError(f"{field} must not contain a drive prefix")
    return path.as_posix()


def _sha256_stream(stream) -> tuple[str, int]:
    digest = sha256()
    size = 0
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _payload_content_sha256(entries: list[tuple[str, int, str]]) -> str:
    digest = sha256()
    for name, size, file_sha256 in sorted(entries):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        digest.update(file_sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


@dataclass(frozen=True)
class GuestRuntimeBundleManifest:
    format_version: str
    runtime_version: str
    target_os: str
    target_architecture: str
    bootstrap_schema_version: str
    bootstrap_service_policy_version: str
    installation_policy_version: str
    entrypoint: str
    content_sha256: str
    signature: str | None = None
    signature_algorithm: str | None = None
    signing_key_id: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "GuestRuntimeBundleManifest":
        if not isinstance(value, Mapping):
            raise ProvisioningError("guest runtime manifest must be a mapping")
        allowed = {
            "format_version",
            "runtime_version",
            "target_os",
            "target_architecture",
            "bootstrap_schema_version",
            "bootstrap_service_policy_version",
            "installation_policy_version",
            "entrypoint",
            "content_sha256",
            "signature",
            "signature_algorithm",
            "signing_key_id",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ProvisioningError(
                "unknown guest runtime manifest field(s): " + ", ".join(unknown)
            )
        required = allowed - {"signature", "signature_algorithm", "signing_key_id"}
        missing = sorted(required - set(value))
        if missing:
            raise ProvisioningError(
                "missing guest runtime manifest field(s): " + ", ".join(missing)
            )
        manifest = cls(**dict(value))
        if manifest.format_version != BUNDLE_FORMAT_VERSION:
            raise ProvisioningError("unsupported guest runtime bundle format")
        if len(manifest.content_sha256) != 64 or any(
            ch not in "0123456789abcdef" for ch in manifest.content_sha256
        ):
            raise ProvisioningError("guest runtime content_sha256 is invalid")
        _safe_member_name(manifest.entrypoint, "guest runtime entrypoint")
        return manifest

    def validate_identity(self, identity: GuestRuntimeIdentity) -> None:
        if (self.bootstrap_service_policy_version != BOOTSTRAP_SERVICE_POLICY_VERSION
                or self.installation_policy_version != RUNTIME_INSTALLATION_POLICY_VERSION):
            raise ProvisioningError("guest runtime bundle uses an unsupported security policy; rebuild it")
        expected = {
            "format_version": identity.bundle_format_version,
            "runtime_version": identity.runtime_version,
            "target_os": identity.target_os,
            "target_architecture": identity.target_architecture,
            "bootstrap_schema_version": identity.bootstrap_schema_version,
            "bootstrap_service_policy_version": identity.bootstrap_service_policy_version,
            "installation_policy_version": identity.runtime_installation_policy_version,
        }
        for field, wanted in expected.items():
            if getattr(self, field) != wanted:
                raise ProvisioningError(
                    f"guest runtime manifest {field} contradicts environment identity"
                )


@dataclass(frozen=True)
class VerifiedGuestRuntimeBundle:
    file: VerifiedFile
    manifest: GuestRuntimeBundleManifest
    payload_files: tuple[str, ...]


@dataclass(frozen=True)
class GuestRuntimeBundleBuildResult:
    path: Path
    sha256: str
    manifest: GuestRuntimeBundleManifest


def _inspect_bundle(
    path: Path,
    identity: GuestRuntimeIdentity,
) -> tuple[GuestRuntimeBundleManifest, tuple[str, ...]]:
    try:
        with zipfile.ZipFile(path, "r") as archive:
            members = archive.infolist()
            if len(members) > _MAX_MEMBERS:
                raise ProvisioningError("guest runtime bundle contains too many members")
            names = [member.filename for member in members]
            if len(names) != len(set(names)):
                raise ProvisioningError(
                    "guest runtime bundle contains duplicate member names"
                )
            if names.count(BUNDLE_MANIFEST_NAME) != 1:
                raise ProvisioningError(
                    "guest runtime bundle requires exactly one manifest"
                )

            payload_entries: list[tuple[str, int, str]] = []
            payload_names: list[str] = []
            manifest_raw: bytes | None = None
            total = 0
            for member in members:
                name = _safe_member_name(member.filename)
                unix_mode = member.external_attr >> 16
                if stat.S_ISLNK(unix_mode):
                    raise ProvisioningError(
                        "guest runtime bundle must not contain symlinks"
                    )
                if member.is_dir():
                    continue
                total += member.file_size
                if total > _MAX_UNCOMPRESSED_BYTES:
                    raise ProvisioningError(
                        "guest runtime bundle expands beyond the size limit"
                    )
                with archive.open(member, "r") as stream:
                    if name == BUNDLE_MANIFEST_NAME:
                        if member.file_size > _MAX_MANIFEST_BYTES:
                            raise ProvisioningError(
                                "guest runtime manifest is too large"
                            )
                        manifest_raw = stream.read(_MAX_MANIFEST_BYTES + 1)
                        continue
                    file_sha256, actual_size = _sha256_stream(stream)
                if actual_size != member.file_size:
                    raise ProvisioningError(
                        "guest runtime bundle member size changed while reading"
                    )
                payload_entries.append((name, actual_size, file_sha256))
                payload_names.append(name)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ProvisioningError(
            "guest runtime bundle is not a valid ZIP archive"
        ) from exc

    if manifest_raw is None:
        raise ProvisioningError("guest runtime bundle manifest is missing")
    try:
        raw = json.loads(manifest_raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProvisioningError("guest runtime manifest is invalid JSON") from exc
    manifest = GuestRuntimeBundleManifest.from_mapping(raw)
    manifest.validate_identity(identity)
    if not payload_entries:
        raise ProvisioningError("guest runtime bundle contains no payload files")
    content_sha256 = _payload_content_sha256(payload_entries)
    if manifest.content_sha256 != content_sha256:
        raise ProvisioningError("guest runtime bundle payload digest mismatch")
    if manifest.entrypoint not in set(payload_names):
        raise ProvisioningError(
            "guest runtime entrypoint is not present in the bundle"
        )
    return manifest, tuple(sorted(payload_names))


def verify_guest_runtime_bundle(
    identity: GuestRuntimeIdentity,
    *,
    allowed_roots: Iterable[str | Path] = (),
    path: str | Path | None = None,
) -> VerifiedGuestRuntimeBundle:
    """Verify complete bundle bytes and the manifest/payload identity.

    Security-sensitive callers should stage the source into a private workspace
    and call this function again on those exact staged bytes before attachment.
    """
    candidate = Path(path if path is not None else identity.bundle_path)
    verified = verify_regular_file(
        candidate,
        expected_sha256=identity.runtime_bundle_sha256,
        allowed_roots=allowed_roots,
    )
    manifest, payload_files = _inspect_bundle(verified.path, identity)
    return VerifiedGuestRuntimeBundle(verified, manifest, payload_files)


def create_guest_runtime_bundle(
    payload_root: str | Path,
    output_path: str | Path,
    *,
    runtime_version: str,
    target_os: str,
    target_architecture: str,
    entrypoint: str,
    bootstrap_schema_version: str = "argus-bootstrap-v1",
    bootstrap_service_policy_version: str = BOOTSTRAP_SERVICE_POLICY_VERSION,
    installation_policy_version: str = RUNTIME_INSTALLATION_POLICY_VERSION,
) -> GuestRuntimeBundleBuildResult:
    """Create a deterministic-format offline bundle from a trusted payload tree."""
    root = Path(payload_root).expanduser().resolve()
    output = Path(output_path).expanduser()
    if not root.is_dir():
        raise ProvisioningError(
            "guest runtime payload root must be a directory"
        )
    if output.exists() or output.is_symlink():
        raise ProvisioningError(
            "guest runtime bundle output already exists"
        )
    output.parent.mkdir(parents=True, exist_ok=True)

    files: list[tuple[str, Path]] = []
    for source in sorted(root.rglob("*")):
        relative = source.relative_to(root).as_posix()
        if source.is_symlink():
            raise ProvisioningError(
                "guest runtime payload must not contain symlinks"
            )
        if source.is_dir():
            continue
        info = source.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ProvisioningError(
                "guest runtime payload must contain only regular files"
            )
        files.append((_safe_member_name(relative), source))
    if not files:
        raise ProvisioningError(
            "guest runtime payload must contain at least one file"
        )

    normalized_entrypoint = _safe_member_name(
        entrypoint, "guest runtime entrypoint"
    )
    if normalized_entrypoint not in {name for name, _ in files}:
        raise ProvisioningError(
            "guest runtime entrypoint is not present in payload"
        )

    temp = output.parent / f".{output.name}.building-{uuid4().hex}"
    entries: list[tuple[str, int, str]] = []
    try:
        with zipfile.ZipFile(
            temp, "x", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            for name, source in files:
                zip_info = zipfile.ZipInfo(
                    name, date_time=(1980, 1, 1, 0, 0, 0)
                )
                zip_info.compress_type = zipfile.ZIP_DEFLATED
                zip_info.external_attr = (
                    (0o100755 if name == normalized_entrypoint else 0o100644)
                    << 16
                )
                digest = sha256()
                size = 0
                with source.open("rb") as src, archive.open(
                    zip_info, "w"
                ) as dst:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        dst.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                entries.append((name, size, digest.hexdigest()))

            content_sha256 = _payload_content_sha256(entries)
            manifest = GuestRuntimeBundleManifest(
                format_version=BUNDLE_FORMAT_VERSION,
                runtime_version=runtime_version,
                target_os=target_os,
                target_architecture=target_architecture,
                bootstrap_schema_version=bootstrap_schema_version,
                bootstrap_service_policy_version=bootstrap_service_policy_version,
                installation_policy_version=installation_policy_version,
                entrypoint=normalized_entrypoint,
                content_sha256=content_sha256,
            )
            manifest_info = zipfile.ZipInfo(
                BUNDLE_MANIFEST_NAME,
                date_time=(1980, 1, 1, 0, 0, 0),
            )
            manifest_info.compress_type = zipfile.ZIP_DEFLATED
            manifest_info.external_attr = 0o100644 << 16
            archive.writestr(
                manifest_info,
                json.dumps(
                    asdict(manifest),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8"),
            )
        with temp.open("rb") as stream:
            bundle_sha256, _ = _sha256_stream(stream)
        temp.rename(output)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise

    return GuestRuntimeBundleBuildResult(
        output.resolve(),
        bundle_sha256,
        manifest,
    )
