from __future__ import annotations

import json
import threading
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from argus.capsule.base import CapsuleError, CapsuleRequest, CapsuleSettings
from argus.capsule.libvirt import LibvirtProvider


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
    )
    values.update(overrides)
    return CapsuleSettings(**values)


def _command(argv: tuple[str, ...]) -> str:
    if len(argv) > 3 and argv[1:3] == ("-c", "qemu:///system"):
        return argv[3]
    return ""


def test_libvirt_rejects_path_escaping_session_id_before_storage_or_commands(tmp_path):
    calls: list[tuple[str, ...]] = []

    def runner(args, timeout):
        calls.append(tuple(str(item) for item in args))
        return ""

    provider = LibvirtProvider(runner=runner)
    settings = _settings(tmp_path)

    for bad in ("../escape", "/absolute", "nested/session", "session\\child", " spaced "):
        with pytest.raises(CapsuleError, match="session id|safe characters|canonical"):
            provider.create(CapsuleRequest(bad, "cli", settings))

    assert calls == []
    assert not (tmp_path / "sessions").exists()
    assert not (tmp_path / "escape").exists()


def test_libvirt_arch_pin_must_match_actual_host(monkeypatch):
    monkeypatch.setattr("argus.capsule.libvirt.platform.machine", lambda: "x86_64")
    assert LibvirtProvider._host_arch("") == "x86_64"
    assert LibvirtProvider._host_arch("amd64") == "x86_64"
    with pytest.raises(CapsuleError, match="require guest architecture to match the host"):
        LibvirtProvider._host_arch("aarch64")

    monkeypatch.setattr("argus.capsule.libvirt.platform.machine", lambda: "arm64")
    assert LibvirtProvider._host_arch("") == "aarch64"
    assert LibvirtProvider._host_arch("aarch64") == "aarch64"
    with pytest.raises(CapsuleError, match="require guest architecture to match the host"):
        LibvirtProvider._host_arch("x86_64")


def test_concurrent_single_subnet_allocation_is_serialized_through_net_create(tmp_path):
    state_lock = threading.Lock()
    networks: dict[str, str] = {}
    domains: set[str] = set()
    filters: set[str] = set()
    net_create_calls: list[str] = []

    def runner(args, timeout):
        argv = tuple(str(item) for item in args)
        if argv[0] == "qemu-img":
            if "info" in argv:
                return json.dumps({"format": "qcow2"})
            if "create" in argv:
                Path(argv[-1]).write_bytes(b"overlay")
                return ""

        command = _command(argv)
        with state_lock:
            if command == "list":
                return "\n".join(sorted(domains))
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
                return "Network filter defined"
            if command == "net-create":
                xml = Path(argv[4]).read_text(encoding="utf-8")
                root = ET.fromstring(xml)
                name = root.findtext("name") or ""
                networks[name] = xml
                net_create_calls.append(name)
                return "Network created"
            if command == "define":
                root = ET.parse(argv[4]).getroot()
                domains.add(root.findtext("name") or "")
                return "Domain defined"
            if command == "start":
                return "Domain started"
            if command == "domifaddr":
                return "vnet0 52:54:00:aa:bb:cc ipv4 10.252.1.2/24"
        return ""

    settings_a = _settings(
        tmp_path / "a",
        vm_root=str(tmp_path / "sessions-a"),
        libvirt_network_cidr="10.252.1.0/24",
    )
    settings_b = _settings(
        tmp_path / "b",
        vm_root=str(tmp_path / "sessions-b"),
        libvirt_network_cidr="10.252.1.0/24",
    )
    provider_a = LibvirtProvider(runner=runner, sleeper=lambda _seconds: None)
    provider_b = LibvirtProvider(runner=runner, sleeper=lambda _seconds: None)

    successes = []
    failures = []

    def create(provider, request):
        try:
            successes.append(provider.create(request))
        except Exception as exc:  # captured for assertions below
            failures.append(exc)

    first = threading.Thread(
        target=create,
        args=(provider_a, CapsuleRequest("concurrent-a", "cli", settings_a)),
    )
    second = threading.Thread(
        target=create,
        args=(provider_b, CapsuleRequest("concurrent-b", "cli", settings_b)),
    )
    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)

    assert not first.is_alive()
    assert not second.is_alive()
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], CapsuleError)
    assert "no free /24" in str(failures[0])
    assert len(net_create_calls) == 1
    assert successes[0].address == "10.252.1.2"

    failed_session = "concurrent-b" if successes[0].session_id == "concurrent-a" else "concurrent-a"
    failed_root = (
        Path(settings_b.vm_root) if failed_session == "concurrent-b" else Path(settings_a.vm_root)
    ) / failed_session
    assert not failed_root.exists()
