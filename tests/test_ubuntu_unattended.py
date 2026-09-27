"""Subiquity answer generation and NoCloud seed lifecycle."""

from dataclasses import replace
from hashlib import sha256
import os
from pathlib import Path
import platform
import stat

import pytest
import yaml

from argus.provisioning.model import (
    EnvironmentDefinition,
    InstallationMediaSource,
    InstallationSpec,
    MachineSpec,
    ProvisioningError,
)
from argus.provisioning.ubuntu_unattended import (
    UbuntuAutoinstallProfile,
    temporary_nocloud_seed_iso,
    validate_ubuntu_source_id,
)
from argus.provisioning.planner import build_provisioning_plan
from argus.provisioning.providers import LibvirtProvisioner
from argus.secrets import ArgusSecretStore


_HASH = "$6$rounds=5000$somesalt$" + "a" * 86


def _definition() -> EnvironmentDefinition:
    installation = InstallationSpec(
        unattended=True,
        credential_ref="secret://ubuntu/bootstrap",
        update_policy="latest",
        target_os="ubuntu", target_release="24.04.1", target_flavor="desktop",
        apt_mirror="http://mirror.internal/ubuntu",
        packages=("curl", "git"),
    )
    return EnvironmentDefinition(
        name="ubuntu-24-04-lab",
        source=InstallationMediaSource(
            path="/isos/ubuntu-24.04.1-desktop-amd64.iso", sha256="a" * 64
        ),
        machine=MachineSpec(
            architecture="x86_64", firmware="bios", disk_bus="virtio",
            network_mode="host_only"
        ),
        installation=installation,
    )


def _profile(**kwargs: object) -> UbuntuAutoinstallProfile:
    return UbuntuAutoinstallProfile(
        definition=_definition(), password_hash=_HASH, **kwargs,
    )


def test_nocloud_answers_are_subiquity_cloud_config() -> None:
    profile = _profile()
    data = yaml.safe_load(profile.user_data())
    autoinstall = data["autoinstall"]

    assert profile.user_data().startswith("#cloud-config\n")
    assert profile.kernel_cmdline == "autoinstall"
    assert autoinstall["version"] == 1
    assert autoinstall["identity"] == {
        "hostname": profile.hostname, "username": "argus", "password": _HASH
    }
    assert autoinstall["locale"] == "en_US.UTF-8"
    assert autoinstall["timezone"] == "Etc/UTC"
    assert autoinstall["source"] == {"id": "ubuntu-desktop"}
    assert autoinstall["storage"] == {"layout": {"name": "direct"}}
    assert autoinstall["refresh-installer"] == {"update": False}
    assert autoinstall["apt"] == {
        "geoip": False,
        "mirror-selection": {"primary": [{"uri": "http://mirror.internal/ubuntu"}]},
        "fallback": "abort",
    }
    assert autoinstall["packages"] == ["curl", "git"]
    assert autoinstall["updates"] == "all"
    assert autoinstall["shutdown"] == "poweroff"
    assert "secret://" not in profile.user_data()
    assert _HASH not in repr(profile)
    assert _HASH not in profile.meta_data()


def test_libvirt_builder_uses_verified_source_and_private_seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    iso = tmp_path / "ubuntu.iso"
    iso.write_bytes(b"verified-ubuntu-iso")
    base = _definition()
    ref = "secret://argus/ubuntu/bootstrap"
    definition = replace(
        base,
        source=replace(base.source, path=str(iso), sha256=sha256(iso.read_bytes()).hexdigest()),
        installation=replace(base.installation, credential_ref=ref),
    )
    store = ArgusSecretStore(tmp_path / "secrets")
    store.set(ref, _HASH)
    commands: list[tuple[str, ...]] = []
    domain_xml: list[str] = []

    def run(argv, _timeout):
        command = tuple(argv)
        commands.append(command)
        if command[0] == "xorriso":
            destination = Path(command[-1])
            if command[-2] == "/casper/install-sources.yaml":
                destination.write_text("- id: ubuntu-desktop\n  variant: desktop\n")
            else:
                destination.write_bytes(b"verified boot input")
        elif command[0] == "cloud-localds":
            Path(command[3]).write_bytes(b"private NoCloud fixture")
        elif command[:2] == ("qemu-img", "create"):
            Path(command[-2]).write_bytes(b"installed Ubuntu fixture")
        elif command[:2] == ("qemu-img", "info"):
            return '{"format":"qcow2","virtual-size":68719476736}'
        elif "net-dumpxml" in command:
            return "<network><name>argus-local</name><bridge name='virbr9'/></network>"
        elif "net-info" in command:
            return "Name: argus-local\nActive: yes"
        elif "domstate" in command:
            return "shut off"
        elif "define" in command:
            domain_xml.append(Path(command[-1]).read_text())
        return ""

    provider = LibvirtProvisioner(
        network_name="argus-local", runner=run, secret_store=store,
        baseline_validator=lambda _image: None,
    )
    plan = build_provisioning_plan(
        definition, provider.capabilities(), output_format="qcow2", cache_root=tmp_path / "cache",
    )
    result = provider.provision(definition, plan)
    assert result.manifest.evidence_run_id is not None
    assert domain_xml and "<kernel>" in domain_xml[0] and "<initrd>" in domain_xml[0]
    assert "<cmdline>autoinstall</cmdline>" in domain_xml[0]
    assert "seed.iso" in domain_xml[0]
    assert any(command[:2] == ("virsh", "-c") and "undefine" in command
               for command in commands)
    assert not list(plan.cache_dir.glob("argus-nocloud-*"))
    assert not (plan.cache_dir / "ubuntu-vmlinuz").exists()
    assert _HASH.encode() not in (plan.cache_dir / "manifest.json").read_bytes()


@pytest.mark.parametrize(
    ("release", "flavor", "supported"),
    [
        ("18.04.6", "server", False),
        ("20.04.6", "server", True),
        ("22.04", "desktop", False),
        ("23.04", "desktop", True),
        ("24.04.1", "desktop", True),
        ("24.04", "something", False),
        ("24.07", "server", False),
    ],
)
def test_release_and_flavor_gate(release: str, flavor: str, supported: bool) -> None:
    def create_profile() -> UbuntuAutoinstallProfile:
        definition = _definition()
        definition = replace(
            definition,
            installation=replace(
                definition.installation,
                target_release=release,
                target_flavor=flavor,
            ),
        )
        return UbuntuAutoinstallProfile(definition=definition, password_hash=_HASH)

    if supported:
        create_profile()
    else:
        with pytest.raises(ProvisioningError):
            create_profile()


def test_unsupported_definition_contract_fails_closed() -> None:
    base = _definition()
    variants = [
        replace(base, installation=replace(base.installation, unattended=False)),
        replace(base, installation=replace(base.installation, credential_ref=None)),
        replace(base, installation=replace(base.installation, edition="desktop")),
        replace(base, installation=replace(base.installation, update_policy="frozen")),
        replace(base, installation=replace(base.installation, update_policy="manual")),
        replace(base, installation=replace(base.installation, packages=("curl;id",))),
        replace(base, installation=replace(
            base.installation, target_os="windows-11", apt_mirror=None,
        )),
        replace(base, machine=replace(base.machine, network_mode="isolated")),
        replace(base, machine=replace(base.machine, architecture="aarch64"),
                source=replace(base.source, architecture="aarch64")),
    ]
    for definition in variants:
        with pytest.raises(ProvisioningError):
            UbuntuAutoinstallProfile(
                definition=definition, password_hash=_HASH,
            )


def test_password_hash_is_required_and_never_echoed() -> None:
    bad = "plaintext-secret"
    with pytest.raises(ProvisioningError) as error:
        UbuntuAutoinstallProfile(
            definition=_definition(), password_hash=bad,
        )
    assert bad not in str(error.value)


def test_nocloud_seed_iso_is_temporary_and_inputs_are_private(tmp_path: Path) -> None:
    profile = _profile()
    seen: list[tuple[str, ...]] = []

    def fake_runner(argv: tuple[str, ...], timeout: float) -> None:
        seen.append(tuple(argv))
        assert timeout == 60
        assert argv[:3] == ("cloud-localds", "-f", "iso")
        seed, user_data, meta_data = map(Path, argv[3:])
        assert _HASH in user_data.read_text(encoding="utf-8")
        assert "instance-id:" in meta_data.read_text(encoding="utf-8")
        if os.name == "posix":
            assert stat.S_IMODE(user_data.stat().st_mode) == 0o600
            assert stat.S_IMODE(meta_data.stat().st_mode) == 0o600
        seed.write_bytes(b"CD001 synthetic seed")

    with temporary_nocloud_seed_iso(
        profile, workspace=tmp_path, runner=fake_runner
    ) as seed:
        assert seed.is_file()
        if os.name == "posix":
            assert stat.S_IMODE(seed.stat().st_mode) == 0o600
        assert not (seed.parent / "user-data").exists()
        assert not (seed.parent / "meta-data").exists()
        assert list(tmp_path.iterdir()) == [seed.parent]

    assert seen
    assert list(tmp_path.iterdir()) == []


def test_requested_source_must_exist_in_staged_iso(tmp_path: Path) -> None:
    iso = tmp_path / "staged.iso"
    iso.write_bytes(b"staged ISO placeholder")
    profile = _profile()
    seen: list[tuple[str, ...]] = []

    def fake_xorriso(argv: tuple[str, ...], timeout: float) -> None:
        seen.append(tuple(argv))
        assert timeout == 30
        assert argv[:6] == (
            "xorriso", "-osirrox", "on", "-indev", str(iso), "-extract"
        )
        assert argv[6] == "/casper/install-sources.yaml"
        Path(argv[7]).write_text(
            "- id: ubuntu-desktop-minimal\n  variant: desktop\n"
            "- id: ubuntu-desktop\n  variant: desktop\n",
            encoding="utf-8",
        )

    validate_ubuntu_source_id(iso, profile, runner=fake_xorriso)
    assert seen


def test_missing_or_wrong_flavor_source_fails_closed(tmp_path: Path) -> None:
    iso = tmp_path / "staged.iso"
    iso.write_bytes(b"staged ISO placeholder")

    def missing(argv: tuple[str, ...], timeout: float) -> None:
        Path(argv[7]).write_text("- id: ubuntu-desktop-minimal\n", encoding="utf-8")

    with pytest.raises(ProvisioningError, match="unavailable"):
        validate_ubuntu_source_id(iso, _profile(), runner=missing)

    def wrong_flavor(argv: tuple[str, ...], timeout: float) -> None:
        Path(argv[7]).write_text(
            "- id: ubuntu-desktop\n  variant: server\n", encoding="utf-8"
        )

    with pytest.raises(ProvisioningError, match="wrong flavor"):
        validate_ubuntu_source_id(iso, _profile(), runner=wrong_flavor)


def test_seed_creation_failure_removes_secret_inputs(tmp_path: Path) -> None:
    def fail_runner(argv: tuple[str, ...], timeout: float) -> None:
        raise ProvisioningError("seed creation failed")

    with pytest.raises(ProvisioningError, match="seed creation failed"):
        with temporary_nocloud_seed_iso(
            _profile(), workspace=tmp_path, runner=fail_runner
        ):
            pytest.fail("seed should not be yielded")
    assert list(tmp_path.iterdir()) == []
