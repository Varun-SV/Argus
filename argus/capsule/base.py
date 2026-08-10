"""Core contracts for disposable Argus Capsule execution."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Mapping, Optional

from argus.adapters.base import AdapterError


class CapsuleError(AdapterError):
    """Raised when a Capsule cannot be created, reached, or destroyed safely."""


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
class CapsuleSettings:
    """Host-side settings for one family of disposable Capsule sessions.

    ``image`` is a read-only/golden virtual disk. Every session receives a
    differencing child disk; Argus never boots the golden disk writable.
    """

    provider: str = "hyperv"
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
    egress_allowlist: tuple[str, ...] = ()
    allow_dhcp: bool = True
    disable_guest_file_copy: bool = True

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
        ):
            if name in raw:
                raw[name] = _strict_bool(raw[name], name)
        if "egress_allowlist" in raw:
            value = raw["egress_allowlist"]
            if value is None:
                raw["egress_allowlist"] = ()
            elif isinstance(value, (list, tuple)):
                raw["egress_allowlist"] = tuple(str(item).strip() for item in value)
            else:
                raise CapsuleError("egress_allowlist must be a list of CIDR strings")
        return cls(**raw)

    @property
    def resolved_vm_root(self) -> Path:
        if self.vm_root:
            return Path(self.vm_root).expanduser().resolve()
        return (Path.home() / ".argus" / "capsules").resolve()

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

    def to_dict(self) -> dict:
        return asdict(self)


class CapsuleProvider(ABC):
    """Hypervisor/provider boundary used by :class:`CapsuleExecutionEnvironment`."""

    provider_name: str = "base"

    @abstractmethod
    def create(self, request: CapsuleRequest) -> CapsuleHandle:
        """Allocate and boot a disposable Capsule.

        This method must clean up its own partial allocations before raising;
        callers cannot destroy a handle that was never returned.
        """

    def retain_failure(self, handle: CapsuleHandle, reason: str) -> FailureCapsule:
        """Freeze a live Capsule for later forensic inspection instead of destroying it."""
        raise CapsuleError(
            f"Capsule provider {self.provider_name!r} does not support failure retention"
        )

    @abstractmethod
    def destroy(self, handle: CapsuleHandle) -> None:
        """Destroy the VM and all provider-owned per-session storage."""
