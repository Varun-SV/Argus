"""Complete the derived/detached ATES v0.1 package after canonical finalization."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .audit import (
    ApprovalError,
    KeyResolver,
    ensure_detached_ledgers,
    record_finalization_audit,
)
from .finalization import FinalizationError, FinalizationResult
from .reports import ReportBundle, ReportError, render_reports

PACKAGE_COMPLETION_VERSION = "ates-package-completion-v1"


class PackageCompletionError(RuntimeError):
    """Canonical finalization succeeded but its required v0.1 package did not."""


@dataclass(frozen=True)
class CompletedRunPackage:
    finalization: FinalizationResult
    approvals_path: Path
    audit_path: Path
    reports: ReportBundle


def complete_run_package(
    finalization: FinalizationResult,
    *,
    approval_key_resolver: Optional[KeyResolver] = None,
) -> CompletedRunPackage:
    """Materialize detached ledgers and reproducible reports for a closed run.

    This function deliberately runs *after* canonical finalization. The
    approvals/audit ledgers and reports never mutate evidence.jsonl, run.json,
    or the immutable evidence/package manifest revision they discuss.
    """
    if not isinstance(finalization, FinalizationResult):
        raise ValueError("package completion requires a FinalizationResult")
    try:
        approvals, audit = ensure_detached_ledgers(finalization.run_dir)
        record_finalization_audit(finalization.run_dir)
        reports = render_reports(
            finalization.run_dir,
            approval_key_resolver=approval_key_resolver,
        )
    except (ApprovalError, ReportError, FinalizationError, OSError, ValueError) as exc:
        raise PackageCompletionError(
            f"required ATES v0.1 package completion failed: {exc}"
        ) from exc
    return CompletedRunPackage(finalization, approvals, audit, reports)


__all__ = [
    "PACKAGE_COMPLETION_VERSION",
    "CompletedRunPackage",
    "PackageCompletionError",
    "complete_run_package",
]
