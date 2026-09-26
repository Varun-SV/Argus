from __future__ import annotations

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import os
from pathlib import Path
import platform
import threading

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
    derived_image_advertisement,
    environment_definition_from_mapping,
    verify_installation_media,
)
from argus.provisioning.build import publish_derived_image
from argus.provisioning.providers import HyperVProvisioner, LibvirtProvisioner


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


def _runtime_definition(tmp_path: Path, provider: str = "hyperv") -> EnvironmentDefinition:
    original = _definition(tmp_path)
    machine = replace(
        original.machine,
        firmware="uefi" if provider == "hyperv" else "bios",
        secure_boot=False,
        tpm_version=None,
        disk_bus="scsi" if provider == "hyperv" else "virtio",
        network_mode="host_only",
    )
    return replace(
        original,
        machine=machine,
        installation=InstallationSpec(unattended=False, update_policy="manual"),
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


@pytest.mark.skipif(
    os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"),
    reason="requires dir_fd + O_NOFOLLOW secure traversal",
)
def test_iso_verification_rejects_intermediate_symlink_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted = tmp_path / "trusted"
    slot = trusted / "slot"
    outside = tmp_path / "outside"
    slot.mkdir(parents=True)
    outside.mkdir()

    iso = slot / "windows.iso"
    iso.write_bytes(b"installation-media")
    (outside / "windows.iso").write_bytes(b"attacker-media")
    definition = _definition(
        tmp_path,
        source=InstallationMediaSource(
            path=str(iso),
            sha256=_digest(b"installation-media"),
        ),
    )

    resolved_iso = iso.resolve()
    original_relative_to = Path.relative_to
    swapped = False

    def swapping_relative_to(self: Path, *other: object) -> Path:
        nonlocal swapped
        result = original_relative_to(self, *other)
        if not swapped and self == resolved_iso:
            slot.rename(trusted / "slot-original")
            slot.symlink_to(outside, target_is_directory=True)
            swapped = True
        return result

    monkeypatch.setattr(Path, "relative_to", swapping_relative_to)

    with pytest.raises(ProvisioningError, match="cannot securely open"):
        verify_installation_media(definition, allowed_roots=(trusted,))
    assert swapped


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


def test_plan_separates_manifest_by_output_format(tmp_path: Path) -> None:
    definition = _definition(tmp_path)
    capabilities = replace(
        _capabilities(),
        provider="libvirt",
        image_formats=("qcow2", "raw"),
    )

    qcow2 = build_provisioning_plan(
        definition,
        capabilities,
        output_format="qcow2",
        cache_root=tmp_path / "cache",
    )
    raw = build_provisioning_plan(
        definition,
        capabilities,
        output_format="raw",
        cache_root=tmp_path / "cache",
    )

    assert qcow2.cache_dir != raw.cache_dir
    assert qcow2.manifest_path != raw.manifest_path
    assert qcow2.manifest_path.name == raw.manifest_path.name == "manifest.json"
    assert qcow2.image_path.name == "base.qcow2"
    assert raw.image_path.name == "base.raw"


def test_derived_image_bridge_returns_normal_capsule_settings(tmp_path: Path) -> None:
    definition = _runtime_definition(tmp_path)
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

    base = CapsuleSettings(
        provider="hyperv",
        memory_mb=8192,
        cpu_count=4,
        network_mode="host_only",
        guest_port=9443,
    )
    settings = capsule_settings_from_derived_image(
        definition,
        manifest,
        image,
        settings=base,
    )
    assert settings.provider == "hyperv"
    assert settings.memory_mb == 8192
    assert settings.cpu_count == 4
    assert settings.network_mode == "host_only"
    assert settings.secure_boot is False
    assert settings.guest_port == 9443
    assert settings.image == str(image.resolve())


def test_derived_manifest_rejects_wrong_definition(tmp_path: Path) -> None:
    definition = _runtime_definition(tmp_path)
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

def test_derived_image_bridge_derives_provider_from_manifest(tmp_path: Path) -> None:
    definition = _runtime_definition(tmp_path, "libvirt")
    image = tmp_path / "base.qcow2"
    image.write_bytes(b"derived-image")
    manifest = DerivedImageManifest(
        environment_id=definition.environment_id,
        definition_sha256=definition.definition_sha256,
        source_sha256=definition.source.sha256,
        provider="libvirt",
        image_format="qcow2",
        image_sha256=_digest(b"derived-image"),
        architecture="x86_64",
        created_at="2026-09-25T00:00:00Z",
    )

    settings = capsule_settings_from_derived_image(definition, manifest, image)

    assert settings.provider == "libvirt"
    assert settings.cpu_count == 4
    assert settings.memory_mb == 8192
    assert settings.network_mode == "host_only"
    assert settings.libvirt_arch == "x86_64"
    assert settings.image == str(image.resolve())


def test_derived_image_bridge_rejects_explicit_machine_contract_mismatch(
    tmp_path: Path,
) -> None:
    definition = _runtime_definition(tmp_path)
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

    with pytest.raises(ProvisioningError, match="memory_mb requires 8192"):
        capsule_settings_from_derived_image(
            definition,
            manifest,
            image,
            settings=CapsuleSettings(
                provider="hyperv",
                cpu_count=4,
                memory_mb=12288,
                network_mode="host_only",
            ),
        )


def test_derived_image_bridge_rejects_explicit_provider_mismatch(tmp_path: Path) -> None:
    definition = _runtime_definition(tmp_path, "libvirt")
    image = tmp_path / "base.qcow2"
    image.write_bytes(b"derived-image")
    manifest = DerivedImageManifest(
        environment_id=definition.environment_id,
        definition_sha256=definition.definition_sha256,
        source_sha256=definition.source.sha256,
        provider="libvirt",
        image_format="qcow2",
        image_sha256=_digest(b"derived-image"),
        architecture="x86_64",
        created_at="2026-09-25T00:00:00Z",
    )

    with pytest.raises(ProvisioningError, match="provider mismatch"):
        capsule_settings_from_derived_image(
            definition,
            manifest,
            image,
            settings=CapsuleSettings(provider="hyperv"),
        )


def test_bridge_rejects_security_and_hardware_downgrade(tmp_path: Path) -> None:
    definition = _runtime_definition(tmp_path)
    image = tmp_path / "base.vhdx"
    image.write_bytes(b"image")
    manifest = DerivedImageManifest(
        definition.environment_id, definition.definition_sha256, definition.source.sha256,
        "hyperv", "vhdx", _digest(b"image"), "x86_64", "2026-09-26T00:00:00Z",
    )
    for machine in (
        replace(definition.machine, secure_boot=True),
        replace(definition.machine, tpm_version="2.0"),
        replace(definition.machine, disk_bus="nvme"),
        replace(definition.machine, network_mode="isolated"),
    ):
        changed = replace(definition, machine=machine)
        changed_manifest = replace(
            manifest, environment_id=changed.environment_id,
            definition_sha256=changed.definition_sha256,
        )
        with pytest.raises(ProvisioningError, match="cannot be preserved"):
            capsule_settings_from_derived_image(changed, changed_manifest, image)


def test_build_publishes_once_and_rejects_corrupt_cache(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    definition = _runtime_definition(tmp_path, "libvirt")
    provider = LibvirtProvisioner(network_name="argus-local")
    plan = build_provisioning_plan(
        definition, provider.capabilities(), output_format="raw", cache_root=tmp_path / "cache"
    )
    calls = []

    def install(iso: Path, image: Path) -> None:
        assert iso.read_bytes() == b"installation-media"
        calls.append(iso)
        image.write_bytes(b"installed-os")

    first = publish_derived_image(definition, plan, install)
    second = publish_derived_image(definition, plan, install)
    assert first.manifest == second.manifest
    assert len(calls) == 1
    assert plan.image_path.read_bytes() == b"installed-os"
    assert not (plan.cache_dir / "installation.iso").exists()
    assert "secret://" not in plan.manifest_path.read_text()
    plan.image_path.chmod(0o644)
    plan.image_path.write_bytes(b"tampered")
    with pytest.raises(ProvisioningError, match="SHA-256 mismatch"):
        publish_derived_image(definition, plan, install)
    assert len(calls) == 1


def test_failed_build_removes_private_resources_and_can_retry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    definition = _runtime_definition(tmp_path, "libvirt")
    provider = LibvirtProvisioner(network_name="argus-local")
    plan = build_provisioning_plan(
        definition, provider.capabilities(), output_format="qcow2", cache_root=tmp_path / "cache"
    )

    def fail(iso: Path, image: Path) -> None:
        image.write_bytes(b"partial")
        raise ProvisioningError("installer failed")

    with pytest.raises(ProvisioningError, match="installer failed"):
        publish_derived_image(definition, plan, fail)
    assert not plan.cache_dir.exists()
    assert not list(plan.cache_dir.parent.glob(".building-*"))
    assert publish_derived_image(
        definition, plan, lambda iso, image: image.write_bytes(b"retry image")
    ).manifest.image_sha256 == _digest(b"retry image")


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock concurrency path")
def test_concurrent_same_key_builds_publish_one_image(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    definition = _runtime_definition(tmp_path, "libvirt")
    provider = LibvirtProvisioner(network_name="argus-local")
    plan = build_provisioning_plan(
        definition, provider.capabilities(), output_format="raw", cache_root=tmp_path / "cache"
    )
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def install(iso: Path, image: Path) -> None:
        calls.append(image)
        entered.set()
        assert release.wait(5)
        image.write_bytes(b"one published image")

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(publish_derived_image, definition, plan, install)
        assert entered.wait(5)
        second = executor.submit(publish_derived_image, definition, plan, install)
        release.set()
        assert first.result(timeout=10).manifest == second.result(timeout=10).manifest
    assert len(calls) == 1


def test_media_swap_before_staging_does_not_publish(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    definition = _runtime_definition(tmp_path, "libvirt")
    provider = LibvirtProvisioner(network_name="argus-local")
    plan = build_provisioning_plan(
        definition, provider.capabilities(), output_format="raw", cache_root=tmp_path / "cache"
    )
    import argus.provisioning.build as build_module

    original = build_module.verify_installation_media

    def swap_after_check(value):
        checked = original(value)
        Path(value.source.path).write_bytes(b"swapped after first verification")
        return checked

    monkeypatch.setattr(build_module, "verify_installation_media", swap_after_check)
    with pytest.raises(ProvisioningError, match="SHA-256 mismatch"):
        publish_derived_image(
            definition, plan, lambda iso, image: image.write_bytes(b"should not run")
        )
    assert not plan.cache_dir.exists()


def test_libvirt_provider_builds_and_cleans_vm(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    definition = _runtime_definition(tmp_path, "libvirt")
    commands = []

    def run(argv, timeout):
        commands.append(tuple(argv))
        if argv[0] == "qemu-img" and argv[1] == "create":
            Path(argv[-2]).write_bytes(b"bootable image fixture")
        if argv[0] == "qemu-img" and argv[1] == "info":
            return '{"format":"qcow2","virtual-size":85899345920}'
        if "net-dumpxml" in argv:
            return "<network><name>argus-local</name><bridge name='virbr9'/></network>"
        if "net-info" in argv:
            return "Name: argus-local\nActive: yes\n"
        if "domstate" in argv:
            return "shut off"
        return ""

    provider = LibvirtProvisioner(network_name="argus-local", runner=run)
    plan = build_provisioning_plan(
        definition, provider.capabilities(), output_format="qcow2", cache_root=tmp_path / "cache"
    )
    result = provider.provision(definition, plan)
    assert result.manifest.image_sha256 == _digest(b"bootable image fixture")
    assert any("define" in command for command in commands)
    assert any("undefine" in command for command in commands)
    assert not (plan.cache_dir / "domain.xml").exists()


def test_libvirt_failure_after_start_cleans_vm_and_does_not_publish(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    definition = _runtime_definition(tmp_path, "libvirt")
    commands = []

    def run(argv, timeout):
        commands.append(tuple(argv))
        if argv[:2] == ("qemu-img", "create"):
            Path(argv[-2]).write_bytes(b"partial disk")
        if "net-dumpxml" in argv:
            return "<network><name>argus-local</name><bridge name='virbr9'/></network>"
        if "net-info" in argv:
            return "Active: yes"
        if "domstate" in argv:
            return "running"
        return ""

    def interrupt(name):
        raise KeyboardInterrupt()

    provider = LibvirtProvisioner(
        network_name="argus-local", runner=run, on_started=interrupt
    )
    plan = build_provisioning_plan(
        definition, provider.capabilities(), output_format="qcow2", cache_root=tmp_path / "cache"
    )
    with pytest.raises(KeyboardInterrupt):
        provider.provision(definition, plan)
    assert any("destroy" in argv for argv in commands)
    assert any("undefine" in argv for argv in commands)
    assert not plan.cache_dir.exists()
    assert not list(plan.cache_dir.parent.glob(".building-*"))


def test_hyperv_provider_builds_and_cleans_vm(tmp_path: Path, monkeypatch) -> None:
    import re

    definition = _runtime_definition(tmp_path)
    commands = []

    def run(script, timeout):
        commands.append(script)
        if script.startswith("New-VHD"):
            path = re.search(r"-Path '([^']+)'", script).group(1)
            Path(path).write_bytes(b"installed Windows fixture")
        if "Get-VMSwitch" in script:
            return "Internal"
        if "Get-VHD" in script:
            return "Dynamic:85899345920"
        if ".State.ToString()" in script:
            return "Off"
        return ""

    monkeypatch.setattr(platform, "system", lambda: "Windows")
    provider = HyperVProvisioner(switch_name="Argus-Internal", runner=run)
    plan = build_provisioning_plan(
        definition, provider.capabilities(), output_format="vhdx", cache_root=tmp_path / "cache"
    )
    result = provider.provision(definition, plan)
    assert result.manifest.image_sha256 == _digest(b"installed Windows fixture")
    assert any("Set-VMFirmware" in command for command in commands)
    assert any("Remove-VM" in command for command in commands)


def test_fleet_advertisement_uses_verified_image_digest_not_alias(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    definition = _runtime_definition(tmp_path, "libvirt")
    provider = LibvirtProvisioner(network_name="argus-local")
    plan = build_provisioning_plan(
        definition, provider.capabilities(), output_format="raw", cache_root=tmp_path / "cache"
    )
    result = publish_derived_image(
        definition, plan, lambda iso, image: image.write_bytes(b"guest os")
    )
    first = derived_image_advertisement(
        definition, result.manifest, plan.image_path, alias="latest", guest_os="linux"
    )
    renamed = derived_image_advertisement(
        definition, result.manifest, plan.image_path, alias="stable", guest_os="linux"
    )
    assert first.digest == renamed.digest == f"sha256:{_digest(b'guest os')}"
    assert first.image_id == renamed.image_id
    assert first.alias != renamed.alias
    plan.image_path.chmod(0o644)
    plan.image_path.write_bytes(b"mutated")
    with pytest.raises(ProvisioningError, match="SHA-256 mismatch"):
        derived_image_advertisement(
            definition, result.manifest, plan.image_path, alias="latest", guest_os="linux"
        )
