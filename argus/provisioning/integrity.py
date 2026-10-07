"""Race-resistant hashing helpers for provisioning inputs and outputs."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import stat
from typing import Iterable

from argus.provisioning.model import ProvisioningError


@dataclass(frozen=True)
class VerifiedFile:
    path: Path
    size: int
    sha256: str


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _windows_final_path(fd: int) -> Path:
    """Return the canonical DOS/UNC path for an already-open Windows file handle."""

    import ctypes
    from ctypes import wintypes
    import msvcrt

    handle = msvcrt.get_osfhandle(fd)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_final_path = kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    get_final_path.restype = wintypes.DWORD

    size = 32768
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        written = get_final_path(handle, buffer, size, 0)
        if written == 0:
            error = ctypes.get_last_error()
            raise OSError(error, "GetFinalPathNameByHandleW failed")
        if written < size:
            raw = buffer.value
            break
        size = written + 1

    unc_prefix = "\\\\?\\UNC\\"
    device_prefix = "\\\\?\\"
    if raw.startswith(unc_prefix):
        raw = "\\\\" + raw[len(unc_prefix):]
    elif raw.startswith(device_prefix):
        raw = raw[len(device_prefix):]
    return Path(raw)


def verify_regular_file(
    path: str | Path,
    *,
    expected_sha256: str,
    allowed_roots: Iterable[str | Path] = (),
) -> VerifiedFile:
    """Open and hash a regular file without accepting a final symlink.

    The returned digest proves the bytes read through this file handle. A later
    provisioner must stage/copy or re-open and re-verify the source immediately
    before attaching it to a hypervisor; this helper deliberately does not claim
    to pin a pathname after the handle is closed.
    """

    candidate = Path(path).expanduser()
    try:
        if candidate.is_symlink():
            raise ProvisioningError("provisioning files must not be symlinks")
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ProvisioningError(f"cannot resolve provisioning file {candidate}: {exc}") from exc

    roots = tuple(Path(root).expanduser().resolve() for root in allowed_roots)
    if roots and not any(_is_within(resolved, root) for root in roots):
        raise ProvisioningError(
            f"provisioning file {resolved} is outside the configured allowed roots"
        )

    flags = os.O_RDONLY
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)

    fd: int | None = None
    if roots:
        matching_roots = [root for root in roots if _is_within(resolved, root)]
        root = max(matching_roots, key=lambda item: len(item.parts))

        if os.name == "nt":
            # Windows has no dir_fd/openat traversal in Python. Pin the exact
            # pre-validated file identity, then verify the path of the opened
            # kernel handle before reading any bytes. A directory/junction swap
            # to another file or outside the trusted roots therefore fails closed.
            try:
                expected = os.stat(resolved, follow_symlinks=False)
                if not stat.S_ISREG(expected.st_mode):
                    raise ProvisioningError("provisioning source must be a regular file")
                fd = os.open(resolved, flags)
                opened_path = _windows_final_path(fd)
                opened = os.fstat(fd)
            except OSError as exc:
                if fd is not None:
                    os.close(fd)
                    fd = None
                raise ProvisioningError(
                    f"cannot securely open provisioning file {resolved}: {exc}"
                ) from exc

            if not any(_is_within(opened_path, allowed) for allowed in roots):
                os.close(fd)
                fd = None
                raise ProvisioningError(
                    f"opened provisioning file {opened_path} is outside the configured allowed roots"
                )
            if expected.st_dev != opened.st_dev or expected.st_ino != opened.st_ino:
                os.close(fd)
                fd = None
                raise ProvisioningError(
                    "provisioning file changed while it was being securely opened"
                )
        else:
            # Anchor each path component to a trusted directory descriptor so
            # an intermediate symlink swap cannot redirect the final open.
            if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
                raise ProvisioningError(
                    "secure allowed-root verification is not supported on this platform"
                )
            relative = resolved.relative_to(root)
            root_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            root_flags |= getattr(os, "O_DIRECTORY", 0)
            root_flags |= os.O_NOFOLLOW
            try:
                dir_fd = os.open(root, root_flags)
                try:
                    parts = relative.parts
                    if not parts:
                        raise ProvisioningError("provisioning source must be a file")
                    for component in parts[:-1]:
                        next_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                        next_flags |= getattr(os, "O_DIRECTORY", 0)
                        next_flags |= os.O_NOFOLLOW
                        next_fd = os.open(component, next_flags, dir_fd=dir_fd)
                        os.close(dir_fd)
                        dir_fd = next_fd
                    fd = os.open(parts[-1], flags, dir_fd=dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError as exc:
                raise ProvisioningError(
                    f"cannot securely open provisioning file {resolved}: {exc}"
                ) from exc
    else:
        try:
            fd = os.open(resolved, flags)
        except OSError as exc:
            raise ProvisioningError(f"cannot open provisioning file {resolved}: {exc}") from exc

    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ProvisioningError("provisioning source must be a regular file")

        digest = sha256()
        with os.fdopen(fd, "rb", closefd=False) as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)

        after = os.fstat(fd)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise ProvisioningError("provisioning file changed while it was being verified")
        actual = digest.hexdigest()
    finally:
        os.close(fd)

    if actual != expected_sha256:
        raise ProvisioningError(
            f"SHA-256 mismatch for {resolved}: expected {expected_sha256}, got {actual}"
        )
    return VerifiedFile(path=resolved, size=before.st_size, sha256=actual)
