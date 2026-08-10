"""Deterministic file-transfer policy for Argus Capsule workspaces.

All host→guest staging and guest→host collection paths are expressed as
POSIX-style relative paths rooted in a per-session workspace. Absolute paths,
drive-qualified paths, traversal, and symlink/reparse escapes are rejected.
Guest alias rules are selected explicitly by guest OS: Windows keeps strict
Win32/NTFS collision handling, while POSIX guests keep case-sensitive names.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Iterable

from argus.capsule.base import CapsuleError

# Keep JSON/base64 requests comfortably below the guest agent's 1 MiB request
# limit and bound both individual and aggregate transfer exposure.
TRANSFER_CHUNK_BYTES = 256 * 1024
TRANSFER_MAX_FILE_BYTES = 64 * 1024 * 1024
TRANSFER_MAX_TOTAL_BYTES = 256 * 1024 * 1024

_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def validate_session_id(value: str) -> str:
    session_id = str(value or "").strip()
    if not _SESSION_RE.fullmatch(session_id):
        raise CapsuleError("Capsule transfer session id must be 1-64 safe characters")
    return session_id


def normalize_relative_path(value: str) -> str:
    """Return one canonical platform-neutral relative path or fail closed."""
    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        raise CapsuleError("Capsule transfer path cannot be empty")
    if raw.startswith("/") or ":" in raw:
        raise CapsuleError("Capsule transfer paths must be relative and drive-free")
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise CapsuleError("Capsule transfer paths cannot contain empty, '.' or '..' segments")
    if any("\x00" in part for part in parts):
        raise CapsuleError("Capsule transfer path contains a NUL byte")
    return "/".join(parts)


def _guest_os_kind(value: str = "") -> str:
    requested = str(value or "").strip().lower()
    if requested in {"windows", "win", "win32", "nt"}:
        return "windows"
    if requested in {"linux", "posix", "unix"}:
        return "posix"
    if requested not in {"", "auto"}:
        raise CapsuleError(f"unsupported Capsule guest OS path semantics: {value!r}")
    return "windows" if os.name == "nt" else "posix"


def normalize_guest_relative_path(value: str, guest_os: str = "") -> str:
    """Normalize one guest-workspace path using the guest filesystem semantics.

    Windows rejects Win32 aliases (trailing spaces/dots and reserved device
    names). POSIX/Linux keeps the platform-neutral traversal/NUL rules but is
    otherwise case-sensitive and permits names such as ``con``.
    """
    relative = normalize_relative_path(value)
    if _guest_os_kind(guest_os) == "windows":
        for part in relative.split("/"):
            if part.rstrip(" .") != part:
                raise CapsuleError(
                    "Capsule guest path segments cannot end in a space or dot"
                )
            stem = part.split(".", 1)[0].casefold()
            if stem in _WINDOWS_RESERVED:
                raise CapsuleError(f"Capsule guest path uses reserved Windows name: {part}")
    return relative


def guest_path_key(value: str, guest_os: str = "") -> str:
    """Return the filesystem-alias key for one guest path."""
    relative = normalize_guest_relative_path(value, guest_os=guest_os)
    if _guest_os_kind(guest_os) == "windows":
        return relative.casefold()
    return relative


def workspace_path(root: Path, relative: str, *, must_exist: bool = False) -> Path:
    """Resolve ``relative`` under ``root`` and reject filesystem escapes.

    ``Path.resolve`` follows symlinks and, on supported Windows/Python builds,
    junction/reparse targets. Containment is checked after resolution so a
    guest-created link cannot redirect collection outside the workspace.
    """
    rel = normalize_relative_path(relative)
    root_resolved = root.resolve(strict=True)
    candidate = root.joinpath(*rel.split("/"))
    try:
        resolved = candidate.resolve(strict=must_exist)
    except OSError as exc:
        raise CapsuleError(f"Capsule transfer path cannot be resolved: {rel}: {exc}") from exc
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise CapsuleError(f"Capsule transfer path escapes the session workspace: {rel}") from exc
    return resolved


def project_source_path(project_root: Path, source: str) -> Path:
    """Resolve an explicitly staged host source without leaving the project.

    Production staging additionally binds the source through a race-safe file
    handle before hashing/uploading. This helper remains useful for callers that
    only need deterministic path validation.
    """
    rel = normalize_relative_path(source)
    root = project_root.resolve(strict=True)
    candidate = root.joinpath(*rel.split("/"))
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise CapsuleError(f"staging source cannot be resolved: {rel}: {exc}") from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise CapsuleError(f"staging source escapes the project root: {rel}") from exc
    if not resolved.is_file():
        raise CapsuleError(f"staging source must be a regular file: {rel}")
    size = resolved.stat().st_size
    if size > TRANSFER_MAX_FILE_BYTES:
        raise CapsuleError(
            f"staging source exceeds {TRANSFER_MAX_FILE_BYTES} byte per-file limit: {rel}"
        )
    return resolved


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def enforce_total_bytes(sizes: Iterable[int]) -> int:
    total = sum(int(size) for size in sizes)
    if total > TRANSFER_MAX_TOTAL_BYTES:
        raise CapsuleError(
            f"Capsule transfers exceed {TRANSFER_MAX_TOTAL_BYTES} byte session limit"
        )
    return total
