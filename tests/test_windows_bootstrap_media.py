from __future__ import annotations

from pathlib import Path, PureWindowsPath
import os
from types import SimpleNamespace
import sys

import pytest

from argus.capsule.base import CapsuleError, CapsuleHandle
from argus.capsule.bootstrap import create_bootstrap_attempt
from argus.capsule.control import new_capsule_id
from argus.capsule.hyperv_isolated import IsolatedHyperVProvider
from argus.capsule.windows_bootstrap import (
    create_windows_bootstrap_disk, destroy_windows_bootstrap_disk,
    validate_windows_bootstrap_acl,
)


def _attempt(tmp_path):
    return create_bootstrap_attempt(
        tmp_path / "attempts", capsule_id=new_capsule_id(),
        control_generation=1, execution_mode="isolated",
        runtime_identity="runtime-sha256-" + "a" * 64,
    )


def test_ntfs_payload_is_protected_before_copy_and_dismounted(tmp_path):
    attempt = _attempt(tmp_path)
    media = tmp_path / "bootstrap.vhdx"
    calls = []

    def run(script, _timeout):
        calls.append(script)
        media.write_bytes(b"fake-vhdx")
        return ""

    create_windows_bootstrap_disk(attempt.root, media, run)
    script = calls[0]
    assert script.index("Set-Acl -LiteralPath $root") < script.index("Copy-Item")
    assert "O:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)" in script
    assert "Format-Volume -FileSystem NTFS" in script
    assert "finally" in script and "Dismount-VHD" in script
    assert "if ((Get-VHD -Path $image -ErrorAction Stop).Attached)" in script
    assert attempt.bootstrap_token not in script
    assert attempt.tls_key_path.read_text() not in script
    destroy_windows_bootstrap_disk(media, run)
    assert not media.exists()
    attempt.destroy()


def test_uncertain_host_unmount_preserves_secret_medium(tmp_path):
    media = tmp_path / "bootstrap.vhdx"
    media.write_bytes(b"secret-bearing-disk")

    def fail(_script, _timeout):
        raise OSError("stderr-containing-secret-sentinel")

    with pytest.raises(CapsuleError, match="host detachment is uncertain") as error:
        destroy_windows_bootstrap_disk(media, fail)
    assert "secret-sentinel" not in str(error.value)
    assert media.is_file()


def test_partial_native_construction_requires_mount_check_before_cleanup(tmp_path):
    attempt = _attempt(tmp_path)
    media = tmp_path / "bootstrap.vhdx"

    def fail(_script, _timeout):
        media.write_bytes(b"partially-rendered-secrets")
        raise OSError("provider-secret-sentinel")

    with pytest.raises(CapsuleError, match="construction failed") as error:
        create_windows_bootstrap_disk(attempt.root, media, fail)
    assert "secret-sentinel" not in str(error.value)
    assert media.exists()
    calls = []
    destroy_windows_bootstrap_disk(media, lambda script, _timeout: calls.append(script) or "")
    assert "Get-VHD" in calls[0] and "Dismount-VHD" in calls[0]
    assert not media.exists()
    attempt.destroy()


def test_hyperv_attaches_fixed_scsi_disk_and_confirms_removal(tmp_path):
    calls = []
    provider = IsolatedHyperVProvider(runner=lambda script, _timeout: calls.append(script) or "Off")
    handle = CapsuleHandle(session_id="session", provider="hyperv", vm_name="Capsule",
                           root_dir=str(tmp_path), address="", guest_port=8765)
    media = tmp_path / "bootstrap.vhdx"
    media.touch()
    provider.attach_bootstrap(handle, media)
    provider.detach_bootstrap(handle, media)
    assert "Add-VMHardDiskDrive" in calls[1] and "-ControllerType SCSI" in calls[1]
    assert calls[1].index("Get-VHD") < calls[1].index("Add-VMHardDiskDrive")
    assert "Remove-VMHardDiskDrive" in calls[2] and "if ($left)" in calls[2]
    assert not any("VMDvdDrive" in call for call in calls)
    iso = tmp_path / "bootstrap.iso"
    iso.touch()
    with pytest.raises(CapsuleError, match="VHDX"):
        provider.attach_bootstrap(handle, iso)


@pytest.mark.parametrize("variant", ["valid", "broad-grant", "unprotected", "user-owner", "null-dacl", "inherit-only"])
def test_guest_rejects_unprotected_ntfs_policy(tmp_path, monkeypatch, variant):
    for member in ("bootstrap.json", "bootstrap.token", "tls-cert.pem", "tls-key.pem"):
        (tmp_path / member).touch()
    aces = [((0, 0), 0x1F01FF, "S-1-5-18"), ((0, 0), 0x1F01FF, "S-1-5-32-544")]
    if variant == "broad-grant":
        aces[1] = ((0, 0), 0x1F01FF, "S-1-5-11")
    dacl = SimpleNamespace(GetAceCount=lambda: len(aces), GetAce=lambda index: aces[index])
    descriptor = SimpleNamespace(
        GetSecurityDescriptorOwner=lambda: "S-1-5-21-1234" if variant == "user-owner" else "S-1-5-32-544",
        GetSecurityDescriptorControl=lambda: (0 if variant == "unprotected" else 0x1000, 1),
        GetSecurityDescriptorDacl=lambda: None if variant == "null-dacl" else dacl,
    )
    monkeypatch.setitem(sys.modules, "win32security", SimpleNamespace(
        OWNER_SECURITY_INFORMATION=1, DACL_SECURITY_INFORMATION=4,
        ACCESS_ALLOWED_ACE_TYPE=0, GetFileSecurity=lambda *_args: descriptor,
        ConvertSidToStringSid=lambda sid: sid,
    ))
    original_ace = dacl.GetAce
    def security(path, _information):
        flags = 8 if variant == "inherit-only" else (3 if Path(path) == tmp_path else 0)
        dacl.GetAce = lambda index: ((0, flags),
                                    original_ace(index)[1], original_ace(index)[2])
        return descriptor
    sys.modules["win32security"].GetFileSecurity = security
    if variant == "valid":
        validate_windows_bootstrap_acl(tmp_path)
    else:
        with pytest.raises(CapsuleError, match="access policy"):
            validate_windows_bootstrap_acl(tmp_path)


@pytest.mark.parametrize("drive_type,filesystem", [(5, "NTFS"), (2, "NTFS"), (3, "FAT32"), (3, "NTFS")])
def test_windows_guest_requires_fixed_ntfs_before_acl_checks(monkeypatch, drive_type, filesystem):
    from argus.capsule import bootstrap_service, windows_bootstrap
    checked = []

    def volume(_root, label, _label_size, _serial, _max, _flags, fs, _fs_size):
        label.value = "ARGUS_BOOTSTRAP"
        fs.value = filesystem
        return 1

    monkeypatch.setattr(bootstrap_service.ctypes, "windll", SimpleNamespace(kernel32=SimpleNamespace(
        GetVolumeInformationW=volume, GetDriveTypeW=lambda _root: drive_type,
    )), raising=False)
    monkeypatch.setattr(windows_bootstrap, "validate_windows_bootstrap_acl", lambda root: checked.append(root))
    root = PureWindowsPath("D:\\")
    if drive_type == 3 and filesystem == "NTFS":
        bootstrap_service._validate_windows_bootstrap_root(root)
        assert checked == [root]
    else:
        with pytest.raises(CapsuleError, match="fixed NTFS"):
            bootstrap_service._validate_windows_bootstrap_root(root)
        assert checked == []


def test_configured_windows_root_does_not_bypass_native_media_validation(tmp_path, monkeypatch):
    from argus.capsule import bootstrap_service
    monkeypatch.setattr(bootstrap_service.platform, "system", lambda: "Windows")
    checked = []

    def reject(root):
        checked.append(root)
        raise CapsuleError("unprotected configured media")

    monkeypatch.setattr(bootstrap_service, "_validate_windows_bootstrap_root", reject)
    with pytest.raises(CapsuleError, match="unprotected configured"):
        with bootstrap_service._bootstrap_source_root(tmp_path):
            pytest.fail("unprotected medium was yielded")
    assert checked == [tmp_path.resolve()]


@pytest.mark.skipif(sys.platform != "win32" or os.environ.get("ARGUS_NATIVE_BOOTSTRAP_TEST") != "1",
                    reason="requires opt-in elevated Hyper-V host and dedicated non-admin test account")
def test_native_ntfs_medium_denies_non_admin_file_and_raw_reads(tmp_path):
    """Native media boundary check; full installed-VM acceptance is separate."""
    import win32con
    import win32file
    import win32net
    import win32netcon
    import win32security
    from argus.capsule.hyperv import _ps_quote

    name = os.environ["ARGUS_BOOTSTRAP_TEST_USER"]
    password = os.environ["ARGUS_BOOTSTRAP_TEST_PASSWORD"]
    admins_sid = win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid, None)
    admins, _, _ = win32security.LookupAccountSid(None, admins_sid)
    assert admins not in win32net.NetUserGetLocalGroups(None, name, win32netcon.LG_INCLUDE_INDIRECT)
    provider = IsolatedHyperVProvider()
    provider._ensure_host()
    attempt = _attempt(tmp_path)
    media = tmp_path / "bootstrap.vhdx"
    token = None
    try:
        provider.create_bootstrap_media(attempt.root, media)
        raw = provider._run_ps(
            "$ErrorActionPreference='Stop'; $disk=Mount-VHD -Path " + _ps_quote(str(media))
            + " -PassThru | Get-Disk; $v=$disk | Get-Partition | Get-Volume "
            "| Where-Object FileSystemLabel -eq 'ARGUS_BOOTSTRAP'; "
            "if (@($v).Count -ne 1) { throw 'ambiguous volume' }; "
            "[string]$v.DriveLetter+','+[string]$disk.Number", 30,
        )
        letter, disk_number = raw.strip().split(",")
        root = Path(letter + ":\\")
        validate_windows_bootstrap_acl(root)
        assert (root / "bootstrap.token").read_text().strip() == attempt.bootstrap_token
        token = win32security.LogonUser(name, ".", password,
                                       win32con.LOGON32_LOGON_INTERACTIVE, win32con.LOGON32_PROVIDER_DEFAULT)
        win32security.ImpersonateLoggedOnUser(token)
        try:
            for member in ("bootstrap.json", "bootstrap.token", "tls-cert.pem", "tls-key.pem"):
                with pytest.raises(PermissionError):
                    (root / member).read_bytes()
                with pytest.raises(PermissionError):
                    (root / member).write_bytes(b"unauthorized mutation")
            for device in ("\\\\.\\" + letter + ":", "\\\\.\\PhysicalDrive" + disk_number):
                with pytest.raises(Exception) as error:
                    opened = win32file.CreateFile(device, win32con.GENERIC_READ,
                                                 win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
                                                 None, win32con.OPEN_EXISTING, 0, None)
                    opened.Close()
                assert error.value.winerror == 5
        finally:
            win32security.RevertToSelf()
    finally:
        if token is not None:
            token.Close()
        provider.destroy_bootstrap_media(media)
        attempt.destroy()
