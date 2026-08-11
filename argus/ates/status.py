"""Canonical ATES final-status derivation and outcome revision selection."""

from __future__ import annotations

from dataclasses import dataclass

from .core import AssertionResult, RunOutcomeRevision, RunStatus


@dataclass(frozen=True)
class StatusInputs:
    required_assertion_results: tuple[AssertionResult, ...] = ()
    required_steps_satisfied: bool = True
    unresolved_action_outcome: bool = False
    evidence_integrity_error: bool = False
    execution_error: bool = False
    deterministic_failure: bool = False
    cancelled: bool = False


def derive_run_status(inputs: StatusInputs) -> RunStatus:
    assertion_error = any(
        result is AssertionResult.ERROR for result in inputs.required_assertion_results
    )
    incomplete_assertion = any(
        result in (AssertionResult.UNEVALUATED, AssertionResult.SKIPPED)
        for result in inputs.required_assertion_results
    )
    if (
        inputs.unresolved_action_outcome
        or inputs.evidence_integrity_error
        or inputs.execution_error
        or assertion_error
    ):
        return RunStatus.ERROR

    if inputs.deterministic_failure or any(
        result is AssertionResult.FAILED for result in inputs.required_assertion_results
    ):
        return RunStatus.FAILED

    if inputs.cancelled:
        return RunStatus.CANCELLED

    if incomplete_assertion or not inputs.required_steps_satisfied:
        return RunStatus.ERROR

    return RunStatus.PASSED


def effective_outcome(
    revisions: tuple[RunOutcomeRevision, ...],
    *,
    evidence_revision: int | None = None,
) -> RunOutcomeRevision:
    if not revisions:
        raise ValueError("at least one finalization revision is required")
    if evidence_revision is not None and evidence_revision < 1:
        raise ValueError("evidence_revision must be >= 1")

    ordered = sorted(revisions, key=lambda item: item.revision)
    run_id = ordered[0].run_id
    if ordered[0].revision != 1:
        raise ValueError("finalization history must start at revision 1")

    previous = None
    seen_ids = set()
    for expected_revision, item in enumerate(ordered, start=1):
        if item.run_id != run_id:
            raise ValueError("finalization history cannot mix run IDs")
        if item.revision != expected_revision:
            raise ValueError("finalization revisions must be contiguous")
        if item.finalization_id in seen_ids:
            raise ValueError("finalization IDs must be unique")
        if previous is not None:
            if item.supersedes_finalization_id != previous.finalization_id:
                raise ValueError("re-finalization must supersede the immediately prior finalization")
            if item.evidence_revision <= previous.evidence_revision:
                raise ValueError("re-finalization must bind a newer evidence revision")
        seen_ids.add(item.finalization_id)
        previous = item

    if evidence_revision is None:
        return ordered[-1]

    applicable = [
        item for item in ordered if item.evidence_revision <= evidence_revision
    ]
    if not applicable:
        raise ValueError("no finalization applies to the requested evidence revision")
    return applicable[-1]
