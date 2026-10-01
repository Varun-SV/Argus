"""Guest-side bootstrap-service media discovery and staging."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import ctypes
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
from typing import Callable, Iterator, Sequence
from uuid import uuid4

from argus.capsule.base import CapsuleError
from argus.capsule.bootstrap import (
    CapsuleBootstrapManifest,
    load_bootstrap_manifest,
)
from argus.capsule.control import GuestControlStateStore


_BOOTSTRAP_LABEL = "ARGUS_BOOTSTRAP"


@dataclass(frozen=True)
class PreparedBootstrapService:
    manifest: CapsuleBootstrapManifest
    staging_root: Path
    token_path: Path
    tls_cert_path: Path
    tls_key_path: Path
    control_state_store: GuestControlStateStore

    def cleanup_public_staging(self) -> None:
        """Remove non-secret staging after SSL/token consumers loaded their bytes."""
        for path in (
            self.tls_cert_path,
            self.staging_root / "bootstrap.json",
        ):
            path.unlink(missing_ok=True)
        try:
            self.staging_root.rmdir()
        except OSError:
            # Token/key consumers may not have run yet. The caller performs a
            # final cleanup when the agent exits.
            pass

    def cleanup_all_staging(self) -> None:
        try:
            shutil.rmtree(self.staging_root)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise CapsuleError(
                f"cannot clean guest bootstrap staging {self.staging_root}: {exc}"
            ) from exc


def _run(argv: Sequence[str], timeout: float) -> None:
    try:
        result = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CapsuleError("Capsule bootstrap media operation failed") from exc
    if result.returncode:
        raise CapsuleError("Capsule bootstrap media operation failed")


def _default_runtime_identity_file() -> Path:
    if platform.system().lower() == "windows":
        base = Path(os.environ.get("ProgramData", r"C:\ProgramData"))
        return base / "Argus" / "runtime-identity.json"
    return Path("/etc/argus/runtime-identity.json")


def _default_control_state_file() -> Path:
    if platform.system().lower() == "windows":
        base = Path(os.environ.get("ProgramData", r"C:\ProgramData"))
        return base / "Argus" / "control-state.json"
    return Path("/var/lib/argus/control-state.json")


def _default_staging_parent() -> Path:
    if platform.system().lower() == "windows":
        base = Path(os.environ.get("ProgramData", r"C:\ProgramData"))
        return base / "Argus" / "bootstrap"
    return Path("/run/argus/bootstrap")


def _installed_runtime_identity(path: Path) -> str:
    if path.is_symlink():
        raise CapsuleError("installed Argus runtime identity file cannot be a symlink")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CapsuleError("installed Argus runtime identity is unavailable") from exc
    identity = str(raw.get("runtime_identity") or "")
    if not identity.startswith("runtime-sha256-") or len(identity) != 79:
        raise CapsuleError("installed Argus runtime identity is invalid")
    return identity


def _copy_regular(source: Path, destination: Path) -> None:
    if source.is_symlink():
        raise CapsuleError("Capsule bootstrap media cannot contain symlinks")
    try:
        info = source.stat()
    except OSError as exc:
        raise CapsuleError("Capsule bootstrap media member is unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
        raise CapsuleError("Capsule bootstrap media member must be a regular file")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    fd = os.open(destination, flags, 0o600)
    try:
        with source.open("rb") as src, os.fdopen(fd, "wb", closefd=False) as dst:
            copied = 0
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
                copied += len(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        if copied != info.st_size:
            raise CapsuleError("Capsule bootstrap media changed while being staged")
    finally:
        os.close(fd)
    if os.name == "posix":
        destination.chmod(0o600)


def _stage_from_root(source_root: Path, staging_parent: Path) -> Path:
    manifest = load_bootstrap_manifest(source_root)
    staging_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        staging_parent.chmod(0o700)
    staging = staging_parent / (
        f"{manifest.capsule_id}-g{manifest.control_generation}-{uuid4().hex}"
    )
    staging.mkdir(mode=0o700)
    try:
        members = (
            ("bootstrap.json", "bootstrap.json"),
            (manifest.token_file, "bootstrap.token"),
            (manifest.tls_cert_file, "tls-cert.pem"),
            (manifest.tls_key_file, "tls-key.pem"),
        )
        for source_name, destination_name in members:
            _copy_regular(
                source_root / source_name,
                staging / destination_name,
            )
        # Re-validate the exact staged certificate/manifest before use.
        load_bootstrap_manifest(staging)
        return staging.resolve()
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _windows_bootstrap_root() -> Path:
    kernel32 = ctypes.windll.kernel32
    for code in range(ord("D"), ord("Z") + 1):
        root = f"{chr(code)}:\\"
        volume_name = ctypes.create_unicode_buffer(261)
        ok = kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(root),
            volume_name,
            len(volume_name),
            None,
            None,
            None,
            None,
            0,
        )
        if ok and volume_name.value == _BOOTSTRAP_LABEL:
            return Path(root)
    raise CapsuleError("ARGUS_BOOTSTRAP media was not found")


@contextmanager
def _bootstrap_source_root(
    explicit_root: str | Path | None = None,
    *,
    runner: Callable[[Sequence[str], float], None] | None = None,
) -> Iterator[Path]:
    configured = str(explicit_root or os.environ.get("ARGUS_BOOTSTRAP_ROOT") or "").strip()
    if configured:
        root = Path(configured).expanduser()
        if not root.is_dir() or root.is_symlink():
            raise CapsuleError("configured Capsule bootstrap root is invalid")
        yield root.resolve()
        return

    if platform.system().lower() == "windows":
        yield _windows_bootstrap_root()
        return

    device = Path("/dev/disk/by-label") / _BOOTSTRAP_LABEL
    if not device.exists():
        raise CapsuleError("ARGUS_BOOTSTRAP block device was not found")
    mount_parent = Path("/run/argus")
    mount_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    mountpoint = mount_parent / f"bootstrap-media-{uuid4().hex}"
    mountpoint.mkdir(mode=0o700)
    run = runner or _run
    mounted = False
    try:
        run(
            (
                "mount",
                "-o",
                "ro,nosuid,nodev,noexec",
                str(device),
                str(mountpoint),
            ),
            30,
        )
        mounted = True
        yield mountpoint
    finally:
        if mounted:
            run(("umount", str(mountpoint)), 30)
        mountpoint.rmdir()


def prepare_bootstrap_service(
    *,
    bootstrap_root: str | Path | None = None,
    runtime_identity_file: str | Path | None = None,
    control_state_file: str | Path | None = None,
    staging_parent: str | Path | None = None,
    runner: Callable[[Sequence[str], float], None] | None = None,
) -> PreparedBootstrapService:
    """Copy one attached generation medium privately and validate its bindings."""
    runtime_file = Path(runtime_identity_file or _default_runtime_identity_file())
    installed_runtime = _installed_runtime_identity(runtime_file)
    destination_parent = Path(staging_parent or _default_staging_parent())

    with _bootstrap_source_root(bootstrap_root, runner=runner) as source_root:
        staging = _stage_from_root(source_root, destination_parent)

    try:
        manifest = load_bootstrap_manifest(staging)
        if manifest.runtime_identity != installed_runtime:
            raise CapsuleError(
                "Capsule bootstrap runtime identity does not match installed runtime"
            )
        state_store = GuestControlStateStore(
            Path(control_state_file or _default_control_state_file())
        )
        state = state_store.load()
        if state.capsule_id and state.capsule_id != manifest.capsule_id:
            raise CapsuleError(
                "Capsule bootstrap identity contradicts initialized guest state"
            )
        if (
            state.highest_committed_generation
            and manifest.control_generation
            <= state.highest_committed_generation
        ):
            raise CapsuleError("stale Capsule bootstrap generation was rejected")
        return PreparedBootstrapService(
            manifest=manifest,
            staging_root=staging,
            token_path=staging / "bootstrap.token",
            tls_cert_path=staging / "tls-cert.pem",
            tls_key_path=staging / "tls-key.pem",
            control_state_store=state_store,
        )
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
