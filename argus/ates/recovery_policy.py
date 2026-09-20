"""Fresh-recovery policy guards for ATES finalization.

Recovery may delegate to canonical finalization only while the same writer
transaction still proves that the producer handed off a completion-ready
terminal marker.  The context flag below scopes that stricter rule to the
fresh-recovery path; ordinary explicit finalization keeps its existing API.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from . import finalization
from .enums import EventType
from .errors import FinalizationError
from .trust_guards import _FRESH_FINALIZATION_HANDOFF_REASONS

_fresh_recovery: ContextVar[bool] = ContextVar("ates_fresh_recovery", default=False)
_installed = False


def _terminal_handoff_is_completion_ready(store: Any) -> bool:
    events = tuple(store.events)
    if not events:
        return False
    terminal = events[-1]
    if terminal.event_type is not EventType.RUN_MARKED_INCOMPLETE:
        return False
    return terminal.payload.get("reason") in _FRESH_FINALIZATION_HANDOFF_REASONS


def install() -> None:
    """Keep fresh-recovery classification and finalization under one lock."""

    global _installed
    if _installed:
        return

    recover = finalization._recover_unbound_revision
    finalize = finalization.finalize_revision_one

    def guarded_recover(*args: Any, **kwargs: Any):
        token = _fresh_recovery.set(True)
        try:
            return recover(*args, **kwargs)
        finally:
            _fresh_recovery.reset(token)

    def guarded_finalize(store: Any, *args: Any, **kwargs: Any):
        # transaction_guards invokes finalization while its AtesEventStore
        # writer lock is still held.  Revalidate the producer handoff here,
        # immediately before mutation, so a lock-release/reacquire race cannot
        # turn an interrupted run into RUN_COMPLETED.
        if _fresh_recovery.get() and not _terminal_handoff_is_completion_ready(store):
            raise FinalizationError(
                "fresh recovery requires a completion-ready terminal handoff"
            )
        return finalize(store, *args, **kwargs)

    finalization._recover_unbound_revision = guarded_recover
    finalization.finalize_revision_one = guarded_finalize
    _installed = True
