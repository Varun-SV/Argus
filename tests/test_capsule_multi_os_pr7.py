from __future__ import annotations

import ipaddress
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from argus.capsule.base import CapsuleError, CapsuleRequest, CapsuleSettings
from argus.capsule.files import guest_path_key, normalize_guest_relative_path
from argus.capsule.libvirt import LibvirtProvider
from argus.execution.secure_capsule import SecureCapsuleExecutionEnvironment


def _linux_settings(tmp_path: Path, **overrides) -> CapsuleSettings:
    image = tmp_path / "linux-golden.qcow2"
    image.write_bytes(b"qcow2-test-image")
    ca = tmp_path / "guest-ca.pem"
    ca.write_text("test-ca", encoding="utf-8")
    values = dict(
        provider="libvirt",
        guest_os="linux",
        image=str(image),
        vm_root=str(tmp_path / "sessions"),
        guest_token="bootstrap-secret",
        guest_transport="https",
        guest_ca_cert=str(ca),
        network_mode="host_only",
        allow_dhcp=True,
        boot_timeout_seconds=2,
    )
    values.update(overrides)
    return CapsuleSettings(**values)


def test_auto_provider_selection_is_host_specific():
    linux = SecureCapsuleExecutionEnvironment._make_secure_provider("auto", "linux")
    windows = SecureCapsuleExecutionEnvironment._make_secure_provider("auto", "win32")

    assert linux.provider_name == "libvirt"
    assert windows.provider_name == "hyperv"

    with pytest.raises(Exception, match="Windows.*Linux"):
        SecureCapsuleExecutionEnvironment._make_secure_provider("auto", "darwin")


def test_libvirt_provider_advertises_fail_closed_capabilities():
    caps = LibvirtProvider().capabilities()
    assert caps.provider == "libvirt"
    assert caps.host_platforms == ("linux",)
    assert caps.guest_os == ("linux",)
    assert caps.secure_transport is True
    assert caps.network_isolation is True
    assert caps.explicit_transfers is True
    assert caps.failure_retention is True
    assert caps.egress_allowlist is False


def test_portable_paths_remain_cross_platform_safe_but_linux_keys_are_case_sensitive():
    assert guest_path_key("logs/Result.txt") == guest_path_key("logs/result.txt")
    assert guest_path_key("logs/Result.txt", guest_os="linux") != guest_path_key(
        "logs/result.txt", guest_os="linux"
    )
    with pytest.raises(CapsuleError, match="reserved Windows name"):
        normalize_guest_relative_path("con")
    assert normalize_guest_relative_path("con", guest_os="linux") == "con"


def test_libvirt_rejects_unsupported_egress_before_any_command(tmp_path):
    calls = []

    def runner(args, timeout):
        calls.append(tuple(args))
        return ""

    provider = LibvirtProvider(runner=runner)
    settings = _linux_settings(
        tmp_path,
        network_mode="allowlist",
        egress_allowlist=("10.20.30.0/24",),
    )
    with pytest.raises(CapsuleError, match="host_only only"):
        provider.create(CapsuleRequest("reject-egress", "cli", settings))
    assert calls == []


def test_libvirt_requires_system_uri_and_linux_guest(tmp_path):
    provider = LibvirtProvider(runner=lambda args, timeout: "")

    with pytest.raises(CapsuleError, match="qemu:///system"):
        provider.create(
            CapsuleRequest(
                "bad-uri",
                "cli",
                _linux_settings(tmp_path, libvirt_uri="qemu:///session"),
            )
        )

    with pytest.raises(CapsuleError, match="guest_os=linux"):
        provider.create(
            CapsuleRequest(
                "bad-guest",
                "cli",
                _linux_settings(tmp_path, guest_os="windows"),
            )
        )


def test_libvirt_builds_isolated_policy_before_start_and_uses_overlay(tmp_path):
    calls: list[tuple[str, ...]] = []
    state: dict[str, str] = {}

    def runner(args, timeout):
        argv = tuple(str(item) for item in args)
        calls.append(argv)
        if argv[0].endswith("qemu-img") or argv[0] == "qemu-img":
            if "info" in argv:
                return json.dumps({"format": "qcow2"})
            if "create" in argv:
                Path(argv[-1]).write_bytes(b"overlay")
                return ""

        command = argv[3] if len(argv) > 3 and argv[1:3] == ("-c", "qemu:///system") else ""
        if command == "net-create":
            root = ET.parse(argv[4]).getroot()
            state["network"] = root.findtext("name") or ""
            host = root.find("./ip/dhcp/host")
            assert host is not None
            state["guest_ip"] = host.attrib["ip"]
            return "Network created"
        if command == "nwfilter-define":
            root = ET.parse(argv[4]).getroot()
            state["filter"] = root.attrib["name"]
            return "Network filter defined"
        if command == "define":
            root = ET.parse(argv[4]).getroot()
            state["vm"] = root.findtext("name") or ""
            return "Domain defined"
        if command == "start":
            return "Domain started"
        if command == "domifaddr":
            return (
                " Name MAC address Protocol Address\n"
                " vnet0 52:54:00:aa:bb:cc ipv4 " + state["guest_ip"] + "/24\n"
            )
        if command == "list":
            return state.get("vm", "")
        if command == "net-list":
            return state.get("network", "")
        if command == "nwfilter-list":
            return " UUID Name\n --------------------------------\n deadbeef " + state.get("filter", "")
        if command == "domstate":
            return "running"
        return ""

    provider = LibvirtProvider(runner=runner, sleeper=lambda _seconds: None)
    settings = _linux_settings(tmp_path, libvirt_network_cidr="10.250.77.0/24")
    handle = provider.create(CapsuleRequest("linux123", "cli", settings))

    assert handle.provider == "libvirt"
    assert handle.guest_os == "linux"
    assert handle.address == "10.250.77.2"
    assert handle.transport == "https"

    root = Path(handle.root_dir)
    network = ET.parse(root / "network.xml").getroot()
    assert network.find("forward") is None
    assert network.find("dns").attrib["enable"] == "no"
    assert network.find("ip").attrib["address"] == "10.250.77.1"

    nwfilter = ET.parse(root / "nwfilter.xml").getroot()
    assert nwfilter.attrib["chain"] == "root"
    rules = nwfilter.findall("rule")
    assert any(
        rule.attrib.get("action") == "drop" and rule.attrib.get("direction") == "inout"
        for rule in rules
    )
    control_in = next(
        rule
        for rule in rules
        if rule.find("tcp") is not None and rule.attrib["direction"] == "in"
    )
    tcp_in = control_in.find("tcp")
    assert tcp_in is not None
    assert tcp_in.attrib["srcipaddr"] == "10.250.77.1"
    assert tcp_in.attrib["dstipaddr"] == "10.250.77.2"
    assert tcp_in.attrib["dstportstart"] == "8765"
    assert tcp_in.attrib["state"] == "NEW,ESTABLISHED"

    domain = ET.parse(root / "domain.xml").getroot()
    assert domain.attrib["type"] == "kvm"
    assert domain.find(".//graphics") is None
    assert domain.find(".//interface/source").attrib["network"] == state["network"]
    assert domain.find(".//filterref").attrib["filter"] == state["filter"]
    assert domain.find(".//disk/source").attrib["file"].endswith("session.qcow2")

    qemu_create = next(
        i for i, argv in enumerate(calls) if argv[0] == "qemu-img" and "create" in argv
    )
    filter_define = next(
        i for i, argv in enumerate(calls) if len(argv) > 3 and argv[3] == "nwfilter-define"
    )
    network_create = next(
        i for i, argv in enumerate(calls) if len(argv) > 3 and argv[3] == "net-create"
    )
    domain_define = next(
        i for i, argv in enumerate(calls) if len(argv) > 3 and argv[3] == "define"
    )
    domain_start = next(
        i for i, argv in enumerate(calls) if len(argv) > 3 and argv[3] == "start"
    )
    assert qemu_create < filter_define < network_create < domain_define < domain_start

    provider.destroy(handle)
    assert not root.exists()
    nwfilter_list = next(
        argv for argv in calls if len(argv) > 3 and argv[3] == "nwfilter-list"
    )
    assert "--name" not in nwfilter_list
    assert any(
        len(argv) > 4 and argv[3] == "nwfilter-undefine" and argv[4] == state["filter"]
        for argv in calls
    )


def test_libvirt_nwfilter_list_parser_uses_regular_table_output():
    raw = (
        " UUID                                   Name\n"
        "--------------------------------------------------------------------\n"
        " 1234abcd                               argus-deadbeef-control\n"
    )
    assert LibvirtProvider._nwfilter_names(raw) == {"argus-deadbeef-control"}


def test_libvirt_default_network_is_deterministic_private_24():
    first = LibvirtProvider._default_network("same-session")
    second = LibvirtProvider._default_network("same-session")
    assert first == second
    assert first.is_private
    assert first.prefixlen == 24
    assert first.subnet_of(ipaddress.ip_network("10.240.0.0/12"))
