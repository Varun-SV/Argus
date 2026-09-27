"""A narrow, credential-free Windows 11 answer profile for Hyper-V builds.

Windows Setup implicitly discovers ``Autounattend.xml`` at the root of a
read-only CD-ROM.  The auxiliary ISO is made inside the private provisioning
workspace and must be detached and removed before that workspace is published.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from pathlib import Path

from argus.provisioning.model import EnvironmentDefinition, ProvisioningError


_UNATTEND = "urn:schemas-microsoft-com:unattend"
_WCM = "http://schemas.microsoft.com/WMIConfig/2002/State"
_SECTOR = 2048
_ANSWER_NAME = b"AUTOUNATTEND.XML;1"
_EDITION_NAMES = {
    "professional": "Windows 11 Pro",
    "education": "Windows 11 Education",
    "enterprise": "Windows 11 Enterprise",
}

ET.register_namespace("", _UNATTEND)
ET.register_namespace("wcm", _WCM)


def _element(parent: ET.Element, name: str, value: str | None = None,
             *, action: bool = False) -> ET.Element:
    attributes = {f"{{{_WCM}}}action": "add"} if action else {}
    child = ET.SubElement(parent, f"{{{_UNATTEND}}}{name}", attributes)
    child.text = value
    return child


def _component(parent: ET.Element, name: str) -> ET.Element:
    return ET.SubElement(parent, f"{{{_UNATTEND}}}component", {
        "name": name,
        "processorArchitecture": "amd64",
        "publicKeyToken": "31bf3856ad364e35",
        "language": "neutral",
        "versionScope": "nonSxS",
    })


def _settings(root: ET.Element, phase: str) -> ET.Element:
    return ET.SubElement(root, f"{{{_UNATTEND}}}settings", {"pass": phase})


def _validate(definition: EnvironmentDefinition) -> str:
    machine = definition.machine
    installation = definition.installation
    if (
        installation.target_os != "windows-11"
        or installation.target_release not in {"23H2", "24H2"}
        or installation.target_flavor is not None
    ):
        raise ProvisioningError("Windows 11 profile requires a supported release and no flavor")
    if not installation.unattended:
        raise ProvisioningError("Windows 11 profile requires unattended=true")
    if (
        machine.architecture != "x86_64"
        or machine.firmware != "uefi"
        or not machine.secure_boot
        or machine.tpm_version != "2.0"
        or machine.disk_bus != "scsi"
        or machine.network_mode != "host_only"
        or machine.cpu_count < 2
        or machine.memory_mb < 4096
        or machine.disk_size_gib < 64
    ):
        raise ProvisioningError("Windows 11 requires the supported Hyper-V UEFI/TPM machine contract")
    if installation.edition not in _EDITION_NAMES:
        raise ProvisioningError("Windows 11 profile requires a supported explicit edition")
    if installation.credential_ref is not None:
        raise ProvisioningError("Windows 11 answer media cannot contain installer credentials")
    if installation.packages:
        raise ProvisioningError("Windows 11 profile cannot guarantee requested packages")
    if installation.update_policy not in {"frozen", "manual"}:
        raise ProvisioningError("Windows 11 profile does not support automatic updates")
    if installation.locale != "en-US" or installation.timezone != "UTC":
        raise ProvisioningError("Windows 11 profile currently supports en-US and UTC")
    return _EDITION_NAMES[installation.edition]


def windows_11_answer_xml(definition: EnvironmentDefinition) -> bytes:
    """Render Setup's UEFI/GPT install and audit-mode shutdown answer file.

    This profile installs Windows from a named image, keeps the VM offline,
    creates no account or password, and shuts down before first audit logon.
    The secure guest agent must be installed by a separate approved bootstrap
    step before the required Capsule baseline can pass.
    """
    image_name = _validate(definition)
    root = ET.Element(f"{{{_UNATTEND}}}unattend")

    winpe = _settings(root, "windowsPE")
    locale = _component(winpe, "Microsoft-Windows-International-Core-WinPE")
    setup_ui = _element(locale, "SetupUILanguage")
    _element(setup_ui, "UILanguage", "en-US")
    for name in ("InputLocale", "SystemLocale", "UILanguage", "UserLocale"):
        _element(locale, name, "en-US")

    setup = _component(winpe, "Microsoft-Windows-Setup")
    disk_configuration = _element(setup, "DiskConfiguration")
    disk = _element(disk_configuration, "Disk", action=True)
    _element(disk, "DiskID", "0")
    _element(disk, "WillWipeDisk", "true")
    creates = _element(disk, "CreatePartitions")
    for order, partition_type, size in (
        (1, "EFI", "260"),
        (2, "MSR", "16"),
        (3, "Primary", None),
    ):
        partition = _element(creates, "CreatePartition", action=True)
        _element(partition, "Order", str(order))
        _element(partition, "Type", partition_type)
        _element(partition, "Size" if size else "Extend", size or "true")
    modifies = _element(disk, "ModifyPartitions")
    for order, partition_id, label, filesystem in (
        (1, 1, "System", "FAT32"),
        (2, 3, "Windows", "NTFS"),
    ):
        partition = _element(modifies, "ModifyPartition", action=True)
        _element(partition, "Order", str(order))
        _element(partition, "PartitionID", str(partition_id))
        _element(partition, "Label", label)
        _element(partition, "Format", filesystem)
    _element(disk_configuration, "WillShowUI", "OnError")

    install = _element(setup, "ImageInstall")
    os_image = _element(install, "OSImage")
    source = _element(os_image, "InstallFrom")
    metadata = _element(source, "MetaData", action=True)
    _element(metadata, "Key", "/IMAGE/NAME")
    _element(metadata, "Value", image_name)
    target = _element(os_image, "InstallTo")
    _element(target, "DiskID", "0")
    _element(target, "PartitionID", "3")
    _element(os_image, "WillShowUI", "OnError")
    user_data = _element(setup, "UserData")
    _element(user_data, "AcceptEula", "true")

    specialize = _settings(root, "specialize")
    runtime_locale = _component(specialize, "Microsoft-Windows-International-Core")
    for name in ("InputLocale", "SystemLocale", "UILanguage", "UserLocale"):
        _element(runtime_locale, name, "en-US")
    shell = _component(specialize, "Microsoft-Windows-Shell-Setup")
    _element(shell, "TimeZone", "UTC")

    oobe = _settings(root, "oobeSystem")
    deployment = _component(oobe, "Microsoft-Windows-Deployment")
    reseal = _element(deployment, "Reseal")
    _element(reseal, "Mode", "Audit")
    _element(reseal, "ForceShutdownNow", "true")

    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _both_endian(value: int, width: int) -> bytes:
    return value.to_bytes(width, "little") + value.to_bytes(width, "big")


def _directory_record(extent: int, size: int, identifier: bytes, *, directory: bool) -> bytes:
    if len(identifier) > 31:
        raise ValueError("ISO 9660 identifier is too long")
    result = bytearray(33 + len(identifier) + (len(identifier) % 2 == 0))
    result[0] = len(result)
    result[2:10] = _both_endian(extent, 4)
    result[10:18] = _both_endian(size, 4)
    result[18:25] = bytes((124, 1, 1, 0, 0, 0, 0))
    result[25] = 2 if directory else 0
    result[28:32] = _both_endian(1, 2)
    result[32] = len(identifier)
    result[33:33 + len(identifier)] = identifier
    return bytes(result)


def _sector(data: bytes) -> bytes:
    if len(data) > _SECTOR:
        raise ValueError("ISO 9660 sector overflow")
    return data.ljust(_SECTOR, b"\x00")


def _single_file_iso(contents: bytes) -> bytes:
    """Produce an ISO 9660 level-2 data disc with one root file."""
    file_sectors = (len(contents) + _SECTOR - 1) // _SECTOR
    total_sectors = 24 + file_sectors
    root_record = _directory_record(23, _SECTOR, b"\x00", directory=True)
    root_directory = _sector(
        root_record
        + _directory_record(23, _SECTOR, b"\x01", directory=True)
        + _directory_record(24, len(contents), _ANSWER_NAME, directory=False)
    )
    path_table_l = b"\x01\x00" + (23).to_bytes(4, "little") + b"\x01\x00\x00\x00"
    path_table_m = b"\x01\x00" + (23).to_bytes(4, "big") + b"\x00\x01\x00\x00"

    primary = bytearray(_SECTOR)
    primary[:7] = b"\x01CD001\x01"
    primary[8:40] = b"ARGUS".ljust(32, b" ")
    primary[40:72] = b"ARGUS_WIN11_ANSWER".ljust(32, b" ")
    primary[80:88] = _both_endian(total_sectors, 4)
    primary[120:124] = _both_endian(1, 2)
    primary[124:128] = _both_endian(1, 2)
    primary[128:132] = _both_endian(_SECTOR, 2)
    primary[132:140] = _both_endian(len(path_table_l), 4)
    primary[140:144] = (19).to_bytes(4, "little")
    primary[148:152] = (21).to_bytes(4, "big")
    primary[156:190] = root_record
    primary[881] = 1
    terminator = b"\xffCD001\x01"

    return (
        b"\x00" * (16 * _SECTOR)
        + bytes(primary)
        + _sector(terminator)
        + bytes(_SECTOR)
        + _sector(path_table_l)
        + bytes(_SECTOR)
        + _sector(path_table_m)
        + bytes(_SECTOR)
        + root_directory
        + contents.ljust(file_sectors * _SECTOR, b"\x00")
    )


def create_windows_11_answer_iso(definition: EnvironmentDefinition, workspace: Path) -> Path:
    """Create private answer media for a second Hyper-V DVD attachment.

    The caller must detach the DVD and unlink this ISO in ``finally`` before
    immutable publication.  No secret reference or secret bytes enter it.
    """
    workspace = Path(workspace)
    if not workspace.is_dir() or workspace.is_symlink():
        raise ProvisioningError("Windows 11 answer workspace must be a private directory")
    data = _single_file_iso(windows_11_answer_xml(definition))
    answer_iso = workspace / "windows-answer.iso"
    created = False
    try:
        with answer_iso.open("xb") as stream:
            created = True
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if created:
            answer_iso.unlink(missing_ok=True)
        raise
    return answer_iso
