"""Attended ISO installation backends for the existing Capsule image formats.

These builders deliberately advertise only machine contracts they can carry
through to today's Capsule providers. OS-specific unattended answers, package
installation, TPM state and Secure Boot are not emulated or silently dropped.
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Sequence
from uuid import uuid4

from argus.capsule.hyperv import _ps_quote
from argus.provisioning.build import ProvisioningCleanupError, publish_derived_image
from argus.provisioning.model import EnvironmentDefinition, ProvisioningError
from argus.provisioning.planner import (
    EnvironmentProvisioner,
    ProvisioningPlan,
    ProvisioningProviderCapabilities,
    ProvisioningResult,
    validate_provider_capabilities,
)


def _attended_only(definition: EnvironmentDefinition) -> None:
    installation = definition.installation
    if installation.unattended or installation.credential_ref or installation.packages:
        raise ProvisioningError(
            "attended provider requires unattended=false, no credential_ref and no packages"
        )
    if installation.update_policy != "manual":
        raise ProvisioningError("attended provider requires update_policy=manual")
    if installation.edition is not None:
        raise ProvisioningError("attended provider cannot guarantee installation.edition")
    if installation.locale != "en-US" or installation.timezone != "UTC":
        raise ProvisioningError(
            "attended provider cannot guarantee installation.locale or timezone"
        )


def _command(argv: Sequence[str], timeout: float) -> str:
    try:
        completed = subprocess.run(
            list(argv), text=True, capture_output=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProvisioningError("provisioning hypervisor command failed or timed out") from exc
    if completed.returncode:
        # Hypervisor output may contain operator-entered installer data. Do not
        # copy stderr/stdout into exceptions, logs, manifests, or ATES.
        raise ProvisioningError("provisioning hypervisor command failed")
    return completed.stdout.strip()


class HyperVProvisioner(EnvironmentProvisioner):
    """Build a Generation-2 VHDX via a licensed, operator-supplied ISO."""

    def __init__(
        self, *, switch_name: str, install_timeout_seconds: float = 7200,
        runner: Callable[[str, float], str] | None = None,
        on_started: Callable[[str], None] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.switch_name = switch_name
        self.install_timeout_seconds = install_timeout_seconds
        self._runner = runner
        self._on_started = on_started
        self._sleep = sleeper
        self._powershell = None

    def capabilities(self) -> ProvisioningProviderCapabilities:
        return ProvisioningProviderCapabilities(
            provider="hyperv", host_platforms=("windows",),
            architectures=("x86_64",), media_types=("iso",), image_formats=("vhdx",),
            firmware_modes=("uefi",), disk_buses=("scsi",),
            network_modes=("host_only",),
            secure_boot=True, tpm_versions=("2.0",),
        )

    def _ps(self, script: str, timeout: float = 30) -> str:
        if self._runner is not None:
            return str(self._runner(script, timeout)).strip()
        if self._powershell is None:
            self._powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        if not self._powershell:
            raise ProvisioningError("PowerShell is required for Hyper-V provisioning")
        return _command([
            self._powershell, "-NoProfile", "-NonInteractive", "-Command",
            "$ErrorActionPreference='Stop'; " + script,
        ], timeout)

    def _install(self, definition: EnvironmentDefinition, iso: Path, image: Path) -> None:
        if self._ps("(Get-VMSwitch -Name " + _ps_quote(self.switch_name) +
                    " -ErrorAction Stop).SwitchType.ToString()", 15).lower() != "internal":
            raise ProvisioningError("provisioning requires a Hyper-V Internal switch")
        name = "ArgusProvision-" + uuid4().hex[:20]
        vm = _ps_quote(name)
        # Collision is checked before mutation; cleanup is restricted to this
        # randomly named VM and runs after success, failure or interruption.
        if self._ps("if (Get-VM -Name " + vm + " -ErrorAction SilentlyContinue) {'exists'}"):
            raise ProvisioningError("provisioning VM name already exists")
        owned = False
        vm_dir = image.parent / "vm"
        try:
            size = definition.machine.disk_size_gib
            self._ps(f"New-VHD -Path {_ps_quote(str(image))} -SizeBytes {size}GB "
                     "-Dynamic -ErrorAction Stop | Out-Null", 90)
            self._ps(
                f"New-VM -Name {vm} -Generation 2 "
                f"-MemoryStartupBytes {definition.machine.memory_mb}MB "
                f"-VHDPath {_ps_quote(str(image))} "
                f"-Path {_ps_quote(str(vm_dir))} "
                f"-SwitchName {_ps_quote(self.switch_name)} -ErrorAction Stop | Out-Null", 90
            )
            owned = True
            secure_boot = (
                "On -SecureBootTemplate MicrosoftWindows"
                if definition.machine.secure_boot else "Off"
            )
            tpm_setup = (
                f"Set-VMKeyProtector -VMName {vm} -NewLocalKeyProtector; "
                f"Enable-VMTPM -VMName {vm}; "
                if definition.machine.tpm_version == "2.0" else ""
            )
            setup_script = (
                f"Set-VMProcessor -VMName {vm} -Count {definition.machine.cpu_count}; "
                f"Set-VM -Name {vm} -AutomaticCheckpointsEnabled $false "
                "-AutomaticStartAction Nothing -AutomaticStopAction TurnOff; "
                f"Add-VMDvdDrive -VMName {vm} -Path {_ps_quote(str(iso))}; "
                f"Set-VMFirmware -VMName {vm} -EnableSecureBoot {secure_boot} "
                f"-FirstBootDevice (Get-VMDvdDrive -VMName {vm}); "
            ) + tpm_setup + (
                f"Add-VMNetworkAdapterExtendedAcl -VMName {vm} -Action Deny "
                "-Direction Inbound -Weight 1; "
                f"Add-VMNetworkAdapterExtendedAcl -VMName {vm} -Action Deny "
                "-Direction Outbound -Weight 1"
            )
            self._ps(setup_script, 60)
            self._ps(f"Start-VM -Name {vm} -ErrorAction Stop | Out-Null", 60)
            if self._on_started is not None:
                self._on_started(name)
            deadline = time.monotonic() + self.install_timeout_seconds
            while time.monotonic() < deadline:
                if self._ps(f"(Get-VM -Name {vm} -ErrorAction Stop).State.ToString()", 15
                            ).lower() == "off":
                    break
                self._sleep(5)
            else:
                raise ProvisioningError("Hyper-V OS installation did not shut down before timeout")
        finally:
            if not owned:
                # New-VM may have created the VM before a transport failure.
                # Only claim it when Hyper-V reports our private VM path.
                try:
                    reported_path = self._ps(
                        f"$v=Get-VM -Name {vm} -ErrorAction SilentlyContinue; "
                        "if ($v) { $v.Path }", 15,
                    )
                    owned = bool(reported_path) and (
                        Path(reported_path).resolve() == vm_dir.resolve()
                    )
                except Exception as exc:
                    raise ProvisioningCleanupError(
                        "cannot establish provisioning VM ownership after failure"
                    ) from exc
            if owned:
                try:
                    self._ps(f"Stop-VM -Name {vm} -TurnOff -Force -ErrorAction SilentlyContinue; "
                             f"Remove-VM -Name {vm} -Force -ErrorAction Stop", 60)
                    if vm_dir.exists():
                        shutil.rmtree(vm_dir)
                except Exception as exc:
                    raise ProvisioningCleanupError("cannot clean up provisioning VM") from exc
        metadata = self._ps(
            f"$v=Get-VHD -Path {_ps_quote(str(image))} -ErrorAction Stop; "
            "\"$($v.VhdType):$($v.Size)\"", 30,
        )
        try:
            disk_type, virtual_size = metadata.split(":", 1)
            valid = disk_type in {"Dynamic", "Fixed"} and (
                int(virtual_size) >= definition.machine.disk_size_gib * 1024**3
            )
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise ProvisioningError("Hyper-V image type or virtual size is invalid")

    def provision(
        self, definition: EnvironmentDefinition, plan: ProvisioningPlan
    ) -> ProvisioningResult:
        if plan.provider != "hyperv" or plan.output_format != "vhdx":
            raise ProvisioningError("Hyper-V plan provider or output format mismatch")
        validate_provider_capabilities(definition, self.capabilities(), output_format="vhdx")
        _attended_only(definition)
        if platform.system().lower() != "windows" and self._runner is None:
            raise ProvisioningError("Hyper-V provisioning requires Windows")
        if not self.switch_name:
            raise ProvisioningError("Hyper-V Internal switch name is required")
        return publish_derived_image(
            definition, plan, lambda iso, image: self._install(definition, iso, image)
        )


class LibvirtProvisioner(EnvironmentProvisioner):
    """Build a BIOS/virtio qcow2 or raw image on local system libvirt."""

    def __init__(
        self, *, network_name: str, install_timeout_seconds: float = 7200,
        runner: Callable[[Sequence[str], float], str] | None = None,
        on_started: Callable[[str], None] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.network_name = network_name
        self.install_timeout_seconds = install_timeout_seconds
        self._runner = runner
        self._on_started = on_started
        self._sleep = sleeper

    def capabilities(self) -> ProvisioningProviderCapabilities:
        return ProvisioningProviderCapabilities(
            provider="libvirt", host_platforms=("linux",),
            architectures=("x86_64",), media_types=("iso",),
            image_formats=("qcow2", "raw"), firmware_modes=("bios",),
            disk_buses=("virtio",), network_modes=("host_only",),
        )

    def _run(self, argv: Sequence[str], timeout: float = 30) -> str:
        if self._runner is not None:
            return str(self._runner(tuple(argv), timeout)).strip()
        return _command(argv, timeout)

    def _virsh(self, *args: str, timeout: float = 30) -> str:
        return self._run(("virsh", "-c", "qemu:///system", *args), timeout)

    def _network(self) -> None:
        if not self.network_name or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_"
                                        for c in self.network_name):
            raise ProvisioningError("libvirt network name must be a safe lowercase identifier")
        try:
            root = ET.fromstring(self._virsh("net-dumpxml", self.network_name, timeout=15))
        except ET.ParseError as exc:
            raise ProvisioningError("libvirt network XML is invalid") from exc
        if root.tag != "network" or root.findtext("name") != self.network_name or (
            root.find("forward") is not None or root.find("bridge") is None
        ):
            raise ProvisioningError("libvirt provisioning requires a non-forwarding host network")
        if self._virsh("net-info", self.network_name, timeout=15).find("Active: yes") < 0:
            raise ProvisioningError("libvirt host-only network is not active")

    def _domain_xml(self, name: str, iso: Path, image: Path,
                    definition: EnvironmentDefinition, fmt: str) -> str:
        machine = definition.machine
        root = ET.Element("domain", {"type": "kvm"})
        ET.SubElement(root, "name").text = name
        ET.SubElement(root, "memory", {"unit": "MiB"}).text = str(machine.memory_mb)
        ET.SubElement(root, "vcpu").text = str(machine.cpu_count)
        os_node = ET.SubElement(root, "os")
        ET.SubElement(os_node, "type", {"arch": "x86_64"}).text = "hvm"
        ET.SubElement(os_node, "boot", {"dev": "cdrom"})
        ET.SubElement(os_node, "boot", {"dev": "hd"})
        ET.SubElement(root, "on_poweroff").text = "destroy"
        devices = ET.SubElement(root, "devices")
        disk = ET.SubElement(devices, "disk", {"type": "file", "device": "disk"})
        ET.SubElement(disk, "driver", {"name": "qemu", "type": fmt})
        ET.SubElement(disk, "source", {"file": str(image)})
        ET.SubElement(disk, "target", {"dev": "vda", "bus": "virtio"})
        cd = ET.SubElement(devices, "disk", {"type": "file", "device": "cdrom"})
        ET.SubElement(cd, "driver", {"name": "qemu", "type": "raw"})
        ET.SubElement(cd, "source", {"file": str(iso)})
        ET.SubElement(cd, "target", {"dev": "hda", "bus": "ide"})
        ET.SubElement(cd, "readonly")
        interface = ET.SubElement(devices, "interface", {"type": "network"})
        ET.SubElement(interface, "source", {"network": self.network_name})
        ET.SubElement(interface, "model", {"type": "virtio"})
        ET.SubElement(devices, "graphics", {
            "type": "vnc", "autoport": "yes", "port": "-1", "listen": "127.0.0.1"
        })
        ET.SubElement(devices, "console", {"type": "pty"})
        return ET.tostring(root, encoding="unicode")

    def _install(self, definition: EnvironmentDefinition, iso: Path,
                 image: Path, fmt: str) -> None:
        self._network()
        name = "argus-provision-" + uuid4().hex[:20]
        if self._virsh("list", "--all", "--name").splitlines().count(name):
            raise ProvisioningError("provisioning VM name already exists")
        self._run(("qemu-img", "create", "-f", fmt, str(image),
                   f"{definition.machine.disk_size_gib}G"), 90)
        xml = image.parent / "domain.xml"
        xml.write_text(self._domain_xml(name, iso, image, definition, fmt), encoding="utf-8")
        owned = False
        try:
            self._virsh("define", str(xml), timeout=60)
            owned = True
            self._virsh("start", name, timeout=60)
            if self._on_started is not None:
                self._on_started(name)
            deadline = time.monotonic() + self.install_timeout_seconds
            while time.monotonic() < deadline:
                if self._virsh("domstate", name, timeout=15).lower() == "shut off":
                    break
                self._sleep(5)
            else:
                raise ProvisioningError("libvirt OS installation did not shut down before timeout")
        finally:
            try:
                if not owned:
                    # virsh define may succeed before an invocation error.
                    # Never destroy a VM unless its disk source is our workdir.
                    try:
                        xml_text = self._virsh("dumpxml", name, timeout=15)
                        root = ET.fromstring(xml_text) if xml_text else None
                        owned = root is not None and any(
                            disk.attrib.get("file") == str(image)
                            for disk in root.findall("./devices/disk/source")
                        )
                    except Exception as exc:
                        raise ProvisioningCleanupError(
                            "cannot establish provisioning VM ownership after failure"
                        ) from exc
                if owned:
                    if self._virsh("domstate", name, timeout=15).lower() != "shut off":
                        self._virsh("destroy", name, timeout=30)
                    self._virsh("undefine", name, timeout=30)
            except Exception as exc:
                raise ProvisioningCleanupError("cannot clean up provisioning VM") from exc
            finally:
                xml.unlink(missing_ok=True)
        if fmt == "qcow2":
            self._run(("qemu-img", "check", "-f", "qcow2", str(image)), 90)
        try:
            metadata = json.loads(self._run(
                ("qemu-img", "info", "--output=json", str(image)), 30
            ))
            valid = metadata.get("format") == fmt and (
                int(metadata.get("virtual-size", 0)) >=
                definition.machine.disk_size_gib * 1024**3
            )
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise ProvisioningError("libvirt image format or virtual size is invalid")

    def provision(self, definition: EnvironmentDefinition,
                  plan: ProvisioningPlan) -> ProvisioningResult:
        if plan.provider != "libvirt" or plan.output_format not in {"qcow2", "raw"}:
            raise ProvisioningError("libvirt plan provider or output format mismatch")
        validate_provider_capabilities(
            definition, self.capabilities(), output_format=plan.output_format
        )
        _attended_only(definition)
        if platform.system().lower() != "linux" and self._runner is None:
            raise ProvisioningError("libvirt provisioning requires Linux")
        return publish_derived_image(
            definition, plan,
            lambda iso, image: self._install(definition, iso, image, plan.output_format),
        )
