"""Core contracts for disposable Argus Capsule execution."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Mapping, Optional

from argus.adapters.base import AdapterError


class CapsuleError(AdapterError):
    """Raised when a Capsule cannot be created, reached, or destroyed safely."""


class CapsuleCleanupError(CapsuleError):
    """Provider resources may still depend on storage; preserve recovery state."""


def _strict_bool(value: Any, name: str) -> bool:
    """Parse a security-sensitive boolean without Python truthiness surprises."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise CapsuleError(f"{name} must be a boolean (true/false)")


@dataclass(frozen=True)
class CapsuleProviderCapabilities:
    """Security/capability contract advertised by one Capsule provider.

    Provider selection must not silently downgrade these properties. A provider
    that cannot satisfy a requested capability should fail closed during
    configuration/prepare rather than emulate it on the host.
    """

    provider: str
    host_platforms: tuple[str, ...]
    guest_os: tuple[str, ...]
    secure_transport: bool
    network_isolation: bool
    explicit_transfers: bool
    failure_retention: bool
    egress_allowlist: bool = False
    protected_bootstrap_media: bool = False

    def supports_guest_os(self, value: str) -> bool:
        wanted = str(value or "auto").strip().lower()
        return wanted == "auto" or wanted in self.guest_os


@dataclass(frozen=True)
class CapsuleSettings:
    """Host-side settings for one family of disposable Capsule sessions.

    ``image`` is a read-only/golden virtual disk. Every supported provider must
    create a writable per-session child/overlay; Argus never boots the golden
    disk writable.
    """

    # Keep PR3-PR6 behavior for configs that omit provider. Cross-platform
    # deployments can opt into ``auto`` (Hyper-V on Windows, libvirt on Linux).
    provider: str = "hyperv"
    guest_os: str = "auto"
    image: str = ""
    switch_name: str = ""
    vm_root: str = ""
    memory_mb: int = 4096
    cpu_count: int = 2
    guest_port: int = 8765
    guest_token: str = ""
    guest_input_mode: str = "physical"
    guest_address: str = ""
    boot_timeout_seconds: float = 120.0
    agent_timeout_seconds: float = 60.0
    allow_external_switch: bool = False
    retain_on_failure: bool = False
    guest_transport: str = "https"
    guest_ca_cert: str = ""
    allow_insecure_http: bool = False
    rotate_session_token: bool = True
    network_mode: str = "host_only"
    # None preserves legacy provider behavior. Provisioned images bind this
    # explicitly so the runtime cannot silently change the build contract.
    secure_boot: Optional[bool] = None
    tpm_version: str = ""
    egress_allowlist: tuple[str, ...] = ()
    allow_dhcp: bool = True
    disable_guest_file_copy: bool = True

    # Verified provisioning commitments. Empty values preserve legacy manually
    # prepared Capsule configurations; the provisioning bridge fills all three.
    environment_id: str = ""
    base_image_sha256: str = ""
    guest_runtime_identity: str = ""
    require_target_desktop: bool = False
    control_root: str = ""

    default_execution_mode: str = "isolated"
    allowed_execution_modes: tuple[str, ...] = ("isolated",)
    allow_llm_mode_change: bool = False
    failure_allow_reconnect: bool = True
    failure_allow_llm_reconnect: bool = False

    # PR7 libvirt/QEMU settings. ``qemu:///system`` is intentionally the only
    # production URI accepted by the first Linux provider because its network
    # isolation relies on libvirt's system networking/nwfilter boundary.
    libvirt_uri: str = "qemu:///system"
    libvirt_network_cidr: str = ""
    libvirt_arch: str = ""
    libvirt_machine: str = ""
    # Trusted host group containing the system QEMU service account. Only this
    # group and the host controller may read per-generation libvirt media.
    libvirt_qemu_group: str = ""

    @classmethod
    def from_mapping(cls, value: Optional[Mapping[str, Any]] = None) -> "CapsuleSettings":
        raw = dict(value or {})
        allowed = {f.name for f in fields(cls)}
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise CapsuleError(
                "unknown capsule setting(s): " + ", ".join(unknown)
            )
        for name in (
            "allow_external_switch",
            "retain_on_failure",
            "allow_insecure_http",
            "rotate_session_token",
            "allow_dhcp",
            "disable_guest_file_copy",
            "secure_boot",
            "require_target_desktop",
            "allow_llm_mode_change",
            "failure_allow_reconnect",
            "failure_allow_llm_reconnect",
        ):
            if name in raw:
                raw[name] = _strict_bool(raw[name], name)
        for tuple_field in ("egress_allowlist", "allowed_execution_modes"):
            if tuple_field not in raw:
                continue
            value = raw[tuple_field]
            if value is None:
                raw[tuple_field] = ()
            elif isinstance(value, (list, tuple)):
                raw[tuple_field] = tuple(str(item).strip() for item in value)
            else:
                raise CapsuleError(f"{tuple_field} must be a list of strings")
        return cls(**raw)

    @property
    def resolved_vm_root(self) -> Path:
        if self.vm_root:
            return Path(self.vm_root).expanduser().resolve()
        return (Path.home() / ".argus" / "capsules").resolve()

    @property
    def resolved_control_root(self) -> Path:
        if self.control_root:
            return Path(self.control_root).expanduser().resolve()
        return (Path.home() / ".argus" / "capsule-control").resolve()

    @property
    def resolved_guest_ca_cert(self) -> Optional[Path]:
        if not self.guest_ca_cert:
            return None
        return Path(self.guest_ca_cert).expanduser().resolve()


@dataclass(frozen=True)
class CapsuleRequest:
    session_id: str
    adapter_type: str
    settings: CapsuleSettings
    capsule_id: str = ""
    control_generation: int = 0
    execution_mode: str = "isolated"


@dataclass(frozen=True)
class CapsuleHandle:
    """Provider-owned resources for one live Capsule."""

    session_id: str
    provider: str
    vm_name: str
    root_dir: str
    address: str
    guest_port: int
    transport: str = "http"
    guest_os: str = "unknown"
    architecture: str = "unknown"
    capsule_id: str = ""
    control_generation: int = 0
    execution_mode: str = "isolated"
    provider_resource_identity: str = ""
    mutable_disk_identity: str = ""

    @property
    def endpoint(self) -> str:
        scheme = (self.transport or "http").lower().strip()
        if scheme not in {"http", "https"}:
            raise CapsuleError(f"unsupported Capsule guest transport: {scheme!r}")
        return f"{scheme}://{self.address}:{self.guest_port}"


@dataclass(frozen=True)
class FailureCapsule:
    """Durable reference to VM/disk evidence retained at test failure."""

    failure_id: str
    session_id: str
    provider: str
    vm_name: str
    root_dir: str
    reason: str
    retained_at: str
    vm_state: str
    capsule_id: str = ""
    failed_generation: int = 0
    execution_mode: str = "isolated"
    provider_resource_identity: str = ""
    mutable_disk_identity: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def persist(self, path: Path) -> None:
        """Atomically update the retained reference after a later failure.

        The same Capsule can fail more than once after reconnect. Its latest
        metadata must advance without overwriting another Capsule's ownership.
        """
        import json
        from argus.capsule.control import _atomic_json

        if path.exists() or path.is_symlink():
            try:
                if path.is_symlink():
                    raise ValueError()
                previous = FailureCapsule(**json.loads(path.read_text(encoding="utf-8")))
                if (not self.capsule_id or previous.capsule_id != self.capsule_id
                        or previous.provider_resource_identity != self.provider_resource_identity
                        or previous.mutable_disk_identity != self.mutable_disk_identity
                        or previous.failed_generation >= self.failed_generation):
                    raise ValueError()
            except (OSError, ValueError, TypeError):
                raise CapsuleError("retained Capsule metadata ownership or generation conflicts") from None
            path.chmod(0o600)
        _atomic_json(path, self.to_dict(), preserve_parent_permissions=True)
        path.chmod(0o444)


class CapsuleProvider(ABC):
    """Hypervisor/provider boundary used by :class:`CapsuleExecutionEnvironment`."""

    provider_name: str = "base"
    provider_capabilities = CapsuleProviderCapabilities(
        provider="base",
        host_platforms=(),
        guest_os=(),
        secure_transport=False,
        network_isolation=False,
        explicit_transfers=False,
        failure_retention=False,
        egress_allowlist=False,
    )

    def capabilities(self) -> CapsuleProviderCapabilities:
        return self.provider_capabilities

    @abstractmethod
    def create(self, request: CapsuleRequest) -> CapsuleHandle:
        """Allocate and boot a disposable Capsule.

        This method must clean up its own partial allocations before raising;
        callers cannot destroy a handle that was never returned.
        """

    def create_stopped(self, request: CapsuleRequest) -> CapsuleHandle:
        """Allocate one stable mutable Capsule without booting it."""
        raise CapsuleError(
            f"Capsule provider {self.provider_name!r} does not support stopped allocation"
        )

    def attach_bootstrap(self, handle: CapsuleHandle, media: Path) -> None:
        raise CapsuleError(
            f"Capsule provider {self.provider_name!r} does not support bootstrap media"
        )

    bootstrap_media_suffix: str = ".iso"

    def bootstrap_media_directory(
        self, handle: CapsuleHandle, settings: CapsuleSettings
    ) -> Path:
        """Prepare private storage for one-attempt provider control media."""
        from argus.capsule.permissions import ensure_private_directory

        directory = settings.resolved_control_root / "bootstrap-media"
        ensure_private_directory(directory)
        return directory

    def create_bootstrap_media(self, source: Path, output: Path) -> Path:
        """Render provider-specific, one-attempt media without booting a VM."""
        from argus.capsule.bootstrap import create_bootstrap_iso

        return create_bootstrap_iso(source, output)

    def destroy_bootstrap_media(self, media: Path) -> None:
        """Called only after confirmed VM detachment; verify host mounts too."""
        media.unlink(missing_ok=True)

    def detach_bootstrap(self, handle: CapsuleHandle, media: Path) -> None:
        raise CapsuleError(
            f"Capsule provider {self.provider_name!r} does not support bootstrap media"
        )

    def start_existing(
        self,
        handle: CapsuleHandle,
        request: CapsuleRequest,
    ) -> CapsuleHandle:
        raise CapsuleError(
            f"Capsule provider {self.provider_name!r} does not support existing-Capsule start"
        )

    def stop_existing(self, handle: CapsuleHandle) -> None:
        raise CapsuleError(
            f"Capsule provider {self.provider_name!r} does not support existing-Capsule stop"
        )

    def quarantine(self, handle: CapsuleHandle) -> None:
        self.stop_existing(handle)

    def inspect_ownership(self, handle: CapsuleHandle) -> tuple[str, str]:
        raise CapsuleError(
            f"Capsule provider {self.provider_name!r} cannot attest retained ownership"
        )

    def retain_failure(self, handle: CapsuleHandle, reason: str) -> FailureCapsule:
        """Freeze a live Capsule for later forensic inspection instead of destroying it."""
        raise CapsuleError(
            f"Capsule provider {self.provider_name!r} does not support failure retention"
        )

    @abstractmethod
    def destroy(self, handle: CapsuleHandle) -> None:
        """Destroy the VM and all provider-owned per-session storage."""
