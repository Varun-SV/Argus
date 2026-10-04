"""Native NTFS bootstrap disks, inaccessible to non-admin target applications.

This medium is a fixed SCSI disk, never an optical/USB removable device. NTFS
ACLs protect files; Windows' fixed-disk raw-access boundary protects their blocks.
Only SYSTEM and BUILTIN Administrators own/access the payload. No host-specific
user SID is granted access on the guest.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from argus.capsule.base import CapsuleError
from argus.capsule.bootstrap import load_bootstrap_manifest
from argus.capsule.hyperv import _ps_quote


def create_windows_bootstrap_disk(
    source: Path, output: Path, run: Callable[[str, float], str],
) -> Path:
    source = source.resolve()
    output = output.resolve()
    manifest = load_bootstrap_manifest(source)
    members = ("bootstrap.json", manifest.token_file,
               manifest.tls_cert_file, manifest.tls_key_file)
    if members != ("bootstrap.json", "bootstrap.token", "tls-cert.pem", "tls-key.pem"):
        raise CapsuleError("Windows bootstrap requires canonical member names")
    if output.suffix.lower() != ".vhdx" or output.exists():
        raise CapsuleError("Windows bootstrap requires a new VHDX path")
    for member in members:
        path = source / member
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise CapsuleError("Windows bootstrap payload member is invalid")
    # Paths (never secret bytes) enter PowerShell. An empty NTFS root is secured
    # before writing the first secret; Copy-Item inherits that root's DACL.
    script = [
        "$ErrorActionPreference='Stop'",
        "$image=" + _ps_quote(str(output)),
        "New-VHD -Path $image -Dynamic -SizeBytes 64MB -ErrorAction Stop | Out-Null",
        "try {",
        "$disk=Mount-VHD -Path $image -PassThru -ErrorAction Stop | Get-Disk -ErrorAction Stop",
        "$partition=$disk | Initialize-Disk -PartitionStyle GPT -PassThru -ErrorAction Stop | "
        "New-Partition -UseMaximumSize -AssignDriveLetter -ErrorAction Stop",
        "$volume=$partition | Format-Volume -FileSystem NTFS -NewFileSystemLabel "
        "'ARGUS_BOOTSTRAP' -Confirm:$false -Force -ErrorAction Stop",
        "if (!$volume.DriveLetter -or $volume.FileSystem -ne 'NTFS') "
        "{ throw 'invalid bootstrap filesystem' }",
        "$root=[string]$volume.DriveLetter+':\\'",
        "$acl=New-Object System.Security.AccessControl.DirectorySecurity",
        "$acl.SetSecurityDescriptorSddlForm('O:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)')",
        "Set-Acl -LiteralPath $root -AclObject $acl -ErrorAction Stop",
    ]
    for member in members:
        script += [
            "$dest=Join-Path $root " + _ps_quote(member),
            "Copy-Item -LiteralPath " + _ps_quote(str(source / member))
            + " -Destination $dest -ErrorAction Stop",
            "$fileAcl=New-Object System.Security.AccessControl.FileSecurity",
            "$fileAcl.SetSecurityDescriptorSddlForm('O:BAD:P(A;;FA;;;SY)(A;;FA;;;BA)')",
            "Set-Acl -LiteralPath $dest -AclObject $fileAcl -ErrorAction Stop",
        ]
    script += [
        "} finally {",
        "$v=Get-VHD -Path $image -ErrorAction Stop",
        "if ($v.Attached) { Dismount-VHD -Path $image -ErrorAction Stop }",
        "if ((Get-VHD -Path $image -ErrorAction Stop).Attached) "
        "{ throw 'bootstrap disk remains host-mounted' }",
        "}",
    ]
    try:
        run("; ".join(script), 120)
    except Exception:
        # The caller must invoke destroy_windows_bootstrap_disk, which verifies
        # host detachment before unlinking even after an uncertain Mount-VHD.
        raise CapsuleError("Windows protected bootstrap disk construction failed") from None
    if not output.is_file():
        raise CapsuleError("Windows protected bootstrap disk was not created")
    return output


def destroy_windows_bootstrap_disk(media: Path, run: Callable[[str, float], str]) -> None:
    if not media.exists():
        return
    try:
        run(
            "$ErrorActionPreference='Stop'; $image=" + _ps_quote(str(media.resolve()))
            + "; $v=Get-VHD -Path $image -ErrorAction Stop; "
            "if ($v.Attached) { Dismount-VHD -Path $image -ErrorAction Stop }; "
            "if ((Get-VHD -Path $image -ErrorAction Stop).Attached) "
            "{ throw 'bootstrap disk remains host-mounted' }", 30,
        )
    except Exception:
        raise CapsuleError("Windows bootstrap host detachment is uncertain") from None
    media.unlink()


def validate_windows_bootstrap_acl(root: Path) -> None:
    """Reject broad grants/ownership and reparse points before reading secrets."""
    import win32security

    permitted = {"S-1-5-18", "S-1-5-32-544"}
    for path in (root, root / "bootstrap.json", root / "bootstrap.token",
                 root / "tls-cert.pem", root / "tls-key.pem"):
        if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
            raise CapsuleError("Windows bootstrap cannot contain reparse points")
        try:
            descriptor = win32security.GetFileSecurity(
                str(path), win32security.OWNER_SECURITY_INFORMATION
                | win32security.DACL_SECURITY_INFORMATION,
            )
            if win32security.ConvertSidToStringSid(descriptor.GetSecurityDescriptorOwner()) not in permitted:
                raise ValueError()
            control, _revision = descriptor.GetSecurityDescriptorControl()
            if not control & 0x1000:  # SE_DACL_PROTECTED
                raise ValueError()
            dacl = descriptor.GetSecurityDescriptorDacl()
            if dacl is None or dacl.GetAceCount() != 2:
                raise ValueError()
            seen = set()
            for index in range(dacl.GetAceCount()):
                (kind, flags), mask, sid = dacl.GetAce(index)
                sid_text = win32security.ConvertSidToStringSid(sid)
                expected_flags = 3 if path == root else 0  # OI|CI on volume root only
                if (kind != win32security.ACCESS_ALLOWED_ACE_TYPE or flags != expected_flags
                        or sid_text not in permitted or mask != 0x1F01FF):
                    raise ValueError()
                seen.add(sid_text)
            if seen != permitted:
                raise ValueError()
        except Exception:
            raise CapsuleError("Windows bootstrap media access policy is invalid") from None
