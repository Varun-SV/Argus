"""Pinned host-output directories for Capsule artifact collection.

The Hyper-V host is Windows, where an opened directory handle that omits
FILE_SHARE_DELETE prevents another same-user process from renaming/replacing the
trusted run/artifact directories while collection is in progress. POSIX handles
are retained and identity-checked as a fail-closed compatibility path for tests
and future providers.
"""
from __future__ import annotations

import os
import stat
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
def pin_artifact_tree(output_dir: Path, relatives) -> Iterator[Path]:
    """Pin the full directory tree used by one collection transaction.

    ``output_dir`` is expected to be ``<runs>/<run>/artifacts``. The trusted
    runs root is pinned first, then the run directory, artifacts directory, and
    every declared parent directory. No artifact bytes are written until all
    these boundaries have been opened and attested.
    """
    output_dir = _lexical_absolute(Path(output_dir))
    run_dir = output_dir.parent
    runs_root = run_dir.parent

    if run_dir == runs_root or output_dir == run_dir:
        raise CapsuleError("invalid artifact output directory layout")

    normalized = [normalize_guest_relative_path(value) for value in relatives]

    with ExitStack() as stack:
        stack.enter_context(pin_directory(runs_root))

        run_dir.mkdir(exist_ok=True)
        stack.enter_context(pin_directory(run_dir))

        output_dir.mkdir(exist_ok=True)
        stack.enter_context(pin_directory(output_dir))

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
                stack.enter_context(pin_directory(current))
                pinned_parents.add(key)

        yield output_dir
