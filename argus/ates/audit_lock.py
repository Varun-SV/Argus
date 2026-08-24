"""Cross-process serialization for detached approval/audit ledger appends."""
from __future__ import annotations

import os

from . import audit_impl as _impl
from .store import AtesStoreError, _PinnedDirectory, _WriterLock, _open_regular_file


def _locked_append_line(root, name: str, line: bytes) -> None:
    pin = None
    lock = None
    handle = None
    try:
        pin = _PinnedDirectory(root)
        lock = _WriterLock(pin)
        lock.assert_authoritative()
        handle, created = _open_regular_file(pin, name)
        handle.seek(0, os.SEEK_END)
        before = handle.tell()
        if before:
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) != b"\n":
                raise _impl.ApprovalError(f"{name} has an unterminated trailing record")
            handle.seek(0, os.SEEK_END)
        written = handle.write(line)
        if written != len(line):
            raise _impl.ApprovalError(f"short append to detached ledger {name}")
        handle.flush()
        os.fsync(handle.fileno())
        lock.assert_authoritative()
        pin.assert_file_identity(name, handle.fileno(), f"detached ledger {name}")
        if os.fstat(handle.fileno()).st_size != before + len(line):
            raise _impl.ApprovalError(f"detached ledger {name} changed during append")
        if created:
            pin.fsync()
        lock.assert_authoritative()
    except _impl.ApprovalError:
        raise
    except (OSError, AtesStoreError) as exc:
        raise _impl.ApprovalError(f"cannot append detached ledger {name} safely") from exc
    finally:
        if handle is not None:
            try:
                handle.close()
            except BaseException:
                pass
        if lock is not None:
            try:
                lock.close()
            except BaseException:
                pass
        if pin is not None:
            try:
                pin.close()
            except BaseException:
                pass


def install() -> None:
    _impl._append_line = _locked_append_line


__all__ = ["install"]
