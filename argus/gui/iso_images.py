"""Build desktop-managed images using the existing verified provisioning path."""

from __future__ import annotations

import os
import platform
import re
import shutil
from pathlib import Path

from argus.capsule.base import CapsuleSettings
from argus.capsule.hyperv_isolated import IsolatedHyperVProvider
from argus.capsule.libvirt import LibvirtProvider
from argus.gui.project_settings import endpoint_url
from argus.provisioning import (
    HyperVProvisioner, LibvirtProvisioner, ProvisioningError, build_provisioning_plan,
    environment_definition_from_mapping,
)


def host_blocker(profile: str) -> str:
    system, machine = platform.system(), platform.machine().lower()
    if profile == "windows-11":
        if not IsolatedHyperVProvider.provider_capabilities.protected_bootstrap_media:
            return "Windows 11 ISO builds are blocked pending native validation of protected Hyper-V bootstrap."
        if system != "Windows" or machine not in {"amd64", "x86_64"}:
            return "This profile requires an x86-64 Windows host with Hyper-V."
    elif profile == "ubuntu":
        if system != "Linux" or machine not in {"amd64", "x86_64"}:
            return "Ubuntu ISO builds require an x86-64 Linux host with KVM."
        if not LibvirtProvider.provider_capabilities.protected_bootstrap_media:
            return "This build does not support protected libvirt bootstrap media."
        missing = [command for command in ("virsh", "qemu-img", "setfacl") if not shutil.which(command)]
        if missing:
            return "Host tools needed: " + ", ".join(missing) + "."
        if not os.access("/dev/kvm", os.R_OK | os.W_OK):
            return "This account needs access to /dev/kvm on a Linux virtualization host."
    else:
        return "Choose an operating system profile."
    return ""


def _selected_path(values: dict, key: str, suffix: str) -> str:
    path = Path(str(values.get(key) or "")).expanduser()
    if path.suffix.lower() != suffix or not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise ProvisioningError(f"Select an existing {suffix} file.")
    return str(path)


def prepare_build(values: dict, *, on_stage, sleeper):
    profile = values.get("os")
    blocker = host_blocker(profile)
    if blocker:
        raise ProvisioningError(blocker)
    windows = profile == "windows-11"
    format_name = values.get("format") or ("vhdx" if windows else "qcow2")
    name = str(values.get("name") or "").strip()
    if not name or len(name) > 100:
        raise ProvisioningError("Enter an image name of up to 100 characters.")
    try:
        cores = int(str(values.get("cpu_count")))
        memory = int(str(values.get("memory_mb")))
        disk = int(str(values.get("disk_size_gib")))
    except ValueError:
        raise ProvisioningError("Enter whole numbers for CPU, memory, and disk size.") from None
    if cores < 1 or memory < 1024 or disk < 16:
        raise ProvisioningError("Allocate at least one CPU, 1024 MiB RAM, and 16 GiB disk.")
    network = str(values.get("network") or "").strip()
    qemu_group = str(values.get("qemu_group") or "").strip()
    if not network or (not windows and not qemu_group):
        raise ProvisioningError("Enter the host-only network and the Linux QEMU access group.")
    for key in ("cache_root", "vm_root"):
        path = Path(str(values.get(key) or "")).expanduser()
        if not path.is_absolute() or not path.is_dir():
            raise ProvisioningError("Select existing folders for the image library and VM storage.")
    cache = Path(values["cache_root"]).expanduser().resolve()
    vm_root = str(Path(values["vm_root"]).expanduser().resolve())
    release = str(values.get("release") or "").strip()
    installation = {
        "unattended": True, "target_os": profile, "target_release": release,
        "update_policy": "manual" if windows else "latest",
    }
    if windows:
        installation["edition"] = values.get("edition")
    else:
        if not re.fullmatch(r"\d{2}\.(04|10)", release):
            raise ProvisioningError("Enter an Ubuntu release such as 24.04.")
        installation.update(
            target_flavor=values.get("flavor"),
            apt_mirror=endpoint_url(str(values.get("mirror") or "").strip(), allow_http=True),
        )
    definition_data = {
        "name": name, "source": {
            "path": _selected_path(values, "iso", ".iso"), "sha256": values.get("iso_sha256"),
            "architecture": "x86_64",
        },
        "machine": {
            "architecture": "x86_64", "cpu_count": cores, "memory_mb": memory,
            "disk_size_gib": disk, "firmware": "uefi" if windows else "bios",
            "disk_bus": "scsi" if windows else "virtio", "network_mode": "host_only",
            "secure_boot": windows, "tpm_version": "2.0" if windows else None,
        },
        "installation": installation,
        "guest_runtime": {
            "bundle_path": _selected_path(values, "runtime", ".zip"),
            "runtime_bundle_sha256": values.get("runtime_sha256"),
            "runtime_version": str(values.get("runtime_version") or "").strip(),
            "target_os": profile, "target_architecture": "x86_64",
        },
    }
    definition = environment_definition_from_mapping(definition_data)
    provider_name = "hyperv" if windows else "libvirt"
    baseline = CapsuleSettings(
        provider=provider_name, guest_os="windows" if windows else "linux", switch_name=network,
        vm_root=vm_root, libvirt_qemu_group=qemu_group, network_mode="host_only", guest_transport="https",
        boot_timeout_seconds=240, agent_timeout_seconds=180,
    )
    kwargs = dict(baseline_settings=baseline, sleeper=sleeper, on_stage=on_stage)
    provider = (HyperVProvisioner(switch_name=network, **kwargs) if windows else
                LibvirtProvisioner(network_name=network, qemu_group=qemu_group, **kwargs))
    evidence_root = cache / "evidence"
    plan = build_provisioning_plan(definition, provider.capabilities(), output_format=format_name,
                                   cache_root=cache, evidence_root=evidence_root)
    binding = {"provider": provider_name, "image_cache_root": str(cache), "vm_root": vm_root,
               "image_format": plan.output_format, "provisioning_evidence_root": str(evidence_root),
               "switch_name": network, "libvirt_qemu_group": qemu_group}
    return provider, definition, definition_data, plan, binding
