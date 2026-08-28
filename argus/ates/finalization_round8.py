"""Round-8 canonical terminal/suppression validation for PR #22.

This layer validates producer-owned payloads that must never be treated as
opaque data during finalization:

* ARTIFACT_SUPPRESSED is reconstructed through the canonical suppression
  contract and may carry only the documented relationship fields;
* runtime.finalization_pending must carry one explicit supported producer
  result instead of silently accepting malformed/unknown values; and
* an ACTION_PROPOSED row that never reaches policy validation is an incomplete
  action lifecycle, just like the already-handled validated-without-dispatch
  state.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from .artifacts import ArtifactContext, ArtifactSuppression
from .core import EventType
from .ids import ArtifactId, FindingId, StepAttemptId

_SUPPORTED_EXECUTION_RESULTS = frozenset(
    {"pass", "fail", "error", "cancelled", "outcome_unknown"}
)
_SUPPRESSION_REQUIRED = frozenset(
    {"artifact_id", "context", "kind", "capture_policy", "reason", "step_attempt_id"}
)
_SUPPRESSION_ALLOWED = _SUPPRESSION_REQUIRED | frozenset(
    {"finding_id", "collection_ordinal"}
)


def _validate_suppression(payload: object, impl) -> None:
    if not isinstance(payload, Mapping):
        raise impl.FinalizationError("ARTIFACT_SUPPRESSED payload is malformed")

    keys = set(payload)
    missing = _SUPPRESSION_REQUIRED - keys
    unexpected = keys - _SUPPRESSION_ALLOWED
    if missing:
        raise impl.FinalizationError(
            "ARTIFACT_SUPPRESSED payload is missing required fields: "
            + ", ".join(sorted(missing))
        )
    if unexpected:
        # Fail before reports can copy an unclassified/plaintext extension field.
        raise impl.FinalizationError(
            "ARTIFACT_SUPPRESSED payload contains unexpected fields: "
            + ", ".join(sorted(str(item) for item in unexpected))
        )

    try:
        suppression = ArtifactSuppression(
            artifact_id=ArtifactId(payload["artifact_id"]),
            context=ArtifactContext(payload["context"]),
            kind=payload["kind"],
            capture_policy=payload["capture_policy"],
            reason=payload["reason"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise impl.FinalizationError(
            "ARTIFACT_SUPPRESSED payload violates the artifact suppression contract"
        ) from exc

    attempt_id = payload.get("step_attempt_id")
    if attempt_id is not None:
        try:
            StepAttemptId(attempt_id)
        except (TypeError, ValueError) as exc:
            raise impl.FinalizationError(
                "ARTIFACT_SUPPRESSED step_attempt_id is invalid"
            ) from exc

    finding_id = payload.get("finding_id")
    if finding_id is not None:
        try:
            FindingId(finding_id)
        except (TypeError, ValueError) as exc:
            raise impl.FinalizationError(
                "ARTIFACT_SUPPRESSED finding_id is invalid"
            ) from exc

    ordinal = payload.get("collection_ordinal")
    if ordinal is not None and (
        isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal <= 0
    ):
        raise impl.FinalizationError(
            "ARTIFACT_SUPPRESSED collection_ordinal must be a positive integer"
        )

    if suppression.kind == "collected_file":
        if suppression.context is not ArtifactContext.COLLECTED_FILE or ordinal is None:
            raise impl.FinalizationError(
                "collected-file suppression requires collected_file context and collection_ordinal"
            )
        if finding_id is not None:
            raise impl.FinalizationError(
                "collected-file suppression cannot claim a Finding relationship"
            )
    else:
        if suppression.context is ArtifactContext.COLLECTED_FILE or ordinal is not None:
            raise impl.FinalizationError(
                "screenshot suppression cannot carry collection metadata"
            )


def _validate_terminal_marker(payload: object, impl) -> None:
    if not isinstance(payload, Mapping):
        raise impl.FinalizationError("RUN_MARKED_INCOMPLETE payload is malformed")
    if payload.get("reason") != "runtime.finalization_pending":
        return
    value = payload.get("execution_result")
    if not isinstance(value, str):
        raise impl.FinalizationError(
            "runtime.finalization_pending requires a string execution_result"
        )
    normalized = value.strip().lower()
    if normalized not in _SUPPORTED_EXECUTION_RESULTS or value != normalized:
        raise impl.FinalizationError(
            "runtime.finalization_pending execution_result is unsupported"
        )


def _has_dangling_proposal(events) -> bool:
    states: dict[str, str] = {}
    for event in tuple(events):
        kind = event.envelope.event_type
        if kind in {
            EventType.ACTION_PROPOSED,
            EventType.ACTION_POLICY_VALIDATED,
            EventType.ACTION_DISPATCH_COMMITTED,
        }:
            action = event.payload.get("action")
            if not isinstance(action, Mapping):
                continue
            action_id = action.get("action_id")
            if not isinstance(action_id, str):
                continue
            if kind is EventType.ACTION_PROPOSED:
                states[action_id] = "proposed"
            elif kind is EventType.ACTION_POLICY_VALIDATED:
                states[action_id] = "validated"
            else:
                states[action_id] = "committed"
        elif kind in {EventType.ACTION_EXECUTED, EventType.ACTION_OUTCOME_UNKNOWN}:
            action_id = event.payload.get("action_id")
            if isinstance(action_id, str):
                states[action_id] = "terminal"
    return any(state == "proposed" for state in states.values())


def install(impl) -> None:
    previous_derive = impl._derive

    def derive(events, run_id):
        snapshot = tuple(events)
        for event in snapshot:
            if event.envelope.event_type is EventType.ARTIFACT_SUPPRESSED:
                _validate_suppression(event.payload, impl)
            elif event.envelope.event_type is EventType.RUN_MARKED_INCOMPLETE:
                _validate_terminal_marker(event.payload, impl)

        dangling_proposal = _has_dangling_proposal(snapshot)
        state = previous_derive(snapshot, run_id)
        if dangling_proposal and not state.status_inputs.execution_error:
            state = replace(
                state,
                status_inputs=replace(state.status_inputs, execution_error=True),
            )
        return state

    impl._derive = derive


__all__ = ["install"]
