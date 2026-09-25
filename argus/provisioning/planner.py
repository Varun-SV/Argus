"""Provider-neutral planning for ISO-backed Argus OS environments."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from argus.provisioning.model import (
    DerivedImageManifest,
    EnvironmentDefinition,
    ProvisioningError,
)


@dataclass(frozen=True)
class ProvisioningProviderCapabilities:
    provider: str
    host_platforms: tuple[str, ...]
    architectures: tuple[str, ...]
    media_types: tuple[str, ...]
    image_formats: tuple[str, ...]
    firmware_modes: tuple[str, ...]
    disk_buses: tuple[str, ...]
    network_modes: tuple[str, ...]
    secure_boot: bool = False
    tpm_versions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.provider.strip():
            raise ProvisioningError("provider capability name must not be empty")


@dataclass(frozen=True)
class ProvisioningPlan:
    environment_id: str
    definition_sha256: str
    provider: str
    output_format: str
    cache_dir: Path
    image_path: Path
    manifest_path: Path


@dataclass(frozen=True)
class ProvisioningResult:
    plan: ProvisioningPlan
    manifest: DerivedImageManifest


def _contains(values: Iterable[str], wanted: str) -> bool:
    return wanted in {str(value).strip().lower() for value in values}


def validate_provider_capabilities(
    definition: EnvironmentDefinition,
    capabilities: ProvisioningProviderCapabilities,
    *,
    output_format: str,
) -> None:
    machine = definition.machine
    source = definition.source
    output_format = output_format.strip().lower()

    if not _contains(capabilities.architectures, machine.architecture):
        raise ProvisioningError(
            f"provider {capabilities.provider!r} does not support architecture "
            f"{machine.architecture!r}"
        )
    if not _contains(capabilities.media_types, source.media_type):
        raise ProvisioningError(
            f"provider {capabilities.provider!r} does not support media type "
            f"{source.media_type!r}"
        )
    if not _contains(capabilities.image_formats, output_format):
        raise ProvisioningError(
            f"provider {capabilities.provider!r} cannot produce {output_format!r}"
        )
    if not _contains(capabilities.firmware_modes, machine.firmware):
        raise ProvisioningError(
            f"provider {capabilities.provider!r} does not support firmware "
            f"{machine.firmware!r}"
        )
    if not _contains(capabilities.disk_buses, machine.disk_bus):
        raise ProvisioningError(
            f"provider {capabilities.provider!r} does not support disk bus "
            f"{machine.disk_bus!r}"
        )
    if not _contains(capabilities.network_modes, machine.network_mode):
        raise ProvisioningError(
            f"provider {capabilities.provider!r} does not support network mode "
            f"{machine.network_mode!r}"
        )
    if machine.secure_boot and not capabilities.secure_boot:
        raise ProvisioningError(
            f"provider {capabilities.provider!r} does not support Secure Boot"
        )
    if machine.tpm_version and not _contains(
        capabilities.tpm_versions, machine.tpm_version
    ):
        raise ProvisioningError(
            f"provider {capabilities.provider!r} does not support TPM "
            f"{machine.tpm_version!r}"
        )


def build_provisioning_plan(
    definition: EnvironmentDefinition,
    capabilities: ProvisioningProviderCapabilities,
    *,
    output_format: str,
    cache_root: str | Path,
) -> ProvisioningPlan:
    """Compile an immutable definition into a deterministic provider cache location."""

    normalized_format = output_format.strip().lower()
    validate_provider_capabilities(
        definition,
        capabilities,
        output_format=normalized_format,
    )

    provider = capabilities.provider.strip().lower()
    if not provider or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for ch in provider):
        raise ProvisioningError(
            "provider name must contain only lowercase letters, digits, hyphen, underscore"
        )

    root = Path(cache_root).expanduser().resolve()
    cache_dir = root / definition.environment_id / provider
    image_path = cache_dir / f"base.{normalized_format}"
    manifest_path = cache_dir / "manifest.json"
    return ProvisioningPlan(
        environment_id=definition.environment_id,
        definition_sha256=definition.definition_sha256,
        provider=provider,
        output_format=normalized_format,
        cache_dir=cache_dir,
        image_path=image_path,
        manifest_path=manifest_path,
    )


class EnvironmentProvisioner(ABC):
    """Hypervisor-specific OS installer boundary.

    Implementations own installation-time VM creation only. Successful output
    is an immutable derived base image that returns to the existing Capsule
    execution path; provisioning must not create a second runtime abstraction.
    """

    @abstractmethod
    def capabilities(self) -> ProvisioningProviderCapabilities:
        """Return the fail-closed provisioning capabilities."""

    @abstractmethod
    def provision(
        self,
        definition: EnvironmentDefinition,
        plan: ProvisioningPlan,
    ) -> ProvisioningResult:
        """Create and attest one immutable derived image."""
