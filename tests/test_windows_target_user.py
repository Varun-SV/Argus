"""Windows account API contract tests; native console acceptance is separate."""

import json
import sys
from types import SimpleNamespace

import pytest

from argus.capsule.base import CapsuleError
from argus.capsule.windows_target import initialize_target_user


def test_target_password_stays_in_sam_and_lsa_and_localized_groups_are_used(tmp_path, monkeypatch):
    calls = []
    groups = ["Administrateurs"]
    class MissingUser(Exception):
        winerror = 2221
    def missing(*args):
        raise MissingUser()
    def remove(server, group, members):
        assert members == ["CAPSULE\\argus-target"]
        groups.remove(group)
    net = SimpleNamespace(error=MissingUser, NetUserGetInfo=missing,
        NetUserAdd=lambda server, level, user: calls.append(("SAM", user["password"])),
        NetUserGetLocalGroups=lambda *args: list(groups),
        NetLocalGroupAddMembers=lambda *args: calls.append(("users", args)),
        NetLocalGroupDelMembers=remove)
    class Key:
        def __enter__(self): return self
        def __exit__(self, *args): pass
    policy = SimpleNamespace(Close=lambda: calls.append(("policy_closed",)))
    security = SimpleNamespace(WinBuiltinUsersSid=1, WinBuiltinAdministratorsSid=2,
        POLICY_CREATE_SECRET=4, LookupAccountName=lambda *args: ("target-sid", "", 1),
        CreateWellKnownSid=lambda kind, _: kind,
        LookupAccountSid=lambda _, sid: ("Utilisateurs" if sid == 1 else "Administrateurs", "", 1),
        LsaOpenPolicy=lambda *args: policy,
        LsaStorePrivateData=lambda p, name, value: calls.append(("LSA", name, value)))
    registry = SimpleNamespace(HKEY_LOCAL_MACHINE=1, KEY_SET_VALUE=2, REG_SZ=3,
        OpenKey=lambda *args: Key(),
        SetValueEx=lambda _, name, reserved, kind, value: calls.append(("registry", name, value)),
        DeleteValue=lambda _, name: calls.append(("delete", name)))
    monkeypatch.setitem(sys.modules, "win32net", net)
    monkeypatch.setitem(sys.modules, "win32netcon", SimpleNamespace(USER_PRIV_USER=1, UF_SCRIPT=1, UF_DONT_EXPIRE_PASSWD=2))
    monkeypatch.setitem(sys.modules, "win32security", security)
    monkeypatch.setitem(sys.modules, "winreg", registry)
    monkeypatch.setenv("COMPUTERNAME", "CAPSULE")
    capsule = "cap-" + "a" * 32
    initialize_target_user(capsule, tmp_path)
    password = next(call[1] for call in calls if call[0] == "SAM")
    assert len(password) >= 48
    assert ("LSA", "DefaultPassword", password) in calls
    assert all(password not in str(call) for call in calls if call[0] == "registry")
    assert password not in (tmp_path / "target-user.json").read_text()
    assert groups == []
    first = list(calls)
    initialize_target_user(capsule, tmp_path)
    assert calls == first
    with pytest.raises(CapsuleError, match="ownership is corrupt"):
        initialize_target_user("cap-" + "b" * 32, tmp_path)


def test_target_marker_corruption_is_not_reinitialized(tmp_path):
    (tmp_path / "target-user.json").write_text(json.dumps({"capsule_id": "foreign"}))
    # Marker validation occurs before any account operation, with API imports
    # supplied by the preceding test only during its monkeypatch lifetime.
    import contextlib
    from unittest.mock import patch
    with contextlib.ExitStack() as stack:
        for module in ("win32net", "win32netcon", "win32security", "winreg"):
            stack.enter_context(patch.dict(sys.modules, {module: SimpleNamespace()}))
        with pytest.raises(CapsuleError, match="ownership is corrupt"):
            initialize_target_user("cap-" + "a" * 32, tmp_path)


@pytest.mark.parametrize(
    ("privilege", "groups", "non_admin"),
    [(1, ["Utilisateurs"], True), (1, ["ADMINISTRATEURS"], False), (2, [], False)],
)
def test_health_uses_native_account_policy_with_indirect_groups(
    monkeypatch, privilege, groups, non_admin
):
    from argus.capsule import secure_guest_agent as agent

    class ApiError(Exception):
        winerror = 5

    calls = []
    def user_info(server, name, level):
        calls.append(("user", server, name, level))
        return {"priv": privilege}
    def local_groups(server, name, flags):
        calls.append(("groups", server, name, flags))
        return groups
    net = SimpleNamespace(error=ApiError, NetUserGetInfo=user_info,
                          NetUserGetLocalGroups=local_groups)
    security = SimpleNamespace(
        error=ApiError, WinBuiltinAdministratorsSid=2,
        CreateWellKnownSid=lambda kind, domain: kind,
        LookupAccountSid=lambda server, sid: ("Administrateurs", "BUILTIN", 4),
    )
    monkeypatch.setitem(sys.modules, "win32net", net)
    monkeypatch.setitem(sys.modules, "win32netcon",
                        SimpleNamespace(USER_PRIV_USER=1, LG_INCLUDE_INDIRECT=1))
    monkeypatch.setitem(sys.modules, "win32security", security)
    monkeypatch.setattr(agent.platform, "system", lambda: "Windows")
    def no_subprocess(*args, **kwargs):
        pytest.fail("Windows health must not spawn a shell")
    monkeypatch.setattr(agent.subprocess, "run", no_subprocess)
    assert agent._target_user_policy() == {
        "target_user": "argus-target", "target_user_present": True,
        "target_user_non_admin": non_admin, "target_user_locked": False,
    }
    assert calls == [
        ("user", None, "argus-target", 1),
        ("groups", None, "argus-target", 1),
    ]


@pytest.mark.parametrize("error_code", [2221, 5])
def test_health_missing_account_and_api_failure_do_not_report_readiness(
    monkeypatch, error_code
):
    from argus.capsule import secure_guest_agent as agent

    class ApiError(Exception):
        winerror = error_code
    def unavailable(*args):
        raise ApiError()
    monkeypatch.setitem(sys.modules, "win32net",
                        SimpleNamespace(error=ApiError, NetUserGetInfo=unavailable))
    monkeypatch.setitem(sys.modules, "win32netcon", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "win32security", SimpleNamespace())
    monkeypatch.setattr(agent.platform, "system", lambda: "Windows")
    result = agent._target_user_policy()
    if error_code == 2221:
        assert result["target_user_present"] is False
        assert result["target_user_non_admin"] is False
    else:
        assert result == {}


@pytest.mark.parametrize("failure_stage", ["groups", "sid"])
def test_health_group_or_sid_lookup_failure_is_not_positive_readiness(
    monkeypatch, failure_stage
):
    from argus.capsule import secure_guest_agent as agent

    class ApiError(Exception):
        winerror = 5
    def unavailable(*args):
        raise ApiError()
    net = SimpleNamespace(
        error=ApiError, NetUserGetInfo=lambda *args: {"priv": 1},
        NetUserGetLocalGroups=unavailable if failure_stage == "groups"
        else lambda *args: ["Utilisateurs"],
    )
    security = SimpleNamespace(
        error=ApiError, WinBuiltinAdministratorsSid=2,
        CreateWellKnownSid=lambda kind, domain: kind,
        LookupAccountSid=unavailable if failure_stage == "sid"
        else lambda *args: ("Administrateurs", "BUILTIN", 4),
    )
    monkeypatch.setitem(sys.modules, "win32net", net)
    monkeypatch.setitem(sys.modules, "win32netcon",
                        SimpleNamespace(USER_PRIV_USER=1, LG_INCLUDE_INDIRECT=1))
    monkeypatch.setitem(sys.modules, "win32security", security)
    monkeypatch.setattr(agent.platform, "system", lambda: "Windows")
    assert agent._target_user_policy() == {}
