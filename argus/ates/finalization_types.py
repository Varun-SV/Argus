"""Shared finalization results, trust states, and format identities."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .core import RunOutcomeRevision

MANIFEST_VERSION = "ates-manifest-v1"
PACKAGE_MANIFEST_VERSION = "ates-package-manifest-v1"
FINALIZATION_BINDING_VERSION = "ates-finalization-binding-v1"
EVIDENCE_DIGEST_PROFILE = "ates-canonical-evidence-jsonl-v1"



class FinalizationError(RuntimeError):
    """Canonical evidence cannot be finalized safely."""


class FinalizationTrustState(str, Enum):
    REGENERATED_VERIFIED = "regenerated_verified"
    BOUND_VERIFIED = "bound_verified"
    UNVERIFIED_DERIVED = "unverified_derived"
    INVALID = "invalid"


@dataclass(frozen=True)
class FinalizationResult:
    outcome: RunOutcomeRevision
    run_dir: Path
    evidence_manifest_path: Path
    package_manifest_path: Path
    binding_path: Path
    trust_state: FinalizationTrustState


def _finalization_error(message: str, cause: BaseException | None = None):
    error = FinalizationError(message)
    if cause is not None:
        raise error from cause
    raise error
