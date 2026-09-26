"""Bridge verified provisioned images back into the existing Capsule runtime."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from argus.capsule.base import CapsuleSettings
from argus.provisioning.integrity import verify_regular_file
from argus.provisioning.model import (
    DerivedImageManifest,
    EnvironmentDefinition,
    ProvisioningError,
)


def _bind_machine_contract(
    definition: EnvironmentDefinition,
    manifest: DerivedImageManifest,
    settings: CapsuleSettings | None,
) -> CapsuleSettings:
    """Bind runtime-equivalent Capsule settings to the immutable machine contract."""

    machine = definition.machine
    # Capsule providers currently express only these hardware contracts. In
    # particular, a verified image must not lose its Secure Boot/TPM or disk
    # bus requirements when a disposable session is created.
    supported = {
        "hyperv": ("vhdx", "x86_64", "uefi", "scsi", "host_only"),
        "libvirt": ("qcow2", "x86_64", "bios", "virtio", "host_only"),
    }
    contract = supported.get(manifest.provider)
    if contract is None:
        raise ProvisioningError("derived image provider has no supported Capsule contract")
    image_format, arch, firmware, disk_bus, network = contract
    if manifest.provider == "libvirt" and manifest.image_format == "raw":
        image_format = "raw"
    if (
        manifest.image_format != image_format
        or machine.architecture != arch
        or machine.firmware != firmware
        or machine.disk_bus != disk_bus
        or machine.network_mode != network
        or machine.secure_boot
        or machine.tpm_version is not None
    ):
        raise ProvisioningError(
            "derived machine contract cannot be preserved by the current Capsule provider"
        )
    architecture = machine.architecture if manifest.provider == "libvirt" else ""

    if settings is None:
        return CapsuleSettings(
            provider=manifest.provider,
            cpu_count=machine.cpu_count,
            memory_mb=machine.memory_mb,
            network_mode=machine.network_mode,
            secure_boot=False,
            libvirt_arch=architecture,
        )

    requested_provider = settings.provider.strip().lower()
    if requested_provider != manifest.provider:
        raise ProvisioningError(
            "derived image provider mismatch: "
            f"manifest requires {manifest.provider!r}, "
            f"Capsule settings request {settings.provider!r}"
        )

    mismatches: list[str] = []
    if settings.cpu_count != machine.cpu_count:
        mismatches.append(
            f"cpu_count requires {machine.cpu_count}, got {settings.cpu_count}"
        )
    if settings.memory_mb != machine.memory_mb:
        mismatches.append(
            f"memory_mb requires {machine.memory_mb}, got {settings.memory_mb}"
        )
    if settings.network_mode.strip().lower() != machine.network_mode:
        mismatches.append(
            f"network_mode requires {machine.network_mode!r}, got {settings.network_mode!r}"
        )
    if settings.secure_boot is True:
        mismatches.append("secure_boot requires false")
    if manifest.provider == "libvirt":
        requested_arch = settings.libvirt_arch.strip().lower()
        if requested_arch and requested_arch != machine.architecture:
            mismatches.append(
                f"libvirt_arch requires {machine.architecture!r}, got {settings.libvirt_arch!r}"
            )

    if mismatches:
        raise ProvisioningError(
            "Capsule settings contradict derived environment machine contract: "
            + "; ".join(mismatches)
        )

    return replace(
        settings,
        provider=manifest.provider,
        cpu_count=machine.cpu_count,
        memory_mb=machine.memory_mb,
        network_mode=machine.network_mode,
        secure_boot=False,
        libvirt_arch=architecture if manifest.provider == "libvirt" else settings.libvirt_arch,
    )


def capsule_settings_from_derived_image(
    definition: EnvironmentDefinition,
    manifest: DerivedImageManifest,
    image_path: str | Path,
    *,
    settings: CapsuleSettings | None = None,
) -> CapsuleSettings:
    """Verify a derived base image and bind it to normal Capsule settings.

    This function is intentionally a bridge, not a new execution environment.
    After verification, the existing ExecutionEnvironment -> Capsule -> Adapter
    runtime remains authoritative.
    """

    manifest.validate_against(definition)
    candidate = Path(image_path)
    expected_suffix = {
        "vhdx": ".vhdx",
        "qcow2": ".qcow2",
        "raw": ".raw",
    }[manifest.image_format]
    if candidate.suffix.lower() != expected_suffix:
        raise ProvisioningError(
            f"derived {manifest.image_format} image must use {expected_suffix} suffix"
        )

    verified = verify_regular_file(
        candidate,
        expected_sha256=manifest.image_sha256,
    )
    base = _bind_machine_contract(definition, manifest, settings)
    return replace(base, image=str(verified.path))
