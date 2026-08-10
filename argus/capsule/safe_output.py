"""Pinned host-output directories for Capsule artifact collection.

Windows relies on directory handles that omit ``FILE_SHARE_DELETE`` so trusted
run/artifact paths cannot be renamed or replaced during collection. POSIX cannot
obtain the same mandatory rename barrier, so PR7 retains opened directory file
descriptors and performs all mutable artifact operations relative to those FDs.
A lexical identity check still runs on context exit to surface concurrent tree
replacement, but bytes are never redirected through a replacement pathname.
"""
from __future__ import annotations

import os
import stat
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Iterator

from argus.capsule.base import CapsuleError
from argus.capsule.files import normalize_guest_relative_path
from argus.capsule.safe_open import _strip_windows_device_prefix, _windows_final_path


class PinnedDirectory:
    def __init__(self, path: Path, *, fd: int | None = None, handle=None, identity=None):
        self.path = path
        self.fd = fd
        self.handle = handle
        self.identity = identity

    def assert_current(self) -> None:
        """Fail if the lexical path no longer names the pinned directory."""
        if os.name == "nt":
            # Windows pins deny FILE_SHARE_DELETE, so successful retention of
            # the handle is itself the replacement barrier.
            return
        try:
            info = os.stat(self.path, follow_symlinks=False)
        except OSError as exc:
            raise CapsuleError(f"pinned artifact directory disappeared: {self.path}: {exc}") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise CapsuleError(f"pinned artifact path is no longer a directory: {self.path}")
        if (info.st_dev, info.st_ino) != self.identity:
            raise CapsuleError(f"pinned artifact directory identity changed: {self.path}")

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        if self.handle is not None:
            try:
                import ctypes
                from ctypes import wintypes

                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
                kernel32.CloseHandle.restype = wintypes.BOOL
                kernel32.CloseHandle(self.handle)
            except Exception:
                pass
            self.handle = None


class PinnedArtifactTree:
    """Artifact output tree whose parent directories remain open and attested.

    On POSIX, mutation helpers below never resolve parent directories through a
    pathname after pinning. ``open(..., dir_fd=...)``, ``rename(..., *_dir_fd=)``
    and ``unlink(..., dir_fd=...)`` therefore stay bound to the opened directory
    identity even if another same-user process renames/replaces its lexical path.
    """

    def __init__(self, path: Path, parents: dict[str, PinnedDirectory]):
        self.path = path
        self._parents = parents

    def __fspath__(self) -> str:
        return os.fspath(self.path)

    def __str__(self) -> str:
        return str(self.path)

    @staticmethod
    def _key(relative: str) -> tuple[str, str, str]:
        normalized = normalize_guest_relative_path(relative)
        parts = normalized.split("/")
        parent = "/".join(parts[:-1])
        lookup = parent.casefold() if os.name == "nt" else parent
        return normalized, lookup, parts[-1]

    def _parent_pin(self, relative: str) -> tuple[PinnedDirectory, str, str]:
        normalized, key, name = self._key(relative)
        pin = self._parents.get(key)
        if pin is None:
            raise CapsuleError(f"artifact parent directory was not pinned: {normalized}")
        return pin, name, normalized

    def lexical_path(self, relative: str) -> Path:
        normalized = normalize_guest_relative_path(relative)
        return self.path.joinpath(*normalized.split("/"))

    def open_temp_file(self, relative: str):
        """Create one POSIX temporary file relative to the pinned parent FD."""
        if os.name == "nt":
            raise CapsuleError("descriptor-relative artifact writes are POSIX-only")
        pin, name, normalized = self._parent_pin(relative)
        if pin.fd is None:
            raise CapsuleError(f"artifact parent directory is no longer pinned: {normalized}")
        temp_name = f".{name}.argus-{uuid.uuid4().hex}.part"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(temp_name, flags, 0o600, dir_fd=pin.fd)
        except OSError as exc:
            raise CapsuleError(f"artifact temporary file cannot be created safely: {normalized}: {exc}") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise CapsuleError(f"artifact temporary object is not a regular file: {normalized}")
            handle = os.fdopen(fd, "wb")
            fd = -1
            return handle, temp_name, self.lexical_path(normalized)
        finally:
            if fd >= 0:
                os.close(fd)

    def commit_temp(self, relative: str, temp_name: str) -> None:
        """Atomically rename a temporary file inside the same pinned parent."""
        if os.name == "nt":
            raise CapsuleError("descriptor-relative artifact commits are POSIX-only")
        pin, name, normalized = self._parent_pin(relative)
        if pin.fd is None:
            raise CapsuleError(f"artifact parent directory is no longer pinned: {normalized}")
        try:
            os.rename(
                temp_name,
                name,
                src_dir_fd=pin.fd,
                dst_dir_fd=pin.fd,
            )
        except OSError as exc:
            raise CapsuleError(f"artifact commit failed inside pinned directory: {normalized}: {exc}") from exc

    def remove_temp(self, relative: str, temp_name: str) -> None:
        if os.name == "nt":
            return
        pin, _name, _normalized = self._parent_pin(relative)
        if pin.fd is None:
            return
        try:
            os.unlink(temp_name, dir_fd=pin.fd)
        except FileNotFoundError:
            pass

    def unlink_relative(self, relative: str) -> None:
        """Remove a committed artifact without re-resolving its parent on POSIX."""
        pin, name, normalized = self._parent_pin(relative)
        if os.name == "nt":
            self.lexical_path(normalized).unlink()
            return
        if pin.fd is None:
            raise CapsuleError(f"artifact parent directory is no longer pinned: {normalized}")
        os.unlink(name, dir_fd=pin.fd)


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _pin_windows_directory(path: Path) -> PinnedDirectory:
    import ctypes
    import ntpath
    from ctypes import wintypes

    expected = ntpath.normcase(ntpath.abspath(str(path)))
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE

    class FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [
            ("FileAttributes", wintypes.DWORD),
            ("ReparseTag", wintypes.DWORD),
        ]

    get_info = kernel32.GetFileInformationByHandleEx
    get_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    get_info.restype = wintypes.BOOL

    FILE_READ_ATTRIBUTES = 0x00000080
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    OPEN_EXISTING = 3
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    raw = create_file(
        str(path),
        FILE_READ_ATTRIBUTES,
        FILE_SHARE_READ | FILE_SHARE_WRITE,  # deliberately omit FILE_SHARE_DELETE
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if int(raw) == INVALID_HANDLE_VALUE:
        raise CapsuleError(
            f"artifact directory cannot be pinned safely: {path}: "
            f"WinError {ctypes.get_last_error()}"
        )

    pin = PinnedDirectory(path, handle=raw)
    try:
        tag = FileAttributeTagInfo()
        if not get_info(
            raw,
            FILE_ATTRIBUTE_TAG_INFO_CLASS,
            ctypes.byref(tag),
            ctypes.sizeof(tag),
        ):
            raise CapsuleError(
                f"artifact directory metadata cannot be validated: {path}: "
                f"WinError {ctypes.get_last_error()}"
            )
        if tag.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
            raise CapsuleError(f"artifact directory cannot be a reparse point: {path}")

        final_path = ntpath.normcase(
            ntpath.abspath(_strip_windows_device_prefix(_windows_final_path(int(raw))))
        )
        if final_path != expected:
            raise CapsuleError(
                f"artifact directory was redirected outside its pinned identity: {path}"
            )
        return pin
    except Exception:
        pin.close()
        raise


def _pin_posix_directory(path: Path) -> PinnedDirectory:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(str(path), os.O_RDONLY | directory | nofollow)
    except OSError as exc:
        raise CapsuleError(f"artifact directory cannot be pinned safely: {path}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise CapsuleError(f"artifact output path is not a directory: {path}")
        final = Path(os.path.realpath(path))
        if final != _lexical_absolute(path):
            raise CapsuleError(f"artifact directory is redirected: {path}")
        return PinnedDirectory(path, fd=fd, identity=(info.st_dev, info.st_ino))
    except Exception:
        os.close(fd)
        raise


def _pin_posix_child(parent: PinnedDirectory, name: str, path: Path) -> PinnedDirectory:
    """Create/open one child through an already-pinned parent directory FD."""
    if parent.fd is None:
        raise CapsuleError(f"artifact parent directory is no longer pinned: {parent.path}")
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise CapsuleError(f"invalid artifact directory component: {name!r}")
    try:
        os.mkdir(name, dir_fd=parent.fd)
    except FileExistsError:
        pass
    except OSError as exc:
        raise CapsuleError(f"artifact directory cannot be created safely: {path}: {exc}") from exc

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(name, flags, dir_fd=parent.fd)
    except OSError as exc:
        raise CapsuleError(f"artifact directory cannot be pinned safely: {path}: {exc}") from exc
    try:
        info = os.fstat(fd)
        entry = os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or not stat.S_ISDIR(entry.st_mode):
            raise CapsuleError(f"artifact output path is not a directory: {path}")
        identity = (info.st_dev, info.st_ino)
        if identity != (entry.st_dev, entry.st_ino):
            raise CapsuleError(f"artifact directory changed while being pinned: {path}")
        return PinnedDirectory(path, fd=fd, identity=identity)
    except Exception:
        os.close(fd)
        raise


@contextmanager
def pin_directory(path: Path) -> Iterator[PinnedDirectory]:
    lexical = _lexical_absolute(path)
    pin = _pin_windows_directory(lexical) if os.name == "nt" else _pin_posix_directory(lexical)
    try:
        yield pin
    finally:
        try:
            pin.assert_current()
        finally:
            pin.close()


@contextmanager
def _pin_child_directory(parent: PinnedDirectory, name: str, path: Path) -> Iterator[PinnedDirectory]:
    pin = _pin_posix_child(parent, name, path)
    try:
        yield pin
    finally:
        try:
            pin.assert_current()
        finally:
            pin.close()


@contextmanager
def pin_artifact_tree(output_dir: Path, relatives) -> Iterator[PinnedArtifactTree]:
    """Pin the full directory tree used by one collection transaction.

    ``output_dir`` is expected to be ``<runs>/<run>/artifacts``. On Windows,
    rename-denying handles keep the lexical tree stable. On POSIX, descendants
    are created/opened through their parent's pinned FD and all later file
    mutations use the corresponding parent FD, so pathname replacement cannot
    redirect artifact bytes.
    """
    output_dir = _lexical_absolute(Path(output_dir))
    run_dir = output_dir.parent
    runs_root = run_dir.parent

    if run_dir == runs_root or output_dir == run_dir:
        raise CapsuleError("invalid artifact output directory layout")

    normalized = [normalize_guest_relative_path(value) for value in relatives]

    with ExitStack() as stack:
        runs_pin = stack.enter_context(pin_directory(runs_root))
        parents: dict[str, PinnedDirectory] = {}

        if os.name == "nt":
            run_dir.mkdir(exist_ok=True)
            stack.enter_context(pin_directory(run_dir))

            output_dir.mkdir(exist_ok=True)
            output_pin = stack.enter_context(pin_directory(output_dir))
            parents[""] = output_pin

            pinned_parents = {""}
            for relative in normalized:
                parts = relative.split("/")[:-1]
                current = output_dir
                prefix = []
                for part in parts:
                    prefix.append(part)
                    key = "/".join(prefix).casefold()
                    current = current / part
                    if key in pinned_parents:
                        continue
                    current.mkdir(exist_ok=True)
                    parents[key] = stack.enter_context(pin_directory(current))
                    pinned_parents.add(key)
        else:
            run_pin = stack.enter_context(
                _pin_child_directory(runs_pin, run_dir.name, run_dir)
            )
            output_pin = stack.enter_context(
                _pin_child_directory(run_pin, output_dir.name, output_dir)
            )
            parents[""] = output_pin

            for relative in normalized:
                parts = relative.split("/")[:-1]
                parent_pin = output_pin
                current = output_dir
                prefix: list[str] = []
                for part in parts:
                    prefix.append(part)
                    key = "/".join(prefix)
                    current = current / part
                    existing = parents.get(key)
                    if existing is not None:
                        parent_pin = existing
                        continue
                    child = stack.enter_context(
                        _pin_child_directory(parent_pin, part, current)
                    )
                    parents[key] = child
                    parent_pin = child

        yield PinnedArtifactTree(output_dir, parents)
