"""Canonical ATES final-status derivation and outcome revision selection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .core import AssertionResult, RunOutcomeRevision, RunStatus


@dataclass(frozen=True)
class StatusInputs:
    required_assertion_results: tuple[AssertionResult, ...] = ()
    required_steps_satisfied: bool = True
    required_assertions_satisfied: bool = True
    unresolved_action_outcome: bool = False
    evidence_integrity_error: bool = False
    execution_error: bool = False
    deterministic_failure: bool = False
    cancelled: bool = False

    def __post_init__(self) -> None:
        raw_results = self.required_assertion_results
        if (
            isinstance(raw_results, (str, bytes, bytearray, Mapping))
            or not isinstance(raw_results, Sequence)
        ):
            raise ValueError(
                "required_assertion_results must be a sequence of AssertionResult values"
            )
        try:
            result_snapshot = tuple(raw_results)
        except (RuntimeError, TypeError, ValueError) as exc:
            raise ValueError(
                "required_assertion_results could not be snapshotted safely"
            ) from exc

        normalized_results = []
        for result in result_snapshot:
            if isinstance(result, AssertionResult):
                normalized_results.append(result)
                continue
            try:
                normalized_results.append(AssertionResult(result))
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "required assertion results must be valid AssertionResult values"
                ) from exc
        object.__setattr__(self, "required_assertion_results", tuple(normalized_results))

        for field_name in (
            "required_steps_satisfied",
            "required_assertions_satisfied",
            "unresolved_action_outcome",
            "evidence_integrity_error",
            "execution_error",
            "deterministic_failure",
            "cancelled",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise ValueError(f"{field_name} must be a boolean")


def derive_run_status(inputs: StatusInputs) -> RunStatus:
    if not isinstance(inputs, StatusInputs):
        raise ValueError("status derivation requires validated StatusInputs")

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

    if (
        incomplete_assertion
        or not inputs.required_steps_satisfied
        or not inputs.required_assertions_satisfied
    ):
        return RunStatus.ERROR

    return RunStatus.PASSED


def effective_outcome(
    revisions: Sequence[RunOutcomeRevision],
    *,
    evidence_revision: int | None = None,
) -> RunOutcomeRevision:
    if (
        isinstance(revisions, (str, bytes, bytearray, Mapping))
        or not isinstance(revisions, Sequence)
    ):
        raise ValueError("finalization history must be a sequence of RunOutcomeRevision values")
    try:
        snapshot = tuple(revisions)
    except (RuntimeError, TypeError, ValueError) as exc:
        raise ValueError("finalization history could not be snapshotted safely") from exc
    if not snapshot:
        raise ValueError("at least one finalization revision is required")
    if not all(isinstance(item, RunOutcomeRevision) for item in snapshot):
        raise ValueError(
            "finalization history must contain only validated RunOutcomeRevision values"
        )
    if evidence_revision is not None and (
        isinstance(evidence_revision, bool)
        or not isinstance(evidence_revision, int)
        or evidence_revision < 1
    ):
        raise ValueError("evidence_revision must be a positive integer")

    ordered = sorted(snapshot, key=lambda item: item.revision)
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
            if item.finalized_at < previous.finalized_at:
                raise ValueError("re-finalization cannot precede the prior finalization time")
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
