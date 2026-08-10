from __future__ import annotations

import base64
import hashlib
import ipaddress
import os
import stat
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from argus.capsule.base import (
    CapsuleError,
    CapsuleHandle,
    CapsuleProvider,
    CapsuleProviderCapabilities,
    CapsuleRequest,
    CapsuleSettings,
)
from argus.capsule.guest_agent import GuestAgentState
from argus.capsule.libvirt import LibvirtProvider
from argus.execution.base import ExecutionEnvironmentError
from argus.execution.secure_capsule import SecureCapsuleExecutionEnvironment


def _command(argv: tuple[str, ...]) -> str:
    if len(argv) > 3 and argv[1:3] == ("-c", "qemu:///system"):
        return argv[3]
    return ""


def _settings(tmp_path: Path, **overrides) -> CapsuleSettings:
    tmp_path.mkdir(parents=True, exist_ok=True)
    image = tmp_path / "golden.qcow2"
    image.write_bytes(b"golden")
    values = dict(
        provider="libvirt",
        guest_os="linux",
        image=str(image),
        vm_root=str(tmp_path / "sessions"),
        guest_token="bootstrap",
        guest_transport="http",
        allow_insecure_http=True,
        network_mode="host_only",
        allow_dhcp=True,
        boot_timeout_seconds=2,
        libvirt_network_cidr="10.253.44.0/24",
    )
    values.update(overrides)
    return CapsuleSettings(**values)


def test_libvirt_allocator_excludes_explicit_host_and_vpn_routes():
    calls: list[tuple[str, ...]] = []

    def runner(args, timeout):
        argv = tuple(str(item) for item in args)
        calls.append(argv)
        if argv[0] == "ip":
            return (
                "default via 192.0.2.1 dev eth0\n"
                "10.253.44.0/24 dev tun0 proto kernel scope link\n"
                "local 192.0.2.10 dev eth0 table local scope host\n"
            )
        return ""

    provider = LibvirtProvider(runner=runner)
    routes = provider._host_route_networks()
    assert ipaddress.ip_network("10.253.44.0/24") in routes
    assert ipaddress.ip_network("192.0.2.10/32") in routes
    assert ipaddress.ip_network("0.0.0.0/0") not in routes

    with pytest.raises(CapsuleError, match="no free /24"):
        provider._allocate_network(
            "qemu:///system", "host-route-collision", "10.253.44.0/24"
        )
    assert any(call[0] == "ip" for call in calls)


@pytest.mark.parametrize("ambiguous_command", ["nwfilter-define", "net-create", "define"])
def test_ambiguous_virsh_create_is_reconciled_and_cleaned(tmp_path, ambiguous_command):
    filters: set[str] = set()
    networks: dict[str, str] = {}
    domains: set[str] = set()

    def runner(args, timeout):
        argv = tuple(str(item) for item in args)
        if argv[0] == "ip":
            return ""
        if argv[0] == "qemu-img":
            if "info" in argv:
                return '{"format":"qcow2"}'
            if "create" in argv:
                Path(argv[-1]).write_bytes(b"overlay")
                return ""

        command = _command(argv)
        if command == "list":
            return "\n".join(sorted(domains)) if "--all" in argv else ""
        if command == "net-list":
            return "\n".join(sorted(networks))
        if command == "net-dumpxml":
            return networks[argv[4]]
        if command == "nwfilter-list":
            rows = "\n".join(f"deadbeef {name}" for name in sorted(filters))
            return "UUID Name\n--------------------------------\n" + rows

        if command == "nwfilter-define":
            root = ET.parse(argv[4]).getroot()
            filters.add(root.attrib["name"])
            if ambiguous_command == command:
                raise CapsuleError("simulated response loss")
            return "Network filter defined"
        if command == "net-create":
            xml = Path(argv[4]).read_text(encoding="utf-8")
            root = ET.fromstring(xml)
            networks[root.findtext("name") or ""] = xml
            if ambiguous_command == command:
                raise CapsuleError("simulated response loss")
            return "Network created"
        if command == "define":
            root = ET.parse(argv[4]).getroot()
            domains.add(root.findtext("name") or "")
            if ambiguous_command == command:
                raise CapsuleError("simulated response loss")
            return "Domain defined"

        if command == "undefine":
            domains.discard(argv[4])
            return "Domain undefined"
        if command == "net-destroy":
            networks.pop(argv[4], None)
            return "Network destroyed"
        if command == "nwfilter-undefine":
            filters.discard(argv[4])
            return "Network filter undefined"
        return ""

    provider = LibvirtProvider(runner=runner)
    settings = _settings(tmp_path)
    session_id = f"ambiguous-{ambiguous_command.replace('-', '')}"
    root = Path(settings.vm_root) / session_id

    with pytest.raises(CapsuleError, match="simulated response loss"):
        provider.create(CapsuleRequest(session_id, "cli", settings))

    assert filters == set()
    assert networks == {}
    assert domains == set()
    assert not root.exists()


def test_failed_ownership_reconciliation_preserves_session_storage(tmp_path):
    filters: set[str] = set()
    networks: dict[str, str] = {}
    probe_should_fail = False

    def runner(args, timeout):
        nonlocal probe_should_fail
        argv = tuple(str(item) for item in args)
        if argv[0] == "ip":
            return ""
        if argv[0] == "qemu-img":
            if "info" in argv:
                return '{"format":"qcow2"}'
            if "create" in argv:
                Path(argv[-1]).write_bytes(b"overlay")
                return ""

        command = _command(argv)
        if command == "list":
            return ""
        if command == "net-list":
            if probe_should_fail and "--all" in argv:
                raise CapsuleError("libvirt unavailable during ownership probe")
            return "\n".join(sorted(networks))
        if command == "net-dumpxml":
            return networks[argv[4]]
        if command == "nwfilter-list":
            rows = "\n".join(f"deadbeef {name}" for name in sorted(filters))
            return "UUID Name\n--------------------------------\n" + rows
        if command == "nwfilter-define":
            root = ET.parse(argv[4]).getroot()
            filters.add(root.attrib["name"])
            return "Network filter defined"
        if command == "net-create":
            xml = Path(argv[4]).read_text(encoding="utf-8")
            root = ET.fromstring(xml)
            networks[root.findtext("name") or ""] = xml
            probe_should_fail = True
            raise CapsuleError("simulated response loss")
        if command == "nwfilter-undefine":
            filters.discard(argv[4])
            return "Network filter undefined"
        return ""

    provider = LibvirtProvider(runner=runner)
    settings = _settings(tmp_path)
    session_id = "ambiguous-probe"
    root = Path(settings.vm_root) / session_id

    with pytest.raises(CapsuleError, match="ambiguous provider ownership"):
        provider.create(CapsuleRequest(session_id, "cli", settings))

    assert root.is_dir()
    assert (root / "session.qcow2").is_file()
    assert networks
    assert filters == set()


class _InjectedProvider(CapsuleProvider):
    provider_name = "extension"

    def __init__(self, host_platforms: tuple[str, ...]):
        self.provider_capabilities = CapsuleProviderCapabilities(
            provider="extension",
            host_platforms=host_platforms,
            guest_os=("linux",),
            secure_transport=True,
            network_isolation=True,
            explicit_transfers=True,
            failure_retention=True,
            egress_allowlist=True,
        )
        self.create_called = False

    def create(self, request: CapsuleRequest):
        self.create_called = True
        raise AssertionError("host-platform validation must happen before create")

    def destroy(self, handle: CapsuleHandle) -> None:
        return None


@pytest.mark.parametrize("advertised", [("linux",), ()])
def test_injected_provider_must_advertise_current_host_platform(monkeypatch, advertised):
    monkeypatch.setattr("argus.execution.secure_capsule.sys.platform", "win32")
    provider = _InjectedProvider(advertised)
    settings = CapsuleSettings(
        provider="extension",
        guest_os="linux",
        guest_token="bootstrap",
        rotate_session_token=True,
    )

    with pytest.raises(ExecutionEnvironmentError, match="current host platform"):
        SecureCapsuleExecutionEnvironment("cli", settings, provider=provider)
    assert provider.create_called is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable bits are not meaningful on Windows")
def test_literal_staged_launch_grants_only_owner_execute_inside_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "argus.capsule.guest_agent.tempfile.gettempdir", lambda: str(tmp_path)
    )
    state = GuestAgentState()
    state.begin_files("exec-stage")

    payload = b"#!/bin/sh\nexit 0\n"
    digest = hashlib.sha256(payload).hexdigest()
    state.stage_begin("bin/run.sh", len(payload), digest)
    state.stage_chunk("bin/run.sh", 0, base64.b64encode(payload).decode("ascii"))
    committed = state.stage_commit("bin/run.sh", len(payload), digest)
    target = Path(committed["guest_path"])
    os.chmod(target, 0o644)
    assert not (stat.S_IMODE(target.stat().st_mode) & stat.S_IXUSR)

    observed_modes: list[int] = []

    class FakeAdapter:
        def set_working_directory(self, _path):
            return None

        def launch_literal(self, launched):
            observed_modes.append(stat.S_IMODE(Path(launched).stat().st_mode))

        def capabilities(self):
            return {"actions": {}}

        def close(self):
            return None

    monkeypatch.setattr(
        "argus.capsule.guest_agent.create_platform_adapter",
        lambda _adapter_type: FakeAdapter(),
    )

    state.start("cli", str(target), "safe", literal_target=True)
    assert observed_modes
    assert observed_modes[0] & stat.S_IXUSR
    assert stat.S_IMODE(target.stat().st_mode) & 0o077 == 0o044


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable bits are not meaningful on Windows")
def test_literal_launch_refuses_to_chmod_outside_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "argus.capsule.guest_agent.tempfile.gettempdir", lambda: str(tmp_path / "guest")
    )
    state = GuestAgentState()
    state.begin_files("exec-boundary")
    outside = tmp_path / "outside.sh"
    outside.write_text("#!/bin/sh\n", encoding="utf-8")
    os.chmod(outside, 0o644)

    with pytest.raises(Exception, match="escapes the Capsule workspace"):
        state.start("cli", str(outside), "safe", literal_target=True)
    assert stat.S_IMODE(outside.stat().st_mode) == 0o644


def test_aarch64_destroy_undefines_managed_nvram(tmp_path):
    calls: list[tuple[str, ...]] = []
    session_id = "arm-nvram"
    vm_name, _network_name, _filter_name, _bridge = LibvirtProvider._resource_names(session_id)

    def runner(args, timeout):
        argv = tuple(str(item) for item in args)
        calls.append(argv)
        command = _command(argv)
        if command == "list":
            return vm_name if "--all" in argv else ""
        if command == "net-list":
            return ""
        if command == "nwfilter-list":
            return ""
        return ""

    root = tmp_path / "arm-session"
    root.mkdir()
    handle = CapsuleHandle(
        session_id=session_id,
        provider="libvirt",
        vm_name=vm_name,
        root_dir=str(root),
        address="10.1.2.2",
        guest_port=8765,
        transport="https",
        guest_os="linux",
        architecture="aarch64",
    )

    LibvirtProvider(runner=runner).destroy(handle)

    undefine = next(call for call in calls if _command(call) == "undefine")
    assert undefine[4] == vm_name
    assert "--nvram" in undefine
    assert not root.exists()
