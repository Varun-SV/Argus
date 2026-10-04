"""Host storage permissions for one-attempt system-QEMU control media."""

from dataclasses import replace
import os
from pathlib import Path
import stat
import shutil
import subprocess
import tempfile

import pytest

from argus.capsule.base import CapsuleError, CapsuleHandle, CapsuleSettings
from argus.capsule.bootstrap import create_bootstrap_attempt
from argus.capsule.control import new_capsule_id
from argus.capsule.libvirt import LibvirtProvider


pytestmark = pytest.mark.skipif(os.name != "posix", reason="native POSIX group permissions")


def _handle(root):
    return CapsuleHandle(
        session_id="session-one", provider="libvirt", vm_name="Argus-test",
        root_dir=str(root), address="", guest_port=8765, capsule_id=new_capsule_id(),
    )


def test_system_qemu_can_read_only_iso_without_opening_control_root():
    import grp

    with tempfile.TemporaryDirectory(dir="/tmp") as name:
        root = Path(name).resolve()
        root.chmod(0o755)
        vm = root / "provider-storage" / "capsule"
        vm.mkdir(parents=True, mode=0o755)
        control = root / "private-home" / "control"
        commands = []
        provider = LibvirtProvider(runner=lambda argv, timeout: commands.append(tuple(argv)) or "")
        settings = CapsuleSettings(
            provider="libvirt", control_root=str(control),
            libvirt_qemu_group=grp.getgrgid(os.getgid()).gr_name,
        )
        attempt = create_bootstrap_attempt(
            control / "bootstrap-attempts", capsule_id=new_capsule_id(),
            control_generation=1, execution_mode="isolated",
            runtime_identity="runtime-sha256-" + "a" * 64,
        )
        try:
            media_dir = provider.bootstrap_media_directory(_handle(vm), settings)
            media = provider.create_bootstrap_media(attempt.root, media_dir / "generation-one.iso")
            assert media.parent == vm / "bootstrap-media"
            assert stat.S_IMODE(media_dir.stat().st_mode) == 0o2710
            assert stat.S_IMODE(media.stat().st_mode) == 0o640
            assert media.stat().st_gid == os.getgid()
            assert stat.S_IMODE(attempt.root.stat().st_mode) == 0o700
            assert stat.S_IMODE(attempt.token_path.stat().st_mode) == 0o600
            assert stat.S_IMODE(attempt.tls_key_path.stat().st_mode) == 0o600
            assert commands == [("setfacl", "-b", "-k", "--", str(media_dir))]
            provider.destroy_bootstrap_media(media)
            assert not media.exists()
            assert attempt.token_path.exists()
        finally:
            attempt.destroy()


def test_qemu_access_rejects_private_ancestor_and_missing_group():
    import grp

    with tempfile.TemporaryDirectory(dir="/tmp") as name:
        root = Path(name).resolve()  # private ancestor cannot be traversed by QEMU
        vm = root / "capsule"
        vm.mkdir(mode=0o755)
        calls = []
        provider = LibvirtProvider(runner=lambda *args: calls.append(args))
        settings = CapsuleSettings(libvirt_qemu_group=grp.getgrgid(os.getgid()).gr_name)
        with pytest.raises(CapsuleError, match="cannot traverse"):
            provider.bootstrap_media_directory(_handle(vm), settings)
        with pytest.raises(CapsuleError, match="explicit libvirt_qemu_group"):
            provider.bootstrap_media_directory(_handle(vm), replace(settings, libvirt_qemu_group=""))
        assert not (vm / "bootstrap-media").exists()
        assert calls == []
        assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_inherited_acl_removal_failure_prevents_secret_rendering():
    import grp

    with tempfile.TemporaryDirectory(dir="/tmp") as name:
        root = Path(name).resolve()
        root.chmod(0o755)
        settings = CapsuleSettings(libvirt_qemu_group=grp.getgrgid(os.getgid()).gr_name)

        def fail_acl(*args):
            raise CapsuleError("ACL removal failed")

        provider = LibvirtProvider(runner=fail_acl)
        with pytest.raises(CapsuleError, match="ACL removal failed"):
            provider.bootstrap_media_directory(_handle(root), settings)
        media_dir = root / "bootstrap-media"
        assert not list(media_dir.iterdir())
        assert stat.S_IMODE(media_dir.stat().st_mode) == 0o700


@pytest.mark.skipif(not shutil.which("setfacl") or not shutil.which("getfacl"),
                    reason="native ACL tools are required")
def test_native_bootstrap_storage_removes_inherited_named_and_default_acls():
    import grp

    with tempfile.TemporaryDirectory(dir="/tmp") as name:
        root = Path(name).resolve()
        root.chmod(0o755)
        unrelated_uid = os.geteuid() + 1
        subprocess.run(
            ["setfacl", "-m", f"d:u:{unrelated_uid}:r-x", str(root)], check=True,
        )
        provider = LibvirtProvider()
        settings = CapsuleSettings(libvirt_qemu_group=grp.getgrgid(os.getgid()).gr_name)
        directory = provider.bootstrap_media_directory(_handle(root), settings)
        acl = subprocess.check_output(["getfacl", "-cpn", str(directory)], text=True)
        assert f"user:{unrelated_uid}:" not in acl
        assert "default:" not in acl
        assert stat.S_IMODE(directory.stat().st_mode) == 0o2710
