"""Windows 11 answer media regression coverage; no Hyper-V host is required."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET

import pytest

from argus.provisioning.model import (
    EnvironmentDefinition,
    GuestRuntimeIdentity,
    InstallationMediaSource,
    InstallationSpec,
    MachineSpec,
    ProvisioningError,
)
from argus.provisioning import create_guest_runtime_bundle
from argus.provisioning.windows_unattended import (
    create_windows_11_answer_iso,
    windows_11_answer_xml,
)
from argus.provisioning.planner import build_provisioning_plan
from argus.provisioning.providers import HyperVProvisioner


_NS = {"a": "urn:schemas-microsoft-com:unattend"}
_SECTOR = 2048


def _guest_runtime(tmp_path: Path) -> GuestRuntimeIdentity:
    payload = tmp_path / "windows-runtime"
    entrypoint = payload / "bin" / "argus-guest-agent.exe"
    entrypoint.parent.mkdir(parents=True, exist_ok=True)
    entrypoint.write_bytes(b"argus guest runtime")
    bundle = tmp_path / "argus-runtime-windows.zip"
    if not bundle.exists():
        create_guest_runtime_bundle(
            payload,
            bundle,
            runtime_version="0.1.0-dev.0",
            target_os="windows-11",
            target_architecture="x86_64",
            entrypoint="bin/argus-guest-agent.exe",
        )
    return GuestRuntimeIdentity(
        bundle_path=str(bundle),
        runtime_bundle_sha256=sha256(bundle.read_bytes()).hexdigest(),
        runtime_version="0.1.0-dev.0",
        target_os="windows-11",
    )


def _definition(tmp_path: Path) -> EnvironmentDefinition:
    iso = tmp_path / "operator.iso"
    iso.write_bytes(b"operator media")
    return EnvironmentDefinition(
        name="windows-11-pro",
        source=InstallationMediaSource(
            path=str(iso), sha256=sha256(b"operator media").hexdigest()
        ),
        machine=MachineSpec(
            architecture="x86_64", cpu_count=2, memory_mb=4096,
            firmware="uefi", secure_boot=True, tpm_version="2.0",
            disk_size_gib=64, disk_bus="scsi", network_mode="host_only",
        ),
        installation=InstallationSpec(
            unattended=True, edition="professional", update_policy="frozen",
            target_os="windows-11", target_release="24H2",
        ),
        guest_runtime=_guest_runtime(tmp_path),
    )


def test_answer_selects_edition_and_installs_to_uefi_disk(tmp_path: Path) -> None:
    root = ET.fromstring(windows_11_answer_xml(_definition(tmp_path)))
    setup = root.find("a:settings[@pass='windowsPE']/a:component[@name='Microsoft-Windows-Setup']", _NS)
    assert setup is not None
    assert setup.findtext("a:ImageInstall/a:OSImage/a:InstallFrom/a:MetaData/a:Key", namespaces=_NS) == "/IMAGE/NAME"
    assert setup.findtext("a:ImageInstall/a:OSImage/a:InstallFrom/a:MetaData/a:Value", namespaces=_NS) == "Windows 11 Pro"
    assert setup.findtext("a:ImageInstall/a:OSImage/a:InstallTo/a:PartitionID", namespaces=_NS) == "3"
    assert setup.findtext("a:DiskConfiguration/a:Disk/a:WillWipeDisk", namespaces=_NS) == "true"
    creates = setup.findall("a:DiskConfiguration/a:Disk/a:CreatePartitions/a:CreatePartition", _NS)
    assert [item.findtext("a:Type", namespaces=_NS) for item in creates] == ["EFI", "MSR", "Primary"]
    assert creates[-1].findtext("a:Extend", namespaces=_NS) == "true"
    shutdown = root.find("a:settings[@pass='oobeSystem']/a:component[@name='Microsoft-Windows-Deployment']/a:Reseal", _NS)
    assert shutdown is not None
    assert shutdown.findtext("a:Mode", namespaces=_NS) == "Audit"
    assert shutdown.findtext("a:ForceShutdownNow", namespaces=_NS) == "false"
    audit = root.find(
        "a:settings[@pass='auditUser']/a:component"
        "[@name='Microsoft-Windows-Deployment']",
        _NS,
    )
    assert audit is not None
    command = audit.findtext(
        "a:RunSynchronous/a:RunSynchronousCommand/a:Path",
        namespaces=_NS,
    )
    assert command is not None and "ARGUS_BUILD" in command
    # The Audit installer invokes native Sysprep with a separate, secret-free
    # Capsule specialization answer file; it must not mix Generalize/Reseal.
    assert audit.find("a:Generalize", _NS) is None


def test_answer_iso_has_root_autounattend_xml(tmp_path: Path) -> None:
    definition = _definition(tmp_path)
    answer_iso = create_windows_11_answer_iso(definition, tmp_path)
    disc = answer_iso.read_bytes()
    primary = disc[16 * _SECTOR:17 * _SECTOR]
    assert primary[:7] == b"\x01CD001\x01"
    assert disc[17 * _SECTOR:17 * _SECTOR + 7] == b"\xffCD001\x01"
    assert int.from_bytes(primary[80:84], "little") == len(disc) // _SECTOR
    root_sector = int.from_bytes(primary[158:162], "little")
    directory = disc[root_sector * _SECTOR:(root_sector + 1) * _SECTOR]
    first_length = directory[0]
    second_length = directory[first_length]
    file_record = directory[first_length + second_length:]
    assert file_record[32] == len(b"AUTOUNATTEND.XML;1")
    assert file_record[33:33 + file_record[32]] == b"AUTOUNATTEND.XML;1"
    extent = int.from_bytes(file_record[2:6], "little")
    size = int.from_bytes(file_record[10:14], "little")
    assert disc[extent * _SECTOR:extent * _SECTOR + size] == windows_11_answer_xml(definition)
    assert b"secret://" not in disc
    assert b"Password" not in disc
    root = ET.fromstring(windows_11_answer_xml(definition))
    assert root.find(".//a:ProductKey/a:Key", _NS) is None


@pytest.mark.parametrize("edition,image_name", [
    ("professional", "Windows 11 Pro"),
    ("education", "Windows 11 Education"),
    ("enterprise", "Windows 11 Enterprise"),
])
def test_answer_defers_activation_without_interactive_product_key_ui(
    tmp_path: Path, edition: str, image_name: str,
) -> None:
    definition = _definition(tmp_path)
    definition = replace(definition, installation=replace(definition.installation, edition=edition))
    root = ET.fromstring(windows_11_answer_xml(definition))
    setup = root.find("a:settings[@pass='windowsPE']/a:component[@name='Microsoft-Windows-Setup']", _NS)
    assert setup.findtext("a:ImageInstall/a:OSImage/a:InstallFrom/a:MetaData/a:Value", namespaces=_NS) == image_name
    assert setup.findtext("a:UserData/a:ProductKey/a:WillShowUI", namespaces=_NS) == "Never"
    assert root.find(".//a:ProductKey/a:Key", _NS) is None
    assert root.find("a:settings[@pass='specialize']/a:component[@name='Microsoft-Windows-Shell-Setup']/a:ProductKey", _NS) is None


def test_hyperv_builder_attaches_and_discards_private_answer_media(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    definition = _definition(tmp_path)
    scripts: list[str] = []

    def run(script: str, _timeout: float) -> str:
        scripts.append(script)
        if script.startswith("New-VHD"):
            match = re.search(r"-Path '([^']+)'", script)
            assert match is not None
            Path(match.group(1)).write_bytes(b"fixture image")
        if "Get-VMSwitch" in script:
            return "Internal"
        if "Get-VHD" in script:
            return "Dynamic:68719476736"
        if ".State.ToString()" in script:
            return "Off"
        return ""

    provider = HyperVProvisioner(
        switch_name="Argus-Internal", runner=run, baseline_validator=lambda _image: None,
    )
    plan = build_provisioning_plan(
        definition, provider.capabilities(), output_format="vhdx", cache_root=tmp_path / "cache",
    )
    result = provider.provision(definition, plan)
    assert result.manifest.evidence_run_id is not None
    assert any(
        "Add-VMDvdDrive" in script
        and "windows-answer.iso" in script
        and "windows-build-payload.iso" in script
        and "-FirstBootDevice $installDrive" in script
        for script in scripts
    )
    assert not (plan.cache_dir / "windows-answer.iso").exists()
    assert not (plan.cache_dir / "windows-build-payload.iso").exists()
    boot = next(script for script in scripts if "InvokeMethod('TypeKey'" in script)
    assert boot.index("-Class Msvm_Keyboard") < boot.index("Start-VM")
    assert boot.index("Start-VM") < boot.index("GetRelated('Msvm_Keyboard')")
    assert boot.index("Start-VM") < boot.index("InvokeMethod('TypeKey'")
    assert "$id=$installer.Id.ToString()" in boot
    assert "$result.ReturnValue -ne 0" in boot
    assert sum("InvokeMethod('TypeKey'" in script for script in scripts) == 1


def test_failed_boot_confirmation_cleans_installer_and_prevents_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    definition = _definition(tmp_path)
    scripts = []
    started, validated = [], []

    def run(script, timeout):
        scripts.append(script)
        if "Get-VMSwitch" in script:
            return "Internal"
        if script.startswith("New-VHD"):
            Path(re.search(r"-Path '([^']+)'", script).group(1)).write_bytes(b"partial disk")
        if "InvokeMethod('TypeKey'" in script:
            raise ProvisioningError("boot confirmation failed")
        return ""

    provider = HyperVProvisioner(
        switch_name="Argus-Internal", runner=run, on_started=started.append,
        baseline_validator=validated.append,
    )
    plan = build_provisioning_plan(
        definition, provider.capabilities(), output_format="vhdx", cache_root=tmp_path / "cache",
    )
    with pytest.raises(ProvisioningError, match="boot confirmation failed"):
        provider.provision(definition, plan)
    assert not started and not validated
    assert any("Remove-VM" in script for script in scripts)
    assert not plan.cache_dir.exists()
    assert not list(plan.cache_dir.parent.glob(".building-*"))


@pytest.mark.skipif(os.name != "nt" or not shutil.which("powershell.exe"),
                    reason="requires Windows PowerShell")
@pytest.mark.parametrize("returns,success,calls", [
    ("0,0,0,0,0,0,0,0,0", True, 9),
    ("0,32775", True, 2),
    ("32775", False, 1),
    ("0,32769", False, 2),
    ("$null", False, 1),
    ("0,$null", False, 2),
])
def test_boot_keyboard_handoff_requires_prior_acknowledgement(
    returns: str, success: bool, calls: int,
) -> None:
    scripts = []
    HyperVProvisioner(switch_name="unused", runner=lambda script, timeout: scripts.append(script))._start_installer("fixture")
    # Execute the production PowerShell branches with in-memory WMI doubles.
    # These functions shadow all native operations: no VM or host is changed.
    shim = """
$ErrorActionPreference='Stop'
$global:callCount=0
$keyboard=New-Object PSObject
$keyboard | Add-Member ScriptMethod GetMethodParameters { param($name) @{keyCode=0} }
$keyboard | Add-Member ScriptMethod InvokeMethod {
    param($name,$key,$options)
    $code=$global:codes[$global:callCount]
    $global:callCount++
    return [pscustomobject]@{ReturnValue=$code}
}
$system=New-Object PSObject
$system | Add-Member ScriptMethod GetRelated { param($name) return $keyboard }
function Get-VM { return [pscustomobject]@{Id=[guid]::Empty} }
function Get-WmiObject { return $system }
function Start-VM {}
function Start-Sleep {}
"""
    command = shim + "$global:codes=@(" + returns + "); try { " + scripts[0] + (
        "; Write-Output 'accepted' } catch { Write-Output 'rejected' }; "
        "Write-Output $global:callCount"
    )
    result = subprocess.run([shutil.which("powershell.exe"), "-NoProfile", "-NonInteractive",
                             "-Command", command], capture_output=True, text=True, timeout=15, check=True)
    assert result.stdout.splitlines() == ["accepted" if success else "rejected", str(calls)]


@pytest.mark.skipif(os.name != "nt" or not shutil.which("tar"),
                    reason="requires the Windows ISO reader")
def test_windows_iso_reader_finds_answer_file(tmp_path: Path) -> None:
    answer_iso = create_windows_11_answer_iso(_definition(tmp_path), tmp_path)
    listed = subprocess.run(
        ["tar", "-tf", str(answer_iso)], check=True, capture_output=True, text=True
    )
    assert "AUTOUNATTEND.XML" in listed.stdout.splitlines()


@pytest.mark.parametrize("field,value", [
    ("credential_ref", "secret://operator/account"),
    ("packages", ("git",)),
    ("update_policy", "latest"),
    ("locale", "fr-FR"),
    ("timezone", "Pacific Standard Time"),
    ("edition", None),
    ("target_os", "ubuntu"),
    ("target_release", "25H2"),
    ("target_flavor", "desktop"),
])
def test_answer_refuses_unsupported_installation_inputs(
    tmp_path: Path, field: str, value: object
) -> None:
    definition = _definition(tmp_path)
    installation = replace(definition.installation, **{field: value})
    with pytest.raises(ProvisioningError):
        windows_11_answer_xml(replace(definition, installation=installation))


@pytest.mark.parametrize("field,value", [
    ("secure_boot", False),
    ("tpm_version", None),
    ("cpu_count", 1),
    ("memory_mb", 2048),
    ("disk_size_gib", 32),
    ("disk_bus", "nvme"),
])
def test_answer_refuses_unsupported_machine_contract(
    tmp_path: Path, field: str, value: object
) -> None:
    definition = _definition(tmp_path)
    machine = replace(definition.machine, **{field: value})
    with pytest.raises(ProvisioningError):
        windows_11_answer_xml(replace(definition, machine=machine))


def test_answer_iso_does_not_replace_existing_media(tmp_path: Path) -> None:
    definition = _definition(tmp_path)
    existing = tmp_path / "windows-answer.iso"
    existing.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        create_windows_11_answer_iso(definition, tmp_path)
    assert existing.read_bytes() == b"existing"
