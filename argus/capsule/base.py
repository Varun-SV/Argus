"""Core contracts for disposable Argus Capsule execution."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Mapping, Optional

from argus.adapters.base import AdapterError


class CapsuleError(AdapterError):
    """Raised when a Capsule cannot be created, reached, or destroyed safely."""


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

    @classmethod
    def from_mapping(cls, value: Optional[Mapping[str, Any]] = None) -> "CapsuleSettings":
        raw = dict(value or {})
        allowed = {f.name for f in fields(cls)}
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise CapsuleError(
                "unknown capsule setting(s): " + ", ".join(unknown)
            )
        return cls(**raw)

    @property
    def resolved_vm_root(self) -> Path:
        if self.vm_root:
            return Path(self.vm_root).expanduser().resolve()
        return (Path.home() / ".argus" / "capsules").resolve()


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

    @property
    def endpoint(self) -> str:
        return f"http://{self.address}:{self.guest_port}"


class CapsuleProvider(ABC):
    """Hypervisor/provider boundary used by :class:`CapsuleExecutionEnvironment`."""

    provider_name: str = "base"

    @abstractmethod
    def create(self, request: CapsuleRequest) -> CapsuleHandle:
        """Allocate and boot a disposable Capsule.

        This method must clean up its own partial allocations before raising;
        callers cannot destroy a handle that was never returned.
        """

    @abstractmethod
    def destroy(self, handle: CapsuleHandle) -> None:
        """Destroy the VM and all provider-owned per-session storage."""
