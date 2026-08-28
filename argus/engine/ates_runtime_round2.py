"""Runtime lifecycle ordering hardening for PR #22.

Closing the underlying target may legitimately happen while a teardown/roam
attempt is still active, but canonical ATES must record the attempt terminal
before TARGET_CLOSED.  Defer only the evidence event; the actual adapter close
still happens at the original call site.
"""
from __future__ import annotations

from .ates_runtime import AtesRuntimeRecorder

_INSTALLED = False


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    previous_target_closed = AtesRuntimeRecorder.target_closed
    previous_complete_current = AtesRuntimeRecorder.complete_current

    def target_closed(self: AtesRuntimeRecorder) -> None:
        if self.current_attempt_id is not None:
            # The physical target is already closed by AtesAdapterProxy. Delay
            # only canonical close evidence until its owning attempt has a
            # durable terminal record.
            self._target_close_evidence_pending = True
            return
        previous_target_closed(self)
        self._target_close_evidence_pending = False

    def complete_current(self: AtesRuntimeRecorder, status: str):
        attempt_id = previous_complete_current(self, status)
        if getattr(self, "_target_close_evidence_pending", False):
            # previous_complete_current has durably emitted STEP_ATTEMPT_COMPLETED
            # and cleared the active attempt before this close is recorded.
            previous_target_closed(self)
            self._target_close_evidence_pending = False
        return attempt_id

    AtesRuntimeRecorder.target_closed = target_closed
    AtesRuntimeRecorder.complete_current = complete_current


__all__ = ["install"]
