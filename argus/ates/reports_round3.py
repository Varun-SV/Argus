"""Round-3 report freshness semantics for PR #22.

Rendered files are derived artifacts and therefore never self-attest that they
are currently regenerated-verified.  Instead they disclose the exact detached
approval/audit byte snapshot they were rendered from; the verifier may grant
REGENERATED_VERIFIED only after comparing those bytes with a fresh regeneration.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Mapping

from . import reports_runtime as _runtime
from .finalization import FinalizationTrustState

_base_model = _runtime._model
_base_manifest = _runtime._manifest


def _member_snapshot(root: Path, name: str) -> dict[str, object]:
    path = root / name
    # Missing detached ledgers are a valid read-only state immediately after
    # canonical finalization.  Distinguish true absence from an unsafe existing
    # filesystem object (including dangling symlinks) without creating anything.
    if not os.path.lexists(path):
        return {
            "path": name,
            "state": "absent",
            "size_bytes": 0,
            "sha256": None,
        }

    raw = _runtime._pinned_bytes(root, name, f"detached ledger snapshot {name}")
    return {
        "path": name,
        "state": "present",
        "size_bytes": len(raw),
        "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
    }


def _detached_snapshot(root: Path) -> dict[str, object]:
    return {
        "snapshot_version": "ates-detached-ledger-snapshot-v1",
        "members": [
            _member_snapshot(root, "approvals.jsonl"),
            _member_snapshot(root, "audit.jsonl"),
        ],
        "freshness_note": (
            "These entries identify the detached-ledger state used to render this report. "
            "A member may be explicitly absent; present-member digests do not prove the files "
            "are still current. Call verify_report_bundle()."
        ),
    }


def _model(root: Path, approval_key_resolver):
    model = _base_model(root, approval_key_resolver)
    snapshot = _detached_snapshot(root)

    # A file cannot prove its own freshness.  Only the active verifier can grant
    # regenerated_verified after regenerating against the current ledgers.
    model["report_trust_state"] = FinalizationTrustState.UNVERIFIED_DERIVED.value
    model["detached_ledger_snapshot"] = snapshot

    # Markdown/HTML already render the source/integrity section, so place the
    # snapshot there as well to make the byte binding visible in every rich view.
    source = model.get("source")
    if isinstance(source, Mapping):
        source_copy = dict(source)
        source_copy["detached_ledger_snapshot"] = snapshot
        model["source"] = source_copy
    return model


def _manifest(root: Path, members):
    manifest = _base_manifest(root, members)
    manifest["detached_ledger_snapshot"] = _detached_snapshot(root)
    return manifest


def install() -> None:
    _runtime._model = _model
    _runtime._manifest = _manifest


__all__ = ["install"]
