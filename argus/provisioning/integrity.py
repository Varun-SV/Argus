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
