from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
import platform

import pytest

from argus.capsule.base import CapsuleSettings
from argus.provisioning import (
    DerivedImageManifest,
    EnvironmentDefinition,
    InstallationMediaSource,
    InstallationSpec,
    MachineSpec,
    ProvisioningError,
    ProvisioningProviderCapabilities,
    build_provisioning_plan,
    capsule_settings_from_derived_image,
    environment_definition_from_mapping,
    verify_installation_media,
)


def _digest(data: bytes) -> str:
    return sha256(data).hexdigest()


def _definition(tmp_path: Path, **overrides) -> EnvironmentDefinition:
    iso = tmp_path / "windows.iso"
    iso.write_bytes(b"installation-media")
    source = InstallationMediaSource(
        path=str(iso),
        sha256=_digest(b"installation-media"),
        architecture="x86_64",
    )
    values = {
        "name": "win11-lab",
        "source": source,
        "machine": MachineSpec(
            architecture="x86_64",
            cpu_count=4,
            memory_mb=8192,
            firmware="uefi",
            secure_boot=True,
            tpm_version="2.0",
            disk_size_gib=80,
            disk_bus="nvme",
            network_mode="isolated",
        ),
        "installation": InstallationSpec(
            edition="professional",
            locale="en-US",
            timezone="UTC",
            update_policy="frozen",
            credential_ref="secret://argus/windows-lab",
        ),
    }
    values.update(overrides)
    return EnvironmentDefinition(**values)


def _capabilities() -> ProvisioningProviderCapabilities:
    return ProvisioningProviderCapabilities(
        provider="hyperv",
        host_platforms=(platform.system().lower(),),
        architectures=("x86_64",),
        media_types=("iso",),
        image_formats=("vhdx",),
        firmware_modes=("uefi",),
        disk_buses=("nvme", "scsi"),
        network_modes=("isolated", "host_only"),
        secure_boot=True,
        tpm_versions=("2.0",),
    )


def test_environment_identity_uses_content_not_source_path_or_secret_ref(tmp_path: Path) -> None:
    first = _definition(tmp_path)
    moved = tmp_path / "moved.iso"
    moved.write_bytes(b"installation-media")
    second = replace(
        first,
        source=replace(first.source, path=str(moved)),
        installation=replace(
            first.installation,
            credential_ref="secret://rotated/credential",
        ),
    )

    assert first.definition_sha256 == second.definition_sha256
    assert first.environment_id == second.environment_id


def test_machine_change_changes_environment_identity(tmp_path: Path) -> None:
    first = _definition(tmp_path)
    second = replace(first, machine=replace(first.machine, memory_mb=16384))

    assert first.environment_id != second.environment_id


def test_secure_boot_requires_uefi() -> None:
    with pytest.raises(ProvisioningError, match="requires UEFI"):
        MachineSpec(firmware="bios", secure_boot=True)


def test_source_and_machine_architecture_must_match(tmp_path: Path) -> None:
    iso = tmp_path / "linux.iso"
    iso.write_bytes(b"linux")
    source = InstallationMediaSource(
        path=str(iso),
        sha256=_digest(b"linux"),
        architecture="aarch64",
    )
    with pytest.raises(ProvisioningError, match="must match"):
        EnvironmentDefinition(
            name="bad-arch",
            source=source,
            machine=MachineSpec(architecture="x86_64"),
        )


def test_mapping_loader_is_strict_and_converts_packages() -> None:
    mapping = {
        "name": "ubuntu-24",
        "source": {
            "kind": "installation_media",
            "path": "/media/ubuntu.iso",
            "sha256": "a" * 64,
            "architecture": "x86_64",
        },
        "machine": {
            "architecture": "x86_64",
            "network_mode": "isolated",
        },
        "installation": {
            "packages": ["python3", "git"],
            "credential_ref": "secret://lab/account",
        },
    }
    definition = environment_definition_from_mapping(mapping)
    assert definition.installation.packages == ("python3", "git")

    mapping["machine"]["mystery_switch"] = True
    with pytest.raises(ProvisioningError, match="unknown machine field"):
        environment_definition_from_mapping(mapping)


def test_iso_verification_binds_actual_bytes(tmp_path: Path) -> None:
    definition = _definition(tmp_path)
    verified = verify_installation_media(definition, allowed_roots=(tmp_path,))
    assert verified.sha256 == definition.source.sha256
    assert verified.size == len(b"installation-media")

    Path(definition.source.path).write_bytes(b"changed")
    with pytest.raises(ProvisioningError, match="SHA-256 mismatch"):
        verify_installation_media(definition, allowed_roots=(tmp_path,))


def test_iso_verification_requires_iso_locator(tmp_path: Path) -> None:
    blob = tmp_path / "windows.img"
    blob.write_bytes(b"installation-media")
    definition = _definition(
        tmp_path,
        source=InstallationMediaSource(
            path=str(blob),
            sha256=_digest(b"installation-media"),
        ),
    )
    with pytest.raises(ProvisioningError, match=r"\.iso"):
        verify_installation_media(definition)


def test_plan_rejects_provider_for_different_host_platform(tmp_path: Path) -> None:
    definition = _definition(tmp_path)
    unsupported = replace(_capabilities(), host_platforms=("unsupported-host",))

    with pytest.raises(ProvisioningError, match="host platform"):
        build_provisioning_plan(
            definition,
            unsupported,
            output_format="vhdx",
            cache_root=tmp_path / "cache",
        )


def test_plan_is_content_addressed_and_provider_gated(tmp_path: Path) -> None:
    definition = _definition(tmp_path)
    plan = build_provisioning_plan(
        definition,
        _capabilities(),
        output_format="vhdx",
        cache_root=tmp_path / "cache",
    )
    assert plan.environment_id == definition.environment_id
    assert plan.image_path.name == "base.vhdx"
    assert definition.environment_id in str(plan.image_path)

    unsupported = replace(_capabilities(), secure_boot=False)
    with pytest.raises(ProvisioningError, match="Secure Boot"):
        build_provisioning_plan(
            definition,
            unsupported,
            output_format="vhdx",
            cache_root=tmp_path / "cache",
        )


def test_derived_image_bridge_returns_normal_capsule_settings(tmp_path: Path) -> None:
    definition = _definition(tmp_path)
    image = tmp_path / "base.vhdx"
    image.write_bytes(b"derived-image")
    manifest = DerivedImageManifest(
        environment_id=definition.environment_id,
        definition_sha256=definition.definition_sha256,
        source_sha256=definition.source.sha256,
        provider="hyperv",
        image_format="vhdx",
        image_sha256=_digest(b"derived-image"),
        architecture="x86_64",
        created_at="2026-09-25T00:00:00Z",
    )

    base = CapsuleSettings(provider="hyperv", memory_mb=12288)
    settings = capsule_settings_from_derived_image(
        definition,
        manifest,
        image,
        settings=base,
    )
    assert settings.provider == "hyperv"
    assert settings.memory_mb == 12288
    assert settings.image == str(image.resolve())


def test_derived_manifest_rejects_wrong_definition(tmp_path: Path) -> None:
    definition = _definition(tmp_path)
    different = replace(
        definition,
        machine=replace(definition.machine, cpu_count=8),
    )
    image = tmp_path / "base.vhdx"
    image.write_bytes(b"derived-image")
    manifest = DerivedImageManifest(
        environment_id=definition.environment_id,
        definition_sha256=definition.definition_sha256,
        source_sha256=definition.source.sha256,
        provider="hyperv",
        image_format="vhdx",
        image_sha256=_digest(b"derived-image"),
        architecture="x86_64",
        created_at="2026-09-25T00:00:00Z",
    )

    with pytest.raises(ProvisioningError, match="environment identity mismatch"):
        capsule_settings_from_derived_image(different, manifest, image)
