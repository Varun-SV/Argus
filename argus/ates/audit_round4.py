"""Round-4 detached-ledger crash recovery hardening for PR #22.

Detached ledgers remain strict for ordinary readers.  Only a writer that already
holds the canonical run-scoped authority may reconcile an unterminated trailing
record, and reconciliation truncates exactly to the last durable newline before
strict JSONL validation resumes.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Optional, Tuple

from . import audit_impl as _impl
from . import audit_round2 as _round2
from .store import AtesStoreError, _PinnedDirectory, _WriterLock, _open_regular_file

_base_ledger_transaction = _round2._ledger_transaction
_base_read_jsonl = _impl._read_jsonl

# Context-local authority prevents read-only validators from ever repairing
# bytes.  A repair is possible only while the round-2 transaction lock is held.
_ACTIVE_AUTHORITY: ContextVar[
    Optional[Tuple[Path, _PinnedDirectory, _WriterLock]]
] = ContextVar("ates_detached_ledger_authority", default=None)


def _same_root(left: Path, right: Path) -> bool:
    try:
        return left.resolve(strict=True) == right.resolve(strict=True)
    except OSError:
        return False


def _repair_trailing_partial_held(
    pin: _PinnedDirectory,
    lock: _WriterLock,
    name: str,
) -> None:
    """Truncate only an unterminated final JSONL record under writer authority."""
    if name not in {"approvals.jsonl", "audit.jsonl"}:
        raise _impl.ApprovalError("detached-ledger tail repair was requested for an unsupported file")

    handle = None
    try:
        lock.assert_authoritative()
        handle, created = _open_regular_file(pin, name)
        pin.assert_file_identity(name, handle.fileno(), f"detached ledger {name}")
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        if size == 0:
            if created:
                pin.fsync()
            return

        handle.seek(-1, os.SEEK_END)
        if handle.read(1) == b"\n":
            return

        # The only repair contract is the final unterminated record.  Earlier
        # newline-terminated bytes are preserved byte-for-byte.
        handle.seek(0)
        raw = handle.read()
        truncate_to = raw.rfind(b"\n") + 1
        handle.truncate(truncate_to)
        handle.flush()
        os.fsync(handle.fileno())

        lock.assert_authoritative()
        pin.assert_file_identity(name, handle.fileno(), f"detached ledger {name}")
        if os.fstat(handle.fileno()).st_size != truncate_to:
            raise _impl.ApprovalError(
                f"detached ledger {name} tail repair did not reach the expected boundary"
            )
        # Directory durability is cheap here and also covers the edge case where
        # the ledger file itself was created by the interrupted operation.
        pin.fsync()
        lock.assert_authoritative()
    except _impl.ApprovalError:
        raise
    except (OSError, AtesStoreError) as exc:
        raise _impl.ApprovalError(
            f"cannot reconcile unterminated detached ledger {name} safely"
        ) from exc
    finally:
        if handle is not None:
            try:
                handle.close()
            except BaseException:
                pass


@contextmanager
def _ledger_transaction(root: Path):
    """Expose held writer authority only for the lifetime of one ledger transaction."""
    with _base_ledger_transaction(root) as (pin, lock):
        token = _ACTIVE_AUTHORITY.set((root, pin, lock))
        try:
            yield pin, lock
        finally:
            _ACTIVE_AUTHORITY.reset(token)


def _read_jsonl(root: Path | str, name: str):
    authority = _ACTIVE_AUTHORITY.get()
    if authority is not None and name in {"approvals.jsonl", "audit.jsonl"}:
        authority_root, pin, lock = authority
        requested_root = Path(root)
        if _same_root(authority_root, requested_root):
            _repair_trailing_partial_held(pin, lock, name)
    # Parsing remains the original strict parser.  A non-final malformed record,
    # duplicate JSON key, invalid UTF-8, etc. still fails closed.
    return _base_read_jsonl(root, name)


def install() -> None:
    # Round 3 calls these two module attributes dynamically, so replacing them
    # upgrades both approval and audit retry paths without creating a sibling
    # implementation that could drift from the established idempotency rules.
    _round2._ledger_transaction = _ledger_transaction
    _impl._read_jsonl = _read_jsonl


__all__ = ["install"]
