from __future__ import annotations

import ipaddress
from dataclasses import replace
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
from argus.capsule.libvirt import LibvirtProvider
from argus.execution.base import ExecutionEnvironmentError
from argus.execution.secure_capsule import SecureCapsuleExecutionEnvironment


def _command(args: tuple[str, ...]) -> str:
    if len(args) > 3 and args[1:3] == ("-c", "qemu:///system"):
        return args[3]
    return ""


@pytest.mark.parametrize(
    "cidr",
    [
        "127.0.0.0/24",
        "0.0.0.0/24",
        "169.254.10.0/24",
        "224.0.0.0/24",
        "240.0.0.0/24",
        "100.64.0.0/24",
    ],
)
def test_libvirt_network_pool_rejects_special_use_space(cidr):
    with pytest.raises(CapsuleError, match="RFC1918"):
        LibvirtProvider._network_pool(cidr)


@pytest.mark.parametrize(
    "cidr",
    ["10.0.0.0/8", "10.42.0.0/16", "172.16.32.0/20", "192.168.77.0/24"],
)
def test_libvirt_network_pool_accepts_rfc1918_pools(cidr):
    assert LibvirtProvider._network_pool(cidr) == ipaddress.ip_network(cidr)


def test_configured_pool_skips_subnet_already_used_by_libvirt():
    calls: list[tuple[str, ...]] = []

    def runner(args, timeout):
        argv = tuple(str(item) for item in args)
        calls.append(argv)
        command = _command(argv)
        if command == "net-list" and "--all" in argv:
            return "existing-network"
        if command == "net-dumpxml":
            return """<network><name>existing-network</name><ip address='10.251.44.1' netmask='255.255.255.0'/></network>"""
        return ""

    provider = LibvirtProvider(runner=runner)
    chosen = provider._allocate_network(
        "qemu:///system", "pool-session", "10.251.44.0/23"
    )

    assert chosen.subnet_of(ipaddress.ip_network("10.251.44.0/23"))
    assert not chosen.overlaps(ipaddress.ip_network("10.251.44.0/24"))
    assert any(_command(call) == "net-dumpxml" for call in calls)


def test_single_24_pool_fails_when_subnet_is_already_in_use():
    def runner(args, timeout):
        argv = tuple(str(item) for item in args)
        command = _command(argv)
        if command == "net-list" and "--all" in argv:
            return "existing-network"
        if command == "net-dumpxml":
            return """<network><name>existing-network</name><ip address='10.250.77.1' prefix='24'/></network>"""
        return ""

    provider = LibvirtProvider(runner=runner)
    with pytest.raises(CapsuleError, match="no free /24"):
        provider._allocate_network(
            "qemu:///system", "second-session", "10.250.77.0/24"
        )


def test_default_network_allocator_skips_hash_candidate_when_occupied():
    session_id = "forced-default-collision"
    first = LibvirtProvider._default_network(session_id)

    def runner(args, timeout):
        argv = tuple(str(item) for item in args)
        command = _command(argv)
        if command == "net-list" and "--all" in argv:
            return "existing-network"
        if command == "net-dumpxml":
            gateway = first.network_address + 1
            return (
                "<network><name>existing-network</name>"
                f"<ip address='{gateway}' prefix='24'/></network>"
            )
        return ""

    chosen = LibvirtProvider(runner=runner)._allocate_network(
        "qemu:///system", session_id, ""
    )
    assert chosen != first
    assert not chosen.overlaps(first)
    assert chosen.subnet_of(ipaddress.ip_network("10.240.0.0/12"))


def test_aarch64_domain_uses_efi_firmware_autoselection_and_virt_machine(tmp_path):
    xml = LibvirtProvider._domain_xml(
        "Argus-arm",
        tmp_path / "session.qcow2",
        "argus-network",
        "argus-filter",
        "52:54:00:01:02:03",
        4096,
        2,
        "aarch64",
        "",
    )

    os_node = xml.find("os")
    assert os_node is not None
    assert os_node.attrib["firmware"] == "efi"
    type_node = os_node.find("type")
    assert type_node is not None
    assert type_node.attrib["arch"] == "aarch64"
    assert type_node.attrib["machine"] == "virt"


def test_x86_domain_does_not_force_efi_firmware(tmp_path):
    xml = LibvirtProvider._domain_xml(
        "Argus-x86",
        tmp_path / "session.qcow2",
        "argus-network",
        "argus-filter",
        "52:54:00:01:02:03",
        4096,
        2,
        "x86_64",
        "",
    )
    os_node = xml.find("os")
    assert os_node is not None
    assert "firmware" not in os_node.attrib


class _CapabilityProvider(CapsuleProvider):
    provider_name = "extension"

    def __init__(self, capabilities: CapsuleProviderCapabilities):
        self.provider_capabilities = capabilities
        self.create_called = False

    def create(self, request: CapsuleRequest) -> CapsuleHandle:
        self.create_called = True
        raise AssertionError("capability validation must happen before provider.create")

    def destroy(self, handle: CapsuleHandle) -> None:
        return None


def _caps(**overrides) -> CapsuleProviderCapabilities:
    values = dict(
        provider="extension",
        host_platforms=("linux", "windows"),
        guest_os=("linux",),
        secure_transport=True,
        network_isolation=True,
        explicit_transfers=True,
        failure_retention=True,
        egress_allowlist=True,
    )
    values.update(overrides)
    return CapsuleProviderCapabilities(**values)


def _settings(**overrides) -> CapsuleSettings:
    values = dict(
        provider="extension",
        guest_os="linux",
        guest_token="bootstrap",
        rotate_session_token=True,
        network_mode="host_only",
    )
    values.update(overrides)
    return CapsuleSettings(**values)


@pytest.mark.parametrize(
    "capabilities,missing",
    [
        (_caps(secure_transport=False), "secure transport"),
        (_caps(network_isolation=False), "network isolation"),
        (_caps(explicit_transfers=False), "explicit staging/collection"),
    ],
)
def test_secure_environment_rejects_injected_provider_missing_core_security(
    capabilities, missing
):
    provider = _CapabilityProvider(capabilities)
    with pytest.raises(ExecutionEnvironmentError, match=missing):
        SecureCapsuleExecutionEnvironment("cli", _settings(), provider=provider)
    assert provider.create_called is False


def test_secure_environment_requires_failure_retention_when_requested():
    provider = _CapabilityProvider(_caps(failure_retention=False))
    with pytest.raises(ExecutionEnvironmentError, match="failure retention"):
        SecureCapsuleExecutionEnvironment(
            "cli", _settings(retain_on_failure=True), provider=provider
        )
    assert provider.create_called is False


@pytest.mark.parametrize(
    "settings",
    [
        _settings(network_mode="allowlist"),
        _settings(egress_allowlist=("10.20.30.0/24",)),
    ],
)
def test_secure_environment_requires_egress_capability_when_requested(settings):
    provider = _CapabilityProvider(_caps(egress_allowlist=False))
    with pytest.raises(ExecutionEnvironmentError, match="egress allowlisting"):
        SecureCapsuleExecutionEnvironment("cli", settings, provider=provider)
    assert provider.create_called is False


def test_secure_environment_accepts_injected_provider_with_required_capabilities():
    provider = _CapabilityProvider(_caps())
    environment = SecureCapsuleExecutionEnvironment("cli", _settings(), provider=provider)
    assert environment.provider is provider
    assert provider.create_called is False
