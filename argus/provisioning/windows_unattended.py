"""A narrow, credential-free Windows 11 answer profile for Hyper-V builds.

Windows Setup implicitly discovers ``Autounattend.xml`` at the root of a
read-only CD-ROM.  The auxiliary ISO is made inside the private provisioning
workspace and must be detached and removed before that workspace is published.
"""

from __future__ import annotations

import base64
import os
import stat
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable

from argus.provisioning.build_payload import BuildPayload
from argus.provisioning.model import EnvironmentDefinition, ProvisioningError
from argus.provisioning.runtime_bundle import (
    GuestRuntimeBundleManifest,
    verify_guest_runtime_bundle,
)


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


def _runtime_manifest(
    definition: EnvironmentDefinition,
    runtime_manifest: GuestRuntimeBundleManifest | None,
) -> GuestRuntimeBundleManifest:
    identity = definition.require_guest_runtime()
    manifest = runtime_manifest
    if manifest is None:
        manifest = verify_guest_runtime_bundle(identity).manifest
    manifest.validate_identity(identity)
    if identity.target_os != "windows-11":
        raise ProvisioningError(
            "Windows build requires a Windows guest runtime bundle"
        )
    if not manifest.entrypoint.lower().endswith(".exe"):
        raise ProvisioningError(
            "Windows guest runtime entrypoint must be an executable"
        )
    return manifest


def windows_11_answer_xml(
    definition: EnvironmentDefinition,
    runtime_manifest: GuestRuntimeBundleManifest | None = None,
) -> bytes:
    """Render unattended install, Audit-mode customization, and generalization."""
    image_name = _validate(definition)
    runtime_manifest = _runtime_manifest(definition, runtime_manifest)
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
    _element(reseal, "ForceShutdownNow", "false")

    audit = _settings(root, "auditUser")
    audit_deployment = _component(audit, "Microsoft-Windows-Deployment")
    synchronous = _element(audit_deployment, "RunSynchronous")
    install_runtime = _element(
        synchronous, "RunSynchronousCommand", action=True
    )
    _element(install_runtime, "Order", "1")
    _element(
        install_runtime,
        "Path",
        (
            "powershell.exe -NoProfile -NonInteractive "
            "-ExecutionPolicy Bypass -Command "
            "\"$v=(Get-Volume -FileSystemLabel 'ARGUS_BUILD' "
            "-ErrorAction SilentlyContinue|Where-Object DriveLetter|"
            "Select-Object -First 1).DriveLetter;"
            "if(!$v){exit 41};& ($v+':\\INSTALL.PS1')\""
        ),
    )
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



def _ps_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def windows_specialize_answer_xml(entrypoint: str) -> bytes:
    """Secret-free first-Capsule policy; no target account exists at build time."""
    root = ET.Element(f"{{{_UNATTEND}}}unattend")
    specialize = _settings(root, "specialize")
    deployment = _component(specialize, "Microsoft-Windows-Deployment")
    synchronous = _element(deployment, "RunSynchronous")
    command = _element(synchronous, "RunSynchronousCommand", action=True)
    _element(command, "Order", "1")
    _element(command, "Path", '"C:\\ProgramData\\Argus\\Runtime\\'
             + entrypoint.replace("/", "\\") + '" --initialize-target-user')
    shell = _component(_settings(root, "oobeSystem"), "Microsoft-Windows-Shell-Setup")
    oobe = _element(shell, "OOBE")
    for name in ("HideEULAPage", "HideOEMRegistrationScreen", "HideOnlineAccountScreens",
                 "HideWirelessSetupInOOBE"):
        _element(oobe, name, "true")
    _element(oobe, "ProtectYourPC", "3")
    logon = _element(shell, "AutoLogon")
    _element(logon, "Enabled", "true")
    _element(logon, "Username", "argus-target")
    _element(logon, "LogonCount", "999")
    # Password is created during specialize and stored as a Windows LSA secret.
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def windows_runtime_install_script(build_payload: BuildPayload) -> bytes:
    """Create the secret-free Audit-mode installer for the verified runtime."""
    identity = build_payload.runtime_identity
    manifest = build_payload.runtime_manifest
    manifest.validate_identity(identity)
    if identity.target_os != "windows-11":
        raise ProvisioningError("Windows build payload has the wrong target OS")
    if not manifest.entrypoint.lower().endswith(".exe"):
        raise ProvisioningError(
            "Windows guest runtime entrypoint must be an executable"
        )

    entrypoint = manifest.entrypoint.replace("/", "\\")
    checks = {
        "format_version": manifest.format_version,
        "runtime_version": manifest.runtime_version,
        "target_os": manifest.target_os,
        "target_architecture": manifest.target_architecture,
        "bootstrap_schema_version": manifest.bootstrap_schema_version,
        "bootstrap_service_policy_version": (
            manifest.bootstrap_service_policy_version
        ),
        "installation_policy_version": manifest.installation_policy_version,
        "content_sha256": manifest.content_sha256,
        "entrypoint": manifest.entrypoint,
    }
    validation = []
    for field, expected in checks.items():
        validation.append(
            "if([string]$m."
            + field
            + " -ne "
            + _ps_literal(expected)
            + "){throw 'Argus runtime manifest mismatch'}"
        )

    script = [
        "$ErrorActionPreference='Stop'",
        "$v=Get-Volume -FileSystemLabel 'ARGUS_BUILD' "
        "-ErrorAction SilentlyContinue|Where-Object DriveLetter|"
        "Select-Object -First 1",
        "if($null -eq $v){throw 'Argus build media unavailable'}",
        "$media=([string]$v.DriveLetter)+':\\'",
        "$bundle=Join-Path $media 'ARGUSRUNTIME.ZIP'",
        "$expected=" + _ps_literal(identity.runtime_bundle_sha256),
        "if((Get-FileHash -LiteralPath $bundle -Algorithm SHA256).Hash."
        "ToLowerInvariant() -ne $expected){throw 'Argus runtime digest mismatch'}",
        "$root='C:\\ProgramData\\Argus'",
        "$runtime=Join-Path $root 'Runtime'",
        "$runtimeIdentity=Join-Path $root 'runtime-identity.json'",
        "if(Test-Path -LiteralPath $runtime){Remove-Item -LiteralPath "
        "$runtime -Recurse -Force}",
        "New-Item -ItemType Directory -Path $runtime -Force|Out-Null",
        "$acl=New-Object System.Security.AccessControl.DirectorySecurity",
        "$acl.SetSecurityDescriptorSddlForm('D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)')",
        "Set-Acl -LiteralPath $root -AclObject $acl",
        "Expand-Archive -LiteralPath $bundle -DestinationPath $runtime -Force",
        "Copy-Item -LiteralPath (Join-Path $media 'ARGUSBUILD.JSON') "
        "-Destination $runtimeIdentity -Force",
        "$m=Get-Content -LiteralPath (Join-Path $runtime "
        "'argus-runtime-manifest.json') -Raw|ConvertFrom-Json",
        *validation,
        "$entry=Join-Path $runtime " + _ps_literal(entrypoint),
        "if(!(Test-Path -LiteralPath $entry -PathType Leaf))"
        "{throw 'Argus runtime entrypoint missing'}",
        "$runtimeAcl=New-Object System.Security.AccessControl.DirectorySecurity",
        "$runtimeAcl.SetSecurityDescriptorSddlForm('D:P(A;OICI;FA;;;SY)"
        "(A;OICI;FA;;;BA)(A;OICI;GRGX;;;AU)')",
        "Set-Acl -LiteralPath $runtime -AclObject $runtimeAcl",
        "Stop-Service -Name vmicvmsession -Force -ErrorAction SilentlyContinue",
        "Set-Service -Name vmicvmsession -StartupType Disabled",
        "New-NetFirewallRule -DisplayName 'Argus Capsule HTTPS' -Direction Inbound "
        "-Action Allow -Protocol TCP -LocalPort 8765 -Program $entry|Out-Null",
        "if(Get-Service -Name 'ArgusBootstrap' -ErrorAction SilentlyContinue)"
        "{throw 'Argus bootstrap service already exists'}",
        "$binary='\"'+$entry+'\" --bootstrap-service "
        "--runtime-identity-file \"'+$runtimeIdentity+'\" "
        "--control-state-file \"'+(Join-Path $root 'control-state.json')+'\"'",
        "New-Service -Name 'ArgusBootstrap' -BinaryPathName $binary "
        "-StartupType Automatic -DisplayName 'Argus Capsule Bootstrap'|Out-Null",
        "$specialize=Join-Path $root 'capsule-specialize.xml'",
        "[System.IO.File]::WriteAllBytes($specialize,[Convert]::FromBase64String("
        + _ps_literal(base64.b64encode(
            windows_specialize_answer_xml(manifest.entrypoint)
        ).decode('ascii')) + "))",
        # A separate answer file avoids mixing Audit reseal with the final
        # specialization/generalization policy. Installation failure must not
        # be hidden by a successful VM shutdown.
        "& ($env:WINDIR+'\\System32\\Sysprep\\Sysprep.exe') "
        "/generalize /oobe /shutdown ('/unattend:'+$specialize)",
        "if($LASTEXITCODE -ne 0){throw 'Windows native generalization failed'}",
    ]
    return ("\r\n".join(script) + "\r\n").encode("utf-8")


def _write_root_files_iso(
    output: Path,
    *,
    volume_label: bytes,
    files: Iterable[tuple[bytes, Path | bytes]],
) -> Path:
    """Write a small ISO-9660 level-2 disc without external tooling."""
    if len(volume_label) > 32:
        raise ProvisioningError("ISO volume label is too long")
    entries = []
    next_extent = 24
    for identifier, source in files:
        if isinstance(source, Path):
            info = source.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
                raise ProvisioningError(
                    "Windows build payload source must be a nonempty regular file"
                )
            size = info.st_size
        else:
            size = len(source)
        if size <= 0 or size > 0xFFFFFFFF:
            raise ProvisioningError("Windows build payload member size is invalid")
        sectors = (size + _SECTOR - 1) // _SECTOR
        entries.append((identifier, source, size, next_extent, sectors))
        next_extent += sectors

    root_record = _directory_record(23, _SECTOR, b"\x00", directory=True)
    directory_bytes = (
        root_record
        + _directory_record(23, _SECTOR, b"\x01", directory=True)
        + b"".join(
            _directory_record(extent, size, identifier, directory=False)
            for identifier, _source, size, extent, _sectors in entries
        )
    )
    if len(directory_bytes) > _SECTOR:
        raise ProvisioningError("Windows build payload directory is too large")
    root_directory = _sector(directory_bytes)
    path_table_l = (
        b"\x01\x00" + (23).to_bytes(4, "little") + b"\x01\x00\x00\x00"
    )
    path_table_m = (
        b"\x01\x00" + (23).to_bytes(4, "big") + b"\x00\x01\x00\x00"
    )

    primary = bytearray(_SECTOR)
    primary[:7] = b"\x01CD001\x01"
    primary[8:40] = b"ARGUS".ljust(32, b" ")
    primary[40:72] = volume_label.ljust(32, b" ")
    primary[80:88] = _both_endian(next_extent, 4)
    primary[120:124] = _both_endian(1, 2)
    primary[124:128] = _both_endian(1, 2)
    primary[128:132] = _both_endian(_SECTOR, 2)
    primary[132:140] = _both_endian(len(path_table_l), 4)
    primary[140:144] = (19).to_bytes(4, "little")
    primary[148:152] = (21).to_bytes(4, "big")
    primary[156:190] = root_record
    primary[881] = 1

    created = False
    try:
        with output.open("xb") as stream:
            created = True
            stream.write(b"\x00" * (16 * _SECTOR))
            stream.write(primary)
            stream.write(_sector(b"\xffCD001\x01"))
            stream.write(bytes(_SECTOR))
            stream.write(_sector(path_table_l))
            stream.write(bytes(_SECTOR))
            stream.write(_sector(path_table_m))
            stream.write(bytes(_SECTOR))
            stream.write(root_directory)
            for _identifier, source, size, _extent, sectors in entries:
                written = 0
                if isinstance(source, Path):
                    with source.open("rb") as src:
                        while True:
                            chunk = src.read(1024 * 1024)
                            if not chunk:
                                break
                            stream.write(chunk)
                            written += len(chunk)
                else:
                    stream.write(source)
                    written = len(source)
                if written != size:
                    raise ProvisioningError(
                        "Windows build payload source changed while creating ISO"
                    )
                stream.write(b"\x00" * (sectors * _SECTOR - size))
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if created:
            output.unlink(missing_ok=True)
        raise
    return output


def create_windows_build_payload_iso(
    build_payload: BuildPayload,
    workspace: Path,
) -> Path:
    """Create removable, read-only build media carrying the verified runtime."""
    workspace = Path(workspace)
    if not workspace.is_dir() or workspace.is_symlink():
        raise ProvisioningError(
            "Windows build payload workspace must be a private directory"
        )
    script = windows_runtime_install_script(build_payload)
    output = workspace / "windows-build-payload.iso"
    return _write_root_files_iso(
        output,
        volume_label=b"ARGUS_BUILD",
        files=(
            (b"ARGUSRUNTIME.ZIP;1", build_payload.runtime_bundle_path),
            (b"ARGUSBUILD.JSON;1", build_payload.manifest_path),
            (b"INSTALL.PS1;1", script),
        ),
    )

def create_windows_11_answer_iso(
    definition: EnvironmentDefinition,
    workspace: Path,
    runtime_manifest: GuestRuntimeBundleManifest | None = None,
) -> Path:
    """Create private answer media for a second Hyper-V DVD attachment.

    The caller must detach the DVD and unlink this ISO in ``finally`` before
    immutable publication.  No secret reference or secret bytes enter it.
    """
    workspace = Path(workspace)
    if not workspace.is_dir() or workspace.is_symlink():
        raise ProvisioningError("Windows 11 answer workspace must be a private directory")
    data = _single_file_iso(
        windows_11_answer_xml(definition, runtime_manifest)
    )
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
