"""First-Capsule Windows target account and OS-protected console logon."""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets

from argus.capsule.base import CapsuleError
from argus.capsule.control import _atomic_json, _file_lock, validate_capsule_id


def initialize_target_user(capsule_id: str, state_root: Path) -> None:
    """Create the final non-admin user only on the mutable Capsule.

    The random OS login password goes directly to SAM and the LSA secret store.
    It is never part of Argus control authentication, arguments, answer media,
    logs or our non-secret initialization marker.
    """
    capsule_id = validate_capsule_id(capsule_id)
    with _file_lock(state_root / "target-user.lock"):
        _initialize_target_user_locked(capsule_id, state_root)


def _initialize_target_user_locked(capsule_id: str, state_root: Path) -> None:
    import win32net
    import win32netcon
    import win32security
    import winreg

    marker = state_root / "target-user.json"
    if marker.exists():
        try:
            raw = json.loads(marker.read_text(encoding="utf-8"))
            if raw != {"capsule_id": capsule_id, "policy": "argus-target-user-v1"}:
                raise ValueError()
        except (OSError, ValueError):
            raise CapsuleError("Windows target-user ownership is corrupt") from None
        return

    name = "argus-target"
    password = secrets.token_urlsafe(48)
    policy = None
    try:
        try:
            win32net.NetUserGetInfo(None, name, 1)
        except win32net.error as exc:
            if exc.winerror != 2221:  # NERR_UserNotFound
                raise
            win32net.NetUserAdd(None, 1, {
                "name": name, "password": password, "priv": win32netcon.USER_PRIV_USER,
                "home_dir": "", "comment": "Argus application target",
                "flags": win32netcon.UF_SCRIPT | win32netcon.UF_DONT_EXPIRE_PASSWD,
                "script_path": "",
            })
        else:
            # A interrupted initialization never reuses an unknown credential.
            win32net.NetUserSetInfo(None, name, 1003, {"password": password})
        user_sid, _, _ = win32security.LookupAccountName(None, name)
        users_sid = win32security.CreateWellKnownSid(win32security.WinBuiltinUsersSid, None)
        admins_sid = win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid, None)
        users, _, _ = win32security.LookupAccountSid(None, users_sid)
        admins, _, _ = win32security.LookupAccountSid(None, admins_sid)
        memberships = win32net.NetUserGetLocalGroups(None, name, 0)
        if users not in memberships:
            win32net.NetLocalGroupAddMembers(None, users, 0, [{"sid": user_sid}])
        if admins in memberships:
            win32net.NetLocalGroupDelMembers(None, admins, [os.environ["COMPUTERNAME"] + "\\" + name])
        if admins in win32net.NetUserGetLocalGroups(None, name, 0):
            raise CapsuleError("Windows target user must not be administrator")
        policy = win32security.LsaOpenPolicy(None, win32security.POLICY_CREATE_SECRET)
        win32security.LsaStorePrivateData(policy, "DefaultPassword", password)
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon",
                            0, winreg.KEY_SET_VALUE) as key:
            for field, value in (
                ("DefaultUserName", name), ("DefaultDomainName", os.environ["COMPUTERNAME"]),
                ("AutoAdminLogon", "1"),
            ):
                winreg.SetValueEx(key, field, 0, winreg.REG_SZ, value)
            try:
                winreg.DeleteValue(key, "DefaultPassword")
            except FileNotFoundError:
                pass
        _atomic_json(marker, {"capsule_id": capsule_id, "policy": "argus-target-user-v1"})
    except Exception:
        raise CapsuleError("Windows non-admin target-user initialization failed") from None
    finally:
        password = ""
        if policy is not None:
            policy.Close()
