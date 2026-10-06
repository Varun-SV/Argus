"""Ownership rollback, persistent reconnect networks and desktop readiness."""

from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

from argus.capsule.base import CapsuleCleanupError, CapsuleError, CapsuleRequest, CapsuleSettings
from argus.capsule.hyperv import _ps_quote
from argus.capsule.hyperv_isolated import IsolatedHyperVProvider
from argus.capsule.libvirt import LibvirtProvider
from argus.capsule.secure_client import SecureGuestAgentClient
from argus.capsule.guest import CapsuleGuestError


def _settings(image, root, **kwargs):
    return CapsuleSettings(image=str(image), vm_root=str(root),
                           environment_id="env-sha256-" + "a" * 64,
                           base_image_sha256="b" * 64,
                           guest_runtime_identity="runtime-sha256-" + "c" * 64, **kwargs)


@pytest.mark.parametrize("desktop", [False, True])
def test_readiness_waits_for_desktop_only_when_required(monkeypatch, desktop):
    import argus.capsule.guest as guest_module

    elapsed = [0.0]
    monkeypatch.setattr(guest_module, "time", SimpleNamespace(
        monotonic=lambda: elapsed[0], sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)))
    client = SecureGuestAgentClient("http://127.0.0.1:8765", "test-token", allow_insecure_http=True,
                                    require_target_desktop=desktop)
    responses = iter([{"ok": True, "target_desktop_ready": False},
                      {"ok": True, "target_desktop_ready": True}])
    calls = []
    def health():
        calls.append(True)
        return next(responses)
    monkeypatch.setattr(client, "health", health)
    client.wait_until_ready(2)
    assert len(calls) == (2 if desktop else 1)


@pytest.mark.parametrize("value", [False, None])
def test_missing_desktop_times_out_instead_of_accepting_agent_health(monkeypatch, value):
    import argus.capsule.guest as guest_module

    elapsed = [0.0]
    monkeypatch.setattr(guest_module, "time", SimpleNamespace(
        monotonic=lambda: elapsed[0], sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)))
    client = SecureGuestAgentClient("http://127.0.0.1:8765", "test-token", allow_insecure_http=True,
                                    require_target_desktop=True)
    monkeypatch.setattr(client, "health", lambda: {"ok": True, "target_desktop_ready": value})
    with pytest.raises(CapsuleGuestError, match="target desktop not ready"):
        client.wait_until_ready(1)


def test_hyperv_name_collision_never_enters_allocation_or_rollback(tmp_path, monkeypatch):
    image = tmp_path / "base.vhdx"
    image.write_bytes(b"base")
    calls = []
    provider = IsolatedHyperVProvider(runner=lambda script, timeout: calls.append(script) or "present")
    monkeypatch.setattr(provider, "_validate_switch", lambda name: None)
    settings = _settings(image, tmp_path / "vm", switch_name="internal")
    with pytest.raises(CapsuleError, match="already exists"):
        provider.create_stopped(CapsuleRequest("session", "cli", settings, capsule_id="cap-" + "a" * 32))
    assert len(calls) == 1
    assert "Get-VM -ErrorAction Stop" in calls[0]
    assert not (settings.resolved_vm_root / ("cap-" + "a" * 32)).exists()


@pytest.mark.skipif(os.name != "nt" or not shutil.which("powershell.exe"), reason="Windows PowerShell branch execution")
@pytest.mark.parametrize("ownership", ["absent", "matching", "foreign-path", "foreign-disk", "denied", "incomplete"])
def test_hyperv_rollback_checks_real_powershell_ownership_branches(tmp_path, monkeypatch, ownership):
    image = tmp_path / "base.vhdx"
    image.write_bytes(b"base")
    capsule_id = "cap-" + "a" * 32
    settings = _settings(image, tmp_path / "vm", switch_name="internal")
    root = settings.resolved_vm_root / capsule_id
    name = "Argus-" + "a" * 20
    vm_path = root / "vm" / name
    disk_path = root / "session.vhdx"
    deleted = []

    def runner(script, timeout):
        if script.startswith("New-VHD"):
            disk_path.write_bytes(b"mutable")
        if script.startswith("New-VM"):
            raise CapsuleError("allocation response failed")
        if "VM configuration ownership mismatch" in script:
            # Shadow every native VM command with in-memory objects, then run
            # the exact production ownership/deletion branches in PowerShell.
            actual_path = tmp_path / "foreign" if ownership == "foreign-path" else vm_path
            actual_disk = tmp_path / "foreign.vhdx" if ownership == "foreign-disk" else disk_path
            shim = (
                "$global:present=" + ("$false" if ownership == "absent" else "$true") + ";"
                "$global:deleted=0; $global:denied=" + ("$true" if ownership == "denied" else "$false") + ";"
                "$global:incomplete=" + ("$true" if ownership == "incomplete" else "$false") + ";"
                "$global:v=[pscustomobject]@{Name=" + _ps_quote(name) + ";Path=" + _ps_quote(str(actual_path))
                + ";Id=[guid]::Empty;State='Off'};"
                "function Get-VM { if($global:denied){throw 'denied'}; if($global:present){$global:v} };"
                "function Get-VMHardDiskDrive { [pscustomobject]@{Path=" + _ps_quote(str(actual_disk)) + "} };"
                "function Stop-VM {}; function Remove-VM { $global:deleted++; if(!$global:incomplete){$global:present=$false} };"
                "$failed=$false; try { " + script + " } catch { $failed=$true };"
                "Write-Output ('__deleted='+$global:deleted); if($failed){exit 1}"
            )
            result = subprocess.run([shutil.which("powershell.exe"), "-NoProfile", "-NonInteractive", "-Command", shim],
                                    text=True, capture_output=True, timeout=15)
            lines = result.stdout.splitlines()
            deleted.append(int(lines[-1].split("=")[1]))
            if result.returncode:
                raise CapsuleError("native ownership or removal could not be confirmed")
            return lines[0]
        return ""

    provider = IsolatedHyperVProvider(runner=runner)
    monkeypatch.setattr(provider, "_validate_switch", lambda switch: None)
    uncertain = ownership not in {"absent", "matching"}
    with pytest.raises(CapsuleCleanupError if uncertain else CapsuleError):
        provider.create_stopped(CapsuleRequest("session", "cli", settings, capsule_id=capsule_id))
    assert root.exists() == uncertain
    assert deleted == [1 if ownership in {"matching", "incomplete"} else 0]


@pytest.mark.parametrize("fault", [None, "network-lost-ack", "domain-failure"])
def test_stable_libvirt_network_survives_restart_and_is_removed_with_owned_vm(tmp_path, fault):
    image = tmp_path / "base.qcow2"
    image.write_bytes(b"base")
    state = {"network": "", "persistent": False, "active": False, "domain": "", "filter": "", "running": False}
    calls = []

    def runner(argv, timeout):
        calls.append(tuple(argv))
        if argv[0] == "ip":
            return "[]"
        if argv[0] == "qemu-img":
            if argv[1] == "info":
                return json.dumps({"format": "qcow2"})
            Path(argv[-1]).write_bytes(b"mutable")
            return ""
        command = argv[3]
        if command in {"net-define", "net-create"}:
            state["network_xml"] = Path(argv[4]).read_text()
            xml = ET.fromstring(state["network_xml"])
            state["network"] = xml.findtext("name")
            state["ip"] = xml.find("./ip/dhcp/host").attrib["ip"]
            state["persistent"] = command == "net-define"
            state["active"] = command == "net-create"
            if fault == "network-lost-ack":
                raise CapsuleError("network acknowledgement lost")
        elif command == "net-start":
            state["active"] = True
        elif command == "net-list":
            return state["network"] if "--all" in argv or state["active"] else ""
        elif command == "net-dumpxml":
            return state["network_xml"]
        elif command == "nwfilter-define":
            state["filter"] = ET.parse(argv[4]).getroot().attrib["name"]
        elif command == "nwfilter-list":
            return "UUID Name\n---\n deadbeef " + state["filter"]
        elif command == "define":
            if fault == "domain-failure":
                raise CapsuleError("domain allocation failed")
            state["domain_xml"] = Path(argv[4]).read_text()
            state["domain"] = ET.fromstring(state["domain_xml"]).findtext("name")
        elif command == "list":
            return state["domain"] if "--all" in argv or state["running"] else ""
        elif command == "domuuid":
            return "12345678-1234-5678-1234-567812345678"
        elif command == "dumpxml":
            return state["domain_xml"]
        elif command == "domstate":
            return "running" if state["running"] else "shut off"
        elif command == "start":
            state["running"] = True
        elif command == "domifaddr":
            return "vnet0 52:54:00:aa:bb:cc ipv4 " + state["ip"] + "/24"
        elif command == "destroy":
            state["running"] = False
        elif command == "undefine":
            state["domain"] = ""
        elif command == "net-destroy":
            state["active"] = False
            if not state["persistent"]:
                state["network"] = ""
        elif command == "net-undefine":
            state["network"] = ""
            state["persistent"] = False
        elif command == "nwfilter-undefine":
            state["filter"] = ""
        return ""

    provider = LibvirtProvider(runner=runner, sleeper=lambda seconds: None)
    settings = _settings(image, tmp_path / "vm", provider="libvirt", guest_os="linux")
    request = CapsuleRequest("session", "cli", settings, capsule_id="cap-" + "a" * 32)
    if fault:
        with pytest.raises(CapsuleError):
            provider.create_stopped(request)
    else:
        handle = provider.create_stopped(request)
        assert state["persistent"] is True
        # Daemon/host restart removes active transient networks, retaining definitions.
        state["active"] = False
        if not state["persistent"]:
            state["network"] = ""
        restarted = provider.start_existing(handle, replace(request, session_id="new", control_generation=2))
        assert restarted.provider_resource_identity == handle.provider_resource_identity
        assert restarted.mutable_disk_identity == handle.mutable_disk_identity
        assert state["active"] and state["running"]
        provider.quarantine(restarted)
        failure = provider.retain_failure(restarted, "assertion")
        assert state["persistent"] and state["network"]
        assert failure.mutable_disk_identity == handle.mutable_disk_identity
        if os.name == "nt":
            # This Linux-provider double runs on Windows CI; clear the Windows
            # read-only file attribute before simulating POSIX unlink semantics.
            (Path(failure.root_dir) / "failure-capsule.json").chmod(0o600)
        provider.destroy(restarted)
    assert state["network"] == state["domain"] == state["filter"] == ""
    assert any(argv[3] == "net-undefine" for argv in calls if len(argv) > 3)


def test_session_storage_removal_retries_transient_windows_locks(tmp_path, monkeypatch):
    from argus.capsule import hyperv
    calls = []

    def flaky(path):
        calls.append(path)
        if len(calls) < 3:
            raise PermissionError("in use by another process")

    monkeypatch.setattr(hyperv.shutil, "rmtree", flaky)
    monkeypatch.setattr(hyperv.time, "sleep", lambda _: None)
    hyperv._remove_tree(tmp_path / "session")
    assert len(calls) == 3


def test_session_storage_removal_reports_a_lasting_lock(tmp_path, monkeypatch):
    from argus.capsule import hyperv

    def locked(path):
        raise PermissionError("in use by another process")

    monkeypatch.setattr(hyperv.shutil, "rmtree", locked)
    monkeypatch.setattr(hyperv.time, "sleep", lambda _: None)
    with pytest.raises(PermissionError):
        hyperv._remove_tree(tmp_path / "session", attempts=3)
