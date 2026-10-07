"""Provider-owned resource identity helpers for retained Capsule safety."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import os
import stat
from uuid import UUID

from argus.capsule.base import CapsuleError


def provider_uuid_identity(provider: str, value: str) -> str:
    name = str(provider or "").strip().lower()
    if not name:
        raise CapsuleError("provider identity name is missing")
    try:
        normalized = str(UUID(str(value or "").strip())).lower()
    except (ValueError, AttributeError) as exc:
        raise CapsuleError(f"{name} provider returned an invalid resource UUID") from exc
    return f"{name}-uuid:{normalized}"


def mutable_disk_identity(path: str | Path) -> str:
    """Bind one writable disk to its opened filesystem object and canonical path."""
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise CapsuleError("Capsule mutable disk cannot be a symlink")
    try:
        with candidate.open("rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise CapsuleError("Capsule mutable disk must be a regular file")
            resolved = Path(os.path.realpath(candidate))
    except OSError as exc:
        raise CapsuleError(f"cannot identify Capsule mutable disk: {exc}") from exc
    path_digest = sha256(
        os.path.normcase(str(resolved)).encode("utf-8", "surrogatepass")
    ).hexdigest()
    return (
        f"file-v1:{int(info.st_dev):x}:{int(info.st_ino):x}:"
        f"path-sha256-{path_digest}"
    )
