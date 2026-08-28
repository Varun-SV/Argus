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

import re
from collections.abc import Mapping
from dataclasses import replace

from . import finalization_round3 as _round3
from .artifacts import ArtifactContext, ArtifactSuppression
from .core import EventType, EvidenceValue
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
_TARGET_ALLOWED = frozenset({"target"})
_ENVIRONMENT_REQUIRED = frozenset({"environment_type", "isolated"})
_INCOMPLETE_ALLOWED = frozenset({"reason", "execution_result"})
_SAFE_INCOMPLETE_REASON = re.compile(
    r"^[a-z][a-z0-9_]{0,31}(?:\.[a-z][a-z0-9_]{0,31}){1,7}$"
)


def _validate_suppression(payload: object, impl):
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
    return suppression, attempt_id, finding_id


def _validate_lifecycle_payloads(events, run_id, impl) -> None:
    starts = [
        event
        for event in events
        if event.envelope.event_type is EventType.RUN_STARTED
    ]
    if len(starts) != 1:
        raise impl.FinalizationError(
            "finalization requires exactly one RUN_STARTED event"
        )
    run = _round3._run_record(starts[0].payload.get("run"), run_id, impl)

    for event in events:
        kind = event.envelope.event_type
        if kind is EventType.ENVIRONMENT_PREPARED:
            payload = event.payload
            if set(payload) != _ENVIRONMENT_REQUIRED:
                raise impl.FinalizationError(
                    "ENVIRONMENT_PREPARED payload shape is invalid"
                )
            if payload.get("environment_type") != run.environment_type:
                raise impl.FinalizationError(
                    "ENVIRONMENT_PREPARED environment_type contradicts RUN_STARTED"
                )
            if not isinstance(payload.get("isolated"), bool):
                raise impl.FinalizationError(
                    "ENVIRONMENT_PREPARED isolated must be a boolean"
                )
        elif kind is EventType.TARGET_LAUNCHED:
            payload = event.payload
            if set(payload) != _TARGET_ALLOWED:
                raise impl.FinalizationError("TARGET_LAUNCHED payload shape is invalid")
            target = payload.get("target")
            if not isinstance(target, Mapping):
                raise impl.FinalizationError(
                    "TARGET_LAUNCHED target must be privacy-classified evidence"
                )
            allowed = {
                "disposition",
                "value",
                "reason",
                "secret_refs",
                "protected_ref",
            }
            if set(target) - allowed:
                raise impl.FinalizationError(
                    "TARGET_LAUNCHED target contains unexpected plaintext fields"
                )
            refs = target.get("secret_refs", ())
            if isinstance(refs, (str, bytes, bytearray, Mapping)):
                raise impl.FinalizationError(
                    "TARGET_LAUNCHED target secret_refs are malformed"
                )
            try:
                EvidenceValue(
                    disposition=target.get("disposition"),
                    value=target.get("value"),
                    reason=target.get("reason"),
                    secret_refs=tuple(refs),
                    protected_ref=target.get("protected_ref"),
                )
            except (TypeError, ValueError) as exc:
                raise impl.FinalizationError(
                    "TARGET_LAUNCHED target is invalid privacy-classified evidence"
                ) from exc


def _validate_suppression_relationships(events, impl) -> None:
    attempt_ids: set[str] = set()
    finding_ids: set[str] = set()
    retained_artifact_ids: set[str] = set()
    suppressions = []

    for event in events:
        kind = event.envelope.event_type
        if kind in {EventType.STEP_ATTEMPT_STARTED, EventType.STEP_ATTEMPT_COMPLETED}:
            attempt = event.payload.get("attempt")
            if isinstance(attempt, Mapping):
                attempt_id = attempt.get("step_attempt_id")
                if isinstance(attempt_id, str):
                    attempt_ids.add(attempt_id)
        elif kind is EventType.FINDING_RECORDED:
            finding = event.payload.get("finding")
            if isinstance(finding, Mapping):
                finding_id = finding.get("finding_id")
                if isinstance(finding_id, str):
                    finding_ids.add(finding_id)
        elif kind in {EventType.CHECKPOINT_CAPTURED, EventType.ARTIFACT_COLLECTED}:
            artifact = event.payload.get("artifact")
            if isinstance(artifact, Mapping):
                artifact_id = artifact.get("artifact_id")
                if isinstance(artifact_id, str):
                    retained_artifact_ids.add(artifact_id)
        elif kind is EventType.ARTIFACT_SUPPRESSED:
            suppressions.append(_validate_suppression(event.payload, impl))

    seen_artifact_ids = set(retained_artifact_ids)
    for suppression, attempt_id, finding_id in suppressions:
        artifact_id = str(suppression.artifact_id)
        if artifact_id in seen_artifact_ids:
            raise impl.FinalizationError(
                "artifact_id is duplicated across retained and suppressed outcomes"
            )
        seen_artifact_ids.add(artifact_id)
        if attempt_id is not None and str(attempt_id) not in attempt_ids:
            raise impl.FinalizationError(
                "ARTIFACT_SUPPRESSED references an unknown step_attempt_id"
            )
        if finding_id is not None and str(finding_id) not in finding_ids:
            raise impl.FinalizationError(
                "ARTIFACT_SUPPRESSED references an unknown finding_id"
            )


def _validate_terminal_marker(payload: object, impl) -> None:
    if not isinstance(payload, Mapping):
        raise impl.FinalizationError("RUN_MARKED_INCOMPLETE payload is malformed")
    unexpected = set(payload) - _INCOMPLETE_ALLOWED
    if unexpected:
        raise impl.FinalizationError(
            "RUN_MARKED_INCOMPLETE payload contains unexpected fields"
        )
    reason = payload.get("reason")
    if not isinstance(reason, str) or not _SAFE_INCOMPLETE_REASON.fullmatch(reason):
        raise impl.FinalizationError(
            "RUN_MARKED_INCOMPLETE reason must be a machine-safe reason code"
        )
    value = payload.get("execution_result")
    if reason != "runtime.finalization_pending" and value is None:
        return
    if not isinstance(value, str):
        raise impl.FinalizationError(
            "RUN_MARKED_INCOMPLETE requires a string execution_result"
        )
    normalized = value.strip().lower()
    if normalized not in _SUPPORTED_EXECUTION_RESULTS or value != normalized:
        raise impl.FinalizationError(
            "RUN_MARKED_INCOMPLETE execution_result is unsupported"
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
        _validate_lifecycle_payloads(snapshot, run_id, impl)
        _validate_suppression_relationships(snapshot, impl)
        for event in snapshot:
            if event.envelope.event_type is EventType.RUN_MARKED_INCOMPLETE:
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
