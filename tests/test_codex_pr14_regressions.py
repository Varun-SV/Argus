from __future__ import annotations

import json
from pathlib import Path

import pytest

from argus.capsule.base import CapsuleError, CapsuleRequest, CapsuleSettings
from argus.capsule.libvirt import LibvirtProvider
from argus.config import load_config
from argus.execution.secure_capsule import SecureCapsuleExecutionEnvironment


def _settings(tmp_path: Path, **overrides) -> CapsuleSettings:
    image = tmp_path / "golden.qcow2"
    image.write_bytes(b"fake-image")
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


def _command(args: tuple[str, ...]) -> str:
    if len(args) > 3 and args[1:3] == ("-c", "qemu:///system"):
        return args[3]
    return ""


def test_project_config_propagates_all_libvirt_settings(tmp_path, monkeypatch):
    argus_dir = tmp_path / ".argus"
    argus_dir.mkdir()
    image = tmp_path / "golden.qcow2"
    image.write_bytes(b"fake-image")
    vm_root = tmp_path / "system-capsules"
    (argus_dir / "config.yaml").write_text(
        f"""
provider: ollama
execution:
  environment: capsule
  capsule:
    provider: libvirt
    guest_os: linux
    image: {image.as_posix()}
    vm_root: {vm_root.as_posix()}
    libvirt_uri: qemu:///system
    libvirt_network_cidr: 10.251.44.0/24
    libvirt_arch: x86_64
    libvirt_machine: pc-q35-test
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("ARGUS_CAPSULE_GUEST_TOKEN", "bootstrap-secret")

    cfg = load_config(tmp_path)
    cc = cfg.execution.capsule
    assert cc.guest_os == "linux"
    assert cc.libvirt_uri == "qemu:///system"
    assert cc.libvirt_network_cidr == "10.251.44.0/24"
    assert cc.libvirt_arch == "x86_64"
    assert cc.libvirt_machine == "pc-q35-test"

    environment = cfg.make_execution_environment("cli")
    assert isinstance(environment, SecureCapsuleExecutionEnvironment)
    assert environment.settings.provider == "libvirt"
    assert environment.settings.guest_os == "linux"
    assert environment.settings.libvirt_uri == "qemu:///system"
    assert environment.settings.libvirt_network_cidr == "10.251.44.0/24"
    assert environment.settings.libvirt_arch == "x86_64"
    assert environment.settings.libvirt_machine == "pc-q35-test"
    assert Path(environment.settings.vm_root) == vm_root


def test_libvirt_env_overrides_are_propagated(tmp_path, monkeypatch):
    argus_dir = tmp_path / ".argus"
    argus_dir.mkdir()
    (argus_dir / "config.yaml").write_text(
        """
provider: ollama
execution:
  environment: capsule
  capsule:
    provider: libvirt
    guest_os: linux
    libvirt_network_cidr: 10.251.1.0/24
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("ARGUS_CAPSULE_GUEST_TOKEN", "bootstrap-secret")
    monkeypatch.setenv("ARGUS_CAPSULE_LIBVIRT_NETWORK_CIDR", "10.252.9.0/24")
    monkeypatch.setenv("ARGUS_CAPSULE_LIBVIRT_ARCH", "aarch64")
    monkeypatch.setenv("ARGUS_CAPSULE_LIBVIRT_MACHINE", "virt")

    environment = load_config(tmp_path).make_execution_environment("cli")
    assert environment.settings.libvirt_network_cidr == "10.252.9.0/24"
    assert environment.settings.libvirt_arch == "aarch64"
    assert environment.settings.libvirt_machine == "virt"


def test_libvirt_names_do_not_alias_sessions_with_same_prefix():
    first = LibvirtProvider._resource_names("abcdefghijkl-first-session")
    second = LibvirtProvider._resource_names("abcdefghijkl-second-session")
    assert first[0] != second[0]
    assert first[1] != second[1]
    assert first[2] != second[2]


def test_preexisting_domain_collision_is_rejected_without_cleanup(tmp_path):
    session_id = "collision-session"
    vm_name, _network_name, _filter_name, _bridge = LibvirtProvider._resource_names(session_id)
    calls: list[tuple[str, ...]] = []

    def runner(args, timeout):
        argv = tuple(str(item) for item in args)
        calls.append(argv)
        if argv[0] == "qemu-img" and "info" in argv:
            return json.dumps({"format": "qcow2"})
        command = _command(argv)
        if command == "list" and "--all" in argv:
            return vm_name
        if command == "net-list":
            return ""
        if command == "nwfilter-list":
            return ""
        return ""

    provider = LibvirtProvider(runner=runner)
    settings = _settings(tmp_path)
    root = Path(settings.vm_root) / session_id

    with pytest.raises(CapsuleError, match="resource collision"):
        provider.create(CapsuleRequest(session_id, "cli", settings))

    assert not root.exists()
    assert not any(_command(call) in {"destroy", "undefine"} for call in calls)
    assert not any(_command(call) in {"net-destroy", "nwfilter-undefine"} for call in calls)
    assert not any(call[0] == "qemu-img" and "create" in call for call in calls)


@pytest.mark.parametrize(
    "overrides,match",
    [
        ({"libvirt_network_cidr": "not-a-network"}, "canonical IPv4 network"),
        ({"guest_address": "10.1.2.3"}, "fixed per-session DHCP lease"),
        ({"libvirt_arch": "mips64"}, "x86_64/aarch64"),
    ],
)
def test_invalid_session_parameters_leave_no_session_directory(tmp_path, overrides, match):
    calls: list[tuple[str, ...]] = []

    def runner(args, timeout):
        calls.append(tuple(str(item) for item in args))
        return ""

    provider = LibvirtProvider(runner=runner)
    settings = _settings(tmp_path, **overrides)
    session_id = "invalid-session"
    root = Path(settings.vm_root) / session_id

    with pytest.raises(CapsuleError, match=match):
        provider.create(CapsuleRequest(session_id, "cli", settings))

    assert not root.exists()
    assert not any(call[0] == "qemu-img" and "create" in call for call in calls)


def test_libvirt_default_storage_is_system_qemu_accessible_location(tmp_path):
    assert LibvirtProvider._resolved_vm_root("") == Path(
        "/var/lib/libvirt/images/argus-capsules"
    )
    explicit = tmp_path / "provisioned-capsules"
    assert LibvirtProvider._resolved_vm_root(str(explicit)) == explicit.resolve()


def test_create_rollback_removes_only_resources_created_by_attempt(tmp_path):
    calls: list[tuple[str, ...]] = []
    state = {"filter": "", "network": "", "domain": ""}

    def runner(args, timeout):
        argv = tuple(str(item) for item in args)
        calls.append(argv)
        if argv[0] == "qemu-img":
            if "info" in argv:
                return json.dumps({"format": "qcow2"})
            if "create" in argv:
                Path(argv[-1]).write_bytes(b"overlay")
                return ""

        command = _command(argv)
        if command == "list":
            if "--all" in argv:
                return state["domain"]
            return ""
        if command == "net-list":
            return state["network"]
        if command == "nwfilter-list":
            if not state["filter"]:
                return ""
            return f"UUID Name\n----- ----\ndeadbeef {state['filter']}"
        if command == "nwfilter-define":
            state["filter"] = LibvirtProvider._resource_names("owned-rollback")[2]
            return "defined"
        if command == "net-create":
            state["network"] = LibvirtProvider._resource_names("owned-rollback")[1]
            return "created"
        if command == "define":
            # Simulate a provider failure before Argus owns a domain.
            raise CapsuleError("domain define failed")
        if command == "net-destroy":
            state["network"] = ""
            return ""
        if command == "nwfilter-undefine":
            state["filter"] = ""
            return ""
        return ""

    provider = LibvirtProvider(runner=runner)
    settings = _settings(tmp_path)

    with pytest.raises(CapsuleError, match="domain define failed"):
        provider.create(CapsuleRequest("owned-rollback", "cli", settings))

    commands = [_command(call) for call in calls]
    assert "nwfilter-undefine" in commands
    assert "net-destroy" in commands
    assert "undefine" not in commands
    assert "destroy" not in commands
    assert not (Path(settings.vm_root) / "owned-rollback").exists()
