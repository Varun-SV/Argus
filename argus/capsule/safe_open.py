"""Race-safe, read-only opens for Capsule transfer boundaries.

Transfer policy must bind authority to an opened object, not to a pathname that
can be replaced after validation. The helpers below walk from a trusted root,
reject redirects, validate the exact opened object, and optionally hold a
snapshot-stability lock while bytes are copied.
"""
from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator

from argus.capsule.base import CapsuleError
from argus.capsule.files import normalize_relative_path


def _require_regular_file(handle: BinaryIO, relative: str, purpose: str) -> None:
    info = os.fstat(handle.fileno())
    if not stat.S_ISREG(info.st_mode):
        raise CapsuleError(f"{purpose} is not a regular file: {relative}")


def _strip_windows_device_prefix(value: str) -> str:
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


def _windows_final_path(handle_value: int) -> str:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_final_path = kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    get_final_path.restype = wintypes.DWORD

    size = 32768
    buffer = ctypes.create_unicode_buffer(size)
    written = get_final_path(wintypes.HANDLE(handle_value), buffer, size, 0)
    if written == 0 or written >= size:
        raise OSError(ctypes.get_last_error(), "GetFinalPathNameByHandleW failed")
    return _strip_windows_device_prefix(buffer.value)


def _open_windows_rooted_file(root: Path, relative: str, purpose: str) -> BinaryIO:
    """Open a Windows file and validate the exact opened handle against root."""
    import ctypes
    import msvcrt
    import ntpath
    from ctypes import wintypes

    candidate = root.joinpath(*relative.split("/"))
    root_final = ntpath.normcase(ntpath.abspath(str(root.resolve(strict=True))))

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
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    class FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [
            ("FileAttributes", wintypes.DWORD),
            ("ReparseTag", wintypes.DWORD),
        ]

    get_info = kernel32.GetFileInformationByHandleEx
    get_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    get_info.restype = wintypes.BOOL

    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    FILE_SHARE_DELETE = 0x00000004
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x00000080
    FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    raw_handle = create_file(
        str(candidate),
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    handle_value = int(raw_handle)
    if handle_value == INVALID_HANDLE_VALUE:
        raise CapsuleError(
            f"{purpose} cannot be opened safely: {relative}: "
            f"WinError {ctypes.get_last_error()}"
        )

    transferred = False
    try:
        tag_info = FileAttributeTagInfo()
        if not get_info(
            raw_handle,
            FILE_ATTRIBUTE_TAG_INFO_CLASS,
            ctypes.byref(tag_info),
            ctypes.sizeof(tag_info),
        ):
            raise CapsuleError(
                f"{purpose} metadata cannot be validated: {relative}: "
                f"WinError {ctypes.get_last_error()}"
            )
        if tag_info.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
            raise CapsuleError(f"{purpose} cannot be a reparse point: {relative}")

        opened_final = ntpath.normcase(ntpath.abspath(_windows_final_path(handle_value)))
        try:
            common = ntpath.commonpath([root_final, opened_final])
        except ValueError as exc:
            raise CapsuleError(f"{purpose} escapes its trusted root: {relative}") from exc
        if common != root_final or opened_final == root_final:
            raise CapsuleError(f"{purpose} escapes its trusted root: {relative}")

        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        fd = msvcrt.open_osfhandle(handle_value, flags)
        transferred = True
        file_obj = os.fdopen(fd, "rb")
        try:
            _require_regular_file(file_obj, relative, purpose)
        except Exception:
            file_obj.close()
            raise
        return file_obj
    finally:
        if not transferred:
            close_handle(raw_handle)


def _open_posix_rooted_file(root: Path, relative: str, purpose: str) -> BinaryIO:
    """Walk from an opened root directory using no-follow openat calls."""
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise CapsuleError("race-safe rooted opens are unsupported on this platform")

    root_flags = os.O_RDONLY | directory | nofollow
    dir_fd = os.open(str(root), root_flags)
    opened_dirs = [dir_fd]
    file_fd = None
    try:
        parts = relative.split("/")
        current_fd = dir_fd
        for part in parts[:-1]:
            next_fd = os.open(part, root_flags, dir_fd=current_fd)
            opened_dirs.append(next_fd)
            current_fd = next_fd

        file_fd = os.open(parts[-1], os.O_RDONLY | nofollow, dir_fd=current_fd)
        file_obj = os.fdopen(file_fd, "rb")
        file_fd = None
        try:
            _require_regular_file(file_obj, relative, purpose)
        except Exception:
            file_obj.close()
            raise
        return file_obj
    except OSError as exc:
        raise CapsuleError(f"{purpose} cannot be opened safely: {relative}: {exc}") from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        for opened in reversed(opened_dirs):
            try:
                os.close(opened)
            except OSError:
                pass


@contextmanager
def open_rooted_regular_file(
    root: Path,
    relative: str,
    *,
    purpose: str = "requested file",
) -> Iterator[BinaryIO]:
    """Yield a regular file bound to a race-safe handle beneath ``root``."""
    relative = normalize_relative_path(relative)
    try:
        handle = (
            _open_windows_rooted_file(root, relative, purpose)
            if os.name == "nt"
            else _open_posix_rooted_file(root, relative, purpose)
        )
    except CapsuleError:
        raise
    except OSError as exc:
        raise CapsuleError(f"{purpose} cannot be opened safely: {relative}: {exc}") from exc

    try:
        yield handle
    finally:
        handle.close()


@contextmanager
def open_workspace_regular_file(root: Path, relative: str) -> Iterator[BinaryIO]:
    """Yield a regular guest-workspace artifact bound to a safe read handle."""
    with open_rooted_regular_file(
        root,
        relative,
        purpose="requested artifact",
    ) as handle:
        yield handle


@contextmanager
def open_project_regular_file(root: Path, relative: str) -> Iterator[BinaryIO]:
    """Yield one staged project source bound before hashing/uploading."""
    with open_rooted_regular_file(
        root,
        relative,
        purpose="staging source",
    ) as handle:
        yield handle


@contextmanager
def stabilize_snapshot_read(handle: BinaryIO, relative: str) -> Iterator[None]:
    """Hold the strongest available mutation barrier while snapshotting.

    The current production Capsule is a Windows Hyper-V guest. There we take an
    exclusive byte-range lock over the entire 64-bit file range, which makes
    ordinary writes through other handles fail while the snapshot is copied.
    POSIX CI/future providers additionally take an advisory exclusive flock;
    collection also performs a second bounded read/digest verification so an
    uncooperative same-size writer is detected rather than silently accepted.
    """
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class OVERLAPPED(ctypes.Structure):
            _fields_ = [
                ("Internal", ctypes.c_void_p),
                ("InternalHigh", ctypes.c_void_p),
                ("Offset", wintypes.DWORD),
                ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        lock_file = kernel32.LockFileEx
        lock_file.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(OVERLAPPED),
        ]
        lock_file.restype = wintypes.BOOL
        unlock_file = kernel32.UnlockFileEx
        unlock_file.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(OVERLAPPED),
        ]
        unlock_file.restype = wintypes.BOOL

        LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
        LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
        os_handle = wintypes.HANDLE(msvcrt.get_osfhandle(handle.fileno()))
        overlapped = OVERLAPPED()
        if not lock_file(
            os_handle,
            LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY,
            0,
            0xFFFFFFFF,
            0xFFFFFFFF,
            ctypes.byref(overlapped),
        ):
            raise CapsuleError(
                f"artifact cannot be stabilized for snapshotting: {relative}: "
                f"WinError {ctypes.get_last_error()}"
            )
        try:
            yield
        finally:
            unlock_file(
                os_handle,
                0,
                0xFFFFFFFF,
                0xFFFFFFFF,
                ctypes.byref(overlapped),
            )
        return

    try:
        import fcntl
    except ImportError:
        yield
        return

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise CapsuleError(
            f"artifact cannot be stabilized for snapshotting: {relative}: {exc}"
        ) from exc
    try:
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
