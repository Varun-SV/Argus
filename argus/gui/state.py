"""Per-user desktop state and OS-enforced project-window ownership."""
from __future__ import annotations

import hashlib
import os
import stat
import sys
from pathlib import Path


def gui_state_root() -> Path:
    override = os.environ.get("ARGUS_GUI_STATE_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
                    or (Path.home() / "AppData" / "Local")) / "Argus"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Argus"
    return Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")) / "argus"


def project_identity(project: Path) -> str:
    return os.path.normcase(str(Path(project).resolve(strict=True)))


class ProjectInUse(OSError):
    """Another desktop window owns this project's conversation history."""


class ProjectLease:
    """Hold an OS lock until confirmed window close or process death.

    Keep the lock file in place on release: unlinking it would allow another
    process to lock a different inode while the original still has an owner.
    """
    def __init__(self, project: Path):
        key = hashlib.sha256(project_identity(project).encode()).hexdigest()
        directory = gui_state_root() / "window-locks"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (key + ".lock")
        if path.is_symlink():
            raise OSError("Project window lock cannot be a symlink.")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_BINARY", 0), 0o600)
        self._fd = None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("Project window lock must be a regular file.")
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fd = fd
        except OSError as exc:
            os.close(fd)
            raise ProjectInUse("This project is already open in another Argus window. Use that window or close it before reopening the project.") from exc

    def close(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
