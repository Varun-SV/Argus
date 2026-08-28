"""Round-5 recovery existence guard for PR #22.

Recovery is a reconciliation operation over an already-existing canonical run.
A syntactically valid but absent RunId must never reach the create-capable
AtesEventStore constructor used by the older recovery implementation.
"""
from __future__ import annotations

from . import finalization_round3 as _round3
from . import finalization_round4 as _round4
from .store import _run_directory_key


def install() -> None:
    from . import finalization_impl as impl

    previous_recover = impl.recover_revision_one

    def recover(project_dir, run_id):
        project, rid = _round4._normalize_project_and_run_id(project_dir, run_id, impl)
        root = project / ".argus" / "runs" / _run_directory_key(rid)
        if not root.exists():
            _round3._finalization_error(
                impl,
                "cannot recover an absent ATES run",
            )
        # Delegate only after proving the canonical namespace already exists.
        # All bound-state routing, partial-tail repair, and exact crash-state
        # preflight remain owned by the previously installed recovery layers.
        return previous_recover(project, rid)

    impl.recover_revision_one = recover

    # Install the next canonical-validation layer after every prior derive shim
    # is in place so tombstones/findings are checked on all finalize/recovery
    # derivations without bypassing the existing lifecycle hardening.
    from .finalization_round6 import install as _install_round6

    _install_round6(impl)

    # Round 7 closes sibling lifecycle/artifact trust gaps after the tombstone
    # and Finding shape checks are installed.
    from .finalization_round7 import install as _install_round7

    _install_round7(impl)

    # Round 8 validates suppressed-artifact payloads, producer terminal markers,
    # and the remaining proposal-only action lifecycle before status is trusted.
    from .finalization_round8 import install as _install_round8

    _install_round8(impl)


__all__ = ["install"]
