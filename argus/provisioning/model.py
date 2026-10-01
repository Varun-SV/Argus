"""Declarative, content-addressed OS environment definitions for Argus provisioning."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import re
from typing import Any, Mapping, Optional


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_ALLOWED_ARCH = {"x86_64", "aarch64"}
_ALLOWED_FIRMWARE = {"bios", "uefi"}
_ALLOWED_DISK_BUS = {"ide", "sata", "scsi", "virtio", "nvme"}
_ALLOWED_NETWORK = {"isolated", "host_only"}
_ALLOWED_IMAGE_FORMAT = {"vhdx", "qcow2", "raw"}


class ProvisioningError(ValueError):
    """Raised when an environment definition or derived image is unsafe/invalid."""


def _text(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ProvisioningError(f"{field} must be a string")
    normalized = value.strip()
    if not allow_empty and not normalized:
        raise ProvisioningError(f"{field} must not be empty")
    return normalized


def _digest(value: Any, field: str) -> str:
    normalized = _text(value, field).lower()
    if not _SHA256_RE.fullmatch(normalized):
        raise ProvisioningError(f"{field} must be a lowercase 64-character SHA-256 digest")
    return normalized


def _positive_int(value: Any, field: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ProvisioningError(f"{field} must be an integer >= {minimum}")
    return value


def _strict_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ProvisioningError(f"{field} must be a boolean")
    return value


def _enum(value: Any, field: str, allowed: set[str]) -> str:
    normalized = _text(value, field).lower()
    if normalized not in allowed:
        raise ProvisioningError(
            f"{field} must be one of: {', '.join(sorted(allowed))}"
        )
    return normalized


def _optional_text(value: Any, field: str) -> Optional[str]:
    if value is None:
        return None
    return _text(value, field)


def _tuple_of_text(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ProvisioningError(f"{field} must be a list")
    result = tuple(_text(item, field) for item in value)
    if len(result) != len(set(result)):
        raise ProvisioningError(f"{field} must not contain duplicates")
    return result


@dataclass(frozen=True)
class InstallationMediaSource:
    """User-supplied installation media.

    The path is a host-local locator only and deliberately does not participate
    in the content identity. The SHA-256 digest is the immutable source identity.
    """

    path: str
    sha256: str
    media_type: str = "iso"
    architecture: str = "x86_64"

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _text(self.path, "source.path"))
        object.__setattr__(self, "sha256", _digest(self.sha256, "source.sha256"))
        object.__setattr__(
            self,
            "media_type",
            _enum(self.media_type, "source.media_type", {"iso"}),
        )
        object.__setattr__(
            self,
            "architecture",
            _enum(self.architecture, "source.architecture", _ALLOWED_ARCH),
        )

    def identity_dict(self) -> dict[str, Any]:
        return {
            "kind": "installation_media",
            "media_type": self.media_type,
            "sha256": self.sha256,
            "architecture": self.architecture,
        }


@dataclass(frozen=True)
class GuestRuntimeIdentity:
    """Content identity plus host-local locator for the offline Argus guest runtime.

    bundle_path is deliberately excluded from identity_dict. The bundle digest
    and versioned runtime/bootstrap policies are the reusable environment
    identity; moving the same approved bundle on the host must not invalidate a
    golden image definition.
    """

    bundle_path: str
    runtime_bundle_sha256: str
    runtime_version: str
    target_os: str
    target_architecture: str = "x86_64"
    bundle_format_version: str = "argus-guest-runtime-bundle-v1"
    bootstrap_schema_version: str = "argus-bootstrap-v1"
    bootstrap_service_policy_version: str = "argus-bootstrap-service-v1"
    runtime_installation_policy_version: str = "argus-runtime-install-v1"

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "bundle_path", _text(self.bundle_path, "guest_runtime.bundle_path")
        )
        object.__setattr__(
            self,
            "runtime_bundle_sha256",
            _digest(
                self.runtime_bundle_sha256,
                "guest_runtime.runtime_bundle_sha256",
            ),
        )
        for field_name in (
            "runtime_version",
            "bundle_format_version",
            "bootstrap_schema_version",
            "bootstrap_service_policy_version",
            "runtime_installation_policy_version",
        ):
            value = _text(getattr(self, field_name), f"guest_runtime.{field_name}")
            if not _NAME_RE.fullmatch(value):
                raise ProvisioningError(
                    f"guest_runtime.{field_name} must be a portable version identifier"
                )
            object.__setattr__(self, field_name, value)
        if self.bundle_format_version != "argus-guest-runtime-bundle-v1":
            raise ProvisioningError(
                "guest_runtime.bundle_format_version must be "
                "'argus-guest-runtime-bundle-v1'"
            )
        object.__setattr__(
            self,
            "target_os",
            _enum(
                self.target_os,
                "guest_runtime.target_os",
                {"ubuntu", "windows-11"},
            ),
        )
        object.__setattr__(
            self,
            "target_architecture",
            _enum(
                self.target_architecture,
                "guest_runtime.target_architecture",
                _ALLOWED_ARCH,
            ),
        )

    def identity_dict(self) -> dict[str, Any]:
        return {
            "kind": "guest_runtime",
            "runtime_bundle_sha256": self.runtime_bundle_sha256,
            "runtime_version": self.runtime_version,
            "bundle_format_version": self.bundle_format_version,
            "target_os": self.target_os,
            "target_architecture": self.target_architecture,
            "bootstrap_schema_version": self.bootstrap_schema_version,
            "bootstrap_service_policy_version": self.bootstrap_service_policy_version,
            "runtime_installation_policy_version": self.runtime_installation_policy_version,
        }

    @property
    def identity_sha256(self) -> str:
        payload = json.dumps(
            self.identity_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        return sha256(payload).hexdigest()

    @property
    def runtime_identity(self) -> str:
        return f"runtime-sha256-{self.identity_sha256}"


@dataclass(frozen=True)
class MachineSpec:
    """Requested virtual hardware/firmware contract for a provisioned OS."""

    architecture: str = "x86_64"
    cpu_count: int = 2
    memory_mb: int = 4096
    firmware: str = "uefi"
    secure_boot: bool = False
    tpm_version: Optional[str] = None
    disk_size_gib: int = 64
    disk_bus: str = "virtio"
    network_mode: str = "isolated"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "architecture",
            _enum(self.architecture, "machine.architecture", _ALLOWED_ARCH),
        )
        object.__setattr__(
            self, "cpu_count", _positive_int(self.cpu_count, "machine.cpu_count")
        )
        object.__setattr__(
            self,
            "memory_mb",
            _positive_int(self.memory_mb, "machine.memory_mb", minimum=256),
        )
        object.__setattr__(
            self,
            "firmware",
            _enum(self.firmware, "machine.firmware", _ALLOWED_FIRMWARE),
        )
        object.__setattr__(
            self, "secure_boot", _strict_bool(self.secure_boot, "machine.secure_boot")
        )
        if self.secure_boot and self.firmware != "uefi":
            raise ProvisioningError("machine.secure_boot requires UEFI firmware")
        if self.tpm_version is not None:
            version = _text(self.tpm_version, "machine.tpm_version")
            if version not in {"1.2", "2.0"}:
                raise ProvisioningError("machine.tpm_version must be 1.2 or 2.0")
            object.__setattr__(self, "tpm_version", version)
        object.__setattr__(
            self,
            "disk_size_gib",
            _positive_int(self.disk_size_gib, "machine.disk_size_gib"),
        )
        object.__setattr__(
            self,
            "disk_bus",
            _enum(self.disk_bus, "machine.disk_bus", _ALLOWED_DISK_BUS),
        )
        object.__setattr__(
            self,
            "network_mode",
            _enum(self.network_mode, "machine.network_mode", _ALLOWED_NETWORK),
        )


@dataclass(frozen=True)
class InstallationSpec:
    """Non-secret unattended installation inputs."""

    unattended: bool = True
    edition: Optional[str] = None
    locale: str = "en-US"
    timezone: str = "UTC"
    packages: tuple[str, ...] = ()
    update_policy: str = "frozen"
    credential_ref: Optional[str] = None
    # These assertions are part of the immutable definition. Providers must
    # also verify the installed guest before claiming the asserted release.
    target_os: Optional[str] = None
    target_release: Optional[str] = None
    target_flavor: Optional[str] = None
    apt_mirror: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "unattended",
            _strict_bool(self.unattended, "installation.unattended"),
        )
        object.__setattr__(
            self, "edition", _optional_text(self.edition, "installation.edition")
        )
        object.__setattr__(
            self, "locale", _text(self.locale, "installation.locale")
        )
        object.__setattr__(
            self, "timezone", _text(self.timezone, "installation.timezone")
        )
        object.__setattr__(
            self,
            "packages",
            _tuple_of_text(self.packages, "installation.packages"),
        )
        object.__setattr__(
            self,
            "update_policy",
            _enum(
                self.update_policy,
                "installation.update_policy",
                {"frozen", "latest", "manual"},
            ),
        )
        if self.credential_ref is not None:
            ref = _text(self.credential_ref, "installation.credential_ref")
            if not ref.startswith("secret://") or len(ref) <= len("secret://"):
                raise ProvisioningError(
                    "installation.credential_ref must be an opaque secret:// reference"
                )
            object.__setattr__(self, "credential_ref", ref)
        if self.target_os is not None:
            object.__setattr__(
                self, "target_os",
                _enum(self.target_os, "installation.target_os", {"ubuntu", "windows-11"}),
            )
        if self.target_release is not None:
            object.__setattr__(
                self, "target_release", _text(self.target_release, "installation.target_release")
            )
        if self.target_flavor is not None:
            object.__setattr__(
                self, "target_flavor", _text(self.target_flavor, "installation.target_flavor").lower()
            )
        if self.target_os is None and (self.target_release or self.target_flavor):
            raise ProvisioningError("installation target release/flavor requires target_os")
        if self.apt_mirror is not None:
            mirror = _text(self.apt_mirror, "installation.apt_mirror")
            object.__setattr__(self, "apt_mirror", mirror)
            if self.target_os != "ubuntu":
                raise ProvisioningError("installation.apt_mirror requires target_os=ubuntu")

    def identity_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("credential_ref", None)
        for optional in ("target_os", "target_release", "target_flavor", "apt_mirror"):
            if payload[optional] is None:
                payload.pop(optional)
        return payload


@dataclass(frozen=True)
class EnvironmentDefinition:
    """Desired OS environment independent of a particular hypervisor output format."""

    name: str
    source: InstallationMediaSource
    machine: MachineSpec = MachineSpec()
    installation: InstallationSpec = InstallationSpec()
    guest_runtime: Optional[GuestRuntimeIdentity] = None
    schema_version: str = "argus-environment-v1"

    def __post_init__(self) -> None:
        name = _text(self.name, "name")
        if not _NAME_RE.fullmatch(name):
            raise ProvisioningError(
                "name must be 1-96 portable characters: letters, digits, dot, underscore, hyphen"
            )
        object.__setattr__(self, "name", name)
        if self.source.architecture != self.machine.architecture:
            raise ProvisioningError(
                "source.architecture must match machine.architecture"
            )
        if self.guest_runtime is not None:
            if self.guest_runtime.target_architecture != self.machine.architecture:
                raise ProvisioningError(
                    "guest_runtime.target_architecture must match machine.architecture"
                )
            if (
                self.installation.target_os is not None
                and self.guest_runtime.target_os != self.installation.target_os
            ):
                raise ProvisioningError(
                    "guest_runtime.target_os must match installation.target_os"
                )
        if self.schema_version != "argus-environment-v1":
            raise ProvisioningError(
                "schema_version must be 'argus-environment-v1'"
            )

    def identity_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "source": self.source.identity_dict(),
            "machine": asdict(self.machine),
            "installation": self.installation.identity_dict(),
        }
        if self.guest_runtime is not None:
            payload["guest_runtime"] = self.guest_runtime.identity_dict()
        return payload

    def require_guest_runtime(self) -> GuestRuntimeIdentity:
        if self.guest_runtime is None:
            raise ProvisioningError(
                "publishable ISO-backed environments require guest_runtime identity"
            )
        return self.guest_runtime

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.identity_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")

    @property
    def definition_sha256(self) -> str:
        return sha256(self.canonical_bytes()).hexdigest()

    @property
    def environment_id(self) -> str:
        return f"env-sha256-{self.definition_sha256}"


@dataclass(frozen=True)
class DerivedImageManifest:
    """Identity/provenance binding for a provider-produced immutable base image."""

    environment_id: str
    definition_sha256: str
    source_sha256: str
    provider: str
    image_format: str
    image_sha256: str
    architecture: str
    created_at: str
    evidence_run_id: Optional[str] = None
    # v1 manifests predate the required boot/agent baseline check. They must
    # not be silently reused as if the installed OS had been validated.
    manifest_version: str = "argus-derived-image-v2"

    def __post_init__(self) -> None:
        environment_id = _text(self.environment_id, "environment_id")
        definition_digest = _digest(self.definition_sha256, "definition_sha256")
        expected_environment_id = f"env-sha256-{definition_digest}"
        if environment_id != expected_environment_id:
            raise ProvisioningError(
                "environment_id must be derived from definition_sha256"
            )
        object.__setattr__(self, "environment_id", environment_id)
        object.__setattr__(self, "definition_sha256", definition_digest)
        object.__setattr__(
            self, "source_sha256", _digest(self.source_sha256, "source_sha256")
        )
        object.__setattr__(self, "provider", _text(self.provider, "provider").lower())
        object.__setattr__(
            self,
            "image_format",
            _enum(self.image_format, "image_format", _ALLOWED_IMAGE_FORMAT),
        )
        object.__setattr__(
            self, "image_sha256", _digest(self.image_sha256, "image_sha256")
        )
        object.__setattr__(
            self,
            "architecture",
            _enum(self.architecture, "architecture", _ALLOWED_ARCH),
        )
        object.__setattr__(self, "created_at", _text(self.created_at, "created_at"))
        if self.evidence_run_id is not None:
            run_id = _text(self.evidence_run_id, "evidence_run_id")
            if not re.fullmatch(r"RUN-[A-Za-z0-9][A-Za-z0-9_-]{0,95}", run_id):
                raise ProvisioningError("evidence_run_id must be a canonical ATES RunId")
            object.__setattr__(self, "evidence_run_id", run_id)
        if self.manifest_version != "argus-derived-image-v2":
            raise ProvisioningError(
                "manifest_version must be 'argus-derived-image-v2'"
            )

    def validate_against(self, definition: EnvironmentDefinition) -> None:
        if self.environment_id != definition.environment_id:
            raise ProvisioningError("derived image environment identity mismatch")
        if self.definition_sha256 != definition.definition_sha256:
            raise ProvisioningError("derived image definition digest mismatch")
        if self.source_sha256 != definition.source.sha256:
            raise ProvisioningError("derived image source digest mismatch")
        if self.architecture != definition.machine.architecture:
            raise ProvisioningError("derived image architecture mismatch")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "DerivedImageManifest":
        if not isinstance(value, Mapping):
            raise ProvisioningError("derived image manifest must be a mapping")
        if "manifest_version" not in value:
            raise ProvisioningError("derived image manifest_version is required")
        allowed = {
            "environment_id",
            "definition_sha256",
            "source_sha256",
            "provider",
            "image_format",
            "image_sha256",
            "architecture",
            "created_at",
            "evidence_run_id",
            "manifest_version",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ProvisioningError(
                "unknown derived image manifest field(s): " + ", ".join(unknown)
            )
        return cls(**dict(value))
