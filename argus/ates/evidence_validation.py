"""Canonical evidence validation and terminal status derivation.

All finalization entry points use this explicit validation pipeline. Capture
privacy, identity/provenance, action/attempt lifecycle, and effective status
remain separate checks without import-time replacement of earlier validators.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from .artifacts import (
    ARTIFACT_BYTES_PROFILE,
    ArtifactContext,
    ArtifactSuppression,
    PROTECTED_ARTIFACT_COMMITMENT_PROFILE,
    PROTECTED_ARTIFACT_VERIFICATION_REF,
)
from .core import (
    ActionRecord,
    ArtifactRecord,
    AssertionRecord,
    AssertionResult,
    EventType,
    EvidenceDisposition,
    EvidenceValue,
    ExecutionKind,
    FindingRecord,
    ObservationRecord,
    RequirementIdentity,
    RoamSource,
    RunRecord,
    ScriptedSource,
    SourceCommitment,
    StepAttemptRecord,
    StepAttemptStatus,
    StepRecord,
    to_json_compatible,
    validate_artifact_path,
    validate_step_attempt_history,
    validate_step_evidence_relationships,
)
from .finalization_types import FinalizationError, _finalization_error
from .ids import ArtifactId, FindingId, ObservationId, RunId, StepAttemptId
from .status import StatusInputs
from .store import StoredEvent



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


_EVIDENCE_FIELDS = frozenset(
    {"disposition", "value", "reason", "secret_refs", "protected_ref"}
)


_COMMITMENT_FIELDS = frozenset(
    {"method", "value", "canonicalization_profile", "verification_ref"}
)


_STEP_FIELDS = frozenset({"step_id", "instruction", "kind"})


_ATTEMPT_FIELDS = frozenset(
    {"step_attempt_id", "step_id", "attempt", "status", "started_at", "ended_at", "retry_reason"}
)


_ACTION_FIELDS = frozenset(
    {"action_id", "step_id", "step_attempt_id", "action_type", "parameters", "operation_id"}
)


_OBSERVATION_FIELDS = frozenset(
    {"observation_id", "step_attempt_id", "source", "captured_at", "capture_policy", "facts"}
)


_REQUIREMENT_FIELDS = frozenset(
    {"requirement_id", "source_system", "version", "source_revision", "commitment"}
)


_ASSERTION_FIELDS = frozenset(
    {
        "assertion_id", "step_id", "step_attempt_id", "kind", "expected", "result",
        "method", "observation_id", "actual", "required", "requirement",
    }
)


_FINDING_FIELDS = frozenset(
    {"finding_id", "title", "description", "evidence_refs", "classification_source", "classification"}
)


_ARTIFACT_FIELDS = frozenset(
    {
        "artifact_id", "kind", "path", "sensitivity", "capture_policy", "content_digest",
        "size_bytes", "protection_state", "protected_ref", "access_policy",
        "retention_policy", "authorization_ref",
    }
)


_CHECKPOINT_REQUIRED = frozenset({"artifact", "context", "step_attempt_id"})


_CHECKPOINT_ALLOWED = _CHECKPOINT_REQUIRED | frozenset({"finding_id"})


_COLLECTED_FIELDS = frozenset({"artifact", "collection_ordinal"})


_CAPSULE_FIELDS = frozenset({"retained"})


@dataclass(frozen=True)
class _EvidenceState:
    run_id: RunId
    steps: tuple[Mapping[str, object], ...]
    final_attempt_by_step: Mapping[str, Mapping[str, object]]
    final_attempt_id_by_step: Mapping[str, str]
    assertions: tuple[Mapping[str, object], ...]
    artifacts: tuple[Mapping[str, object], ...]
    status_inputs: StatusInputs


def _canonical_json_equal(left: object, right: object) -> bool:
    """Compare JSON values without Python's boolean/number equality coercion."""
    try:
        return json.dumps(
            to_json_compatible(left),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ) == json.dumps(
            to_json_compatible(right),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise FinalizationError("evidence value is not canonical JSON") from exc


def _time(value, label):
    if not isinstance(value, str): raise FinalizationError(f"{label} must be an ISO-8601 string")
    try: return datetime.fromisoformat(value)
    except ValueError as exc: raise FinalizationError(f"{label} is not a valid ISO-8601 timestamp") from exc


def _evidence(value):
    if value is None: return None
    if not isinstance(value, Mapping): raise FinalizationError("step attempt retry_reason is malformed")
    refs = value.get("secret_refs", ())
    if isinstance(refs, (str, bytes, bytearray, Mapping)): raise FinalizationError("retry_reason secret_refs are malformed")
    try:
        return EvidenceValue(
            disposition=value.get("disposition"), value=value.get("value"),
            reason=value.get("reason"), secret_refs=tuple(refs),
            protected_ref=value.get("protected_ref"),
        )
    except (TypeError, ValueError) as exc: raise FinalizationError("step attempt retry_reason is invalid") from exc


def _attempt(value, *, running):
    if not isinstance(value, Mapping): raise FinalizationError("step-attempt payload is malformed")
    try:
        rec = StepAttemptRecord(
            step_attempt_id=value["step_attempt_id"], step_id=value["step_id"],
            attempt=value["attempt"], status=value["status"],
            started_at=_time(value["started_at"], "step attempt started_at"),
            ended_at=None if value.get("ended_at") is None else _time(value.get("ended_at"), "step attempt ended_at"),
            retry_reason=_evidence(value.get("retry_reason")),
        )
    except (KeyError, TypeError, ValueError) as exc: raise FinalizationError("step-attempt evidence is invalid") from exc
    if running != (rec.status is StepAttemptStatus.RUNNING):
        raise FinalizationError("step-attempt event type/status disagree")
    return rec


def _validate_attempts(events, run_id=None):
    events = tuple(events)
    if run_id is None:
        if not events:
            raise FinalizationError("cannot validate an empty ATES attempt stream")
        run_id = events[0].run_id
    starts = [e for e in events if e.envelope.event_type is EventType.RUN_STARTED]
    if len(starts) != 1: return
    raw_steps = starts[0].payload.get("steps")
    if isinstance(raw_steps, (str, bytes, bytearray, Mapping)) or not isinstance(raw_steps, Sequence): return
    step_ids = {x.get("step_id") for x in raw_steps if isinstance(x, Mapping) and isinstance(x.get("step_id"), str)}
    opened, closed, schedules, active = {}, {}, {}, None
    for event in events:
        if event.run_id != run_id: raise FinalizationError("finalization event history mixes run IDs")
        t = event.envelope.event_type
        if t is EventType.STEP_RETRY_SCHEDULED:
            sid, prev, nxt, ordinal = (event.payload.get(k) for k in ("step_id", "previous_step_attempt_id", "next_step_attempt_id", "next_attempt"))
            prior = closed.get(prev) if isinstance(prev, str) else None
            if (active is not None or not isinstance(sid, str) or sid not in step_ids or not isinstance(nxt, str)
                or not nxt or isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 2
                or nxt in schedules or nxt in opened or prior is None):
                raise FinalizationError("STEP_RETRY_SCHEDULED payload/causality is invalid")
            prior_rec, prior_seq = prior
            if str(prior_rec.step_id) != sid or ordinal != prior_rec.attempt + 1 or prior_seq >= event.sequence:
                raise FinalizationError("retry scheduling causality is invalid")
            schedules[nxt] = (sid, ordinal, event.sequence)
        elif t is EventType.STEP_ATTEMPT_STARTED:
            rec = _attempt(event.payload.get("attempt"), running=True)
            aid, sid = str(rec.step_attempt_id), str(rec.step_id)
            if sid not in step_ids or aid in opened or aid in closed or active is not None:
                raise FinalizationError("started step-attempt identity/lifecycle is invalid")
            if rec.attempt > 1:
                scheduled = schedules.get(aid)
                if scheduled is None or scheduled[0] != sid or scheduled[1] != rec.attempt or scheduled[2] >= event.sequence:
                    raise FinalizationError("retry start does not match STEP_RETRY_SCHEDULED")
            elif aid in schedules: raise FinalizationError("first attempt cannot be retry-scheduled")
            opened[aid] = (rec, event.sequence); active = aid
        elif t is EventType.STEP_ATTEMPT_COMPLETED:
            rec = _attempt(event.payload.get("attempt"), running=False)
            aid = str(rec.step_attempt_id); start = opened.get(aid)
            if start is None: raise FinalizationError("step attempt completed without a matching start")
            if aid in closed or active != aid: raise FinalizationError("step attempt completion lifecycle is invalid")
            srec, sseq = start
            if (
                rec.step_id != srec.step_id
                or rec.attempt != srec.attempt
                or rec.started_at != srec.started_at
                or not _canonical_json_equal(rec.retry_reason, srec.retry_reason)
                or sseq >= event.sequence
            ):
                raise FinalizationError("step attempt completion does not match its start")
            closed[aid] = (rec, event.sequence); active = None
    if active is not None or set(opened) != set(closed): raise FinalizationError("canonical history contains an unfinished step attempt")
    retry_ids = {aid for aid, (rec, _) in opened.items() if rec.attempt > 1}
    if set(schedules) != retry_ids: raise FinalizationError("canonical history contains an orphan/missing retry schedule")
    records = tuple(rec for rec, _ in sorted(closed.values(), key=lambda x: x[1]))
    try: validate_step_attempt_history(records)
    except ValueError as exc: raise FinalizationError(f"step attempt history is invalid: {exc}") from exc


def _evidence_value(value: object, label: str) -> EvidenceValue:
    if not isinstance(value, Mapping):
        raise FinalizationError(f"{label} is malformed")
    refs = value.get("secret_refs", ())
    if isinstance(refs, (str, bytes, bytearray, Mapping)) or not isinstance(refs, Sequence):
        raise FinalizationError(f"{label} secret_refs are malformed")
    try:
        return EvidenceValue(
            disposition=value.get("disposition"),
            value=value.get("value"),
            reason=value.get("reason"),
            secret_refs=tuple(refs),
            protected_ref=value.get("protected_ref"),
        )
    except (TypeError, ValueError) as exc:
        raise FinalizationError(f"{label} is invalid") from exc


def _source_commitment(value: object, label: str):
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise FinalizationError(f"{label} is malformed")
    try:
        return SourceCommitment(
            method=value["method"],
            value=value["value"],
            canonicalization_profile=value.get("canonicalization_profile"),
            verification_ref=value.get("verification_ref"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise FinalizationError(f"{label} is invalid") from exc


def _requirement(value: object):
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise FinalizationError("assertion requirement is malformed")
    try:
        return RequirementIdentity(
            requirement_id=value["requirement_id"],
            source_system=value["source_system"],
            version=value.get("version"),
            source_revision=value.get("source_revision"),
            commitment=_source_commitment(
                value.get("commitment"), "assertion requirement commitment"
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise FinalizationError("assertion requirement is invalid") from exc


def _step_record(value: object):
    if not isinstance(value, Mapping):
        raise FinalizationError("RUN_STARTED step is malformed")
    missing_instruction = "instruction" not in value
    try:
        instruction = (
            EvidenceValue.suppressed("evidence.missing_step_instruction")
            if missing_instruction
            else _evidence_value(value["instruction"], "step instruction")
        )
        record = StepRecord(
            step_id=value["step_id"],
            instruction=instruction,
            kind=value["kind"],
        )
        return record, missing_instruction
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, FinalizationError):
            raise
        raise FinalizationError("RUN_STARTED step is invalid") from exc


def _action_record(value: object, label: str) -> ActionRecord:
    if not isinstance(value, Mapping):
        raise FinalizationError(f"{label} action is malformed")
    parameters = value.get("parameters")
    if not isinstance(parameters, Mapping):
        raise FinalizationError(f"{label} action parameters are malformed")
    try:
        return ActionRecord(
            action_id=value["action_id"],
            step_id=value["step_id"],
            step_attempt_id=value["step_attempt_id"],
            action_type=value["action_type"],
            parameters={
                key: _evidence_value(item, f"{label} action parameter {key}")
                for key, item in parameters.items()
            },
            operation_id=value.get("operation_id"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, FinalizationError):
            raise
        raise FinalizationError(f"{label} action is invalid") from exc


def _observation_record(value: object) -> ObservationRecord:
    if not isinstance(value, Mapping):
        raise FinalizationError("observation is malformed")
    facts = value.get("facts")
    if not isinstance(facts, Mapping):
        raise FinalizationError("observation facts are malformed")
    try:
        return ObservationRecord(
            observation_id=value["observation_id"],
            step_attempt_id=value["step_attempt_id"],
            source=value["source"],
            captured_at=_time(value["captured_at"], "observation captured_at"),
            capture_policy=value["capture_policy"],
            facts={
                key: _evidence_value(item, f"observation fact {key}")
                for key, item in facts.items()
            },
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, FinalizationError):
            raise
        raise FinalizationError("observation is invalid") from exc


def _assertion_record(value: object) -> AssertionRecord:
    if not isinstance(value, Mapping):
        raise FinalizationError("assertion is malformed")
    actual = value.get("actual")
    try:
        return AssertionRecord(
            assertion_id=value["assertion_id"],
            step_id=value["step_id"],
            step_attempt_id=value["step_attempt_id"],
            kind=value["kind"],
            expected=_evidence_value(value["expected"], "assertion expected"),
            result=value["result"],
            method=value["method"],
            observation_id=value.get("observation_id"),
            actual=None
            if actual is None
            else _evidence_value(actual, "assertion actual"),
            required=value.get("required", True),
            requirement=_requirement(value.get("requirement")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, FinalizationError):
            raise
        raise FinalizationError("assertion is invalid") from exc


def _same_action_identity(left: ActionRecord, right: ActionRecord) -> bool:
    return (
        left.action_id == right.action_id
        and left.step_id == right.step_id
        and left.step_attempt_id == right.step_attempt_id
        and left.action_type == right.action_type
        and left.operation_id == right.operation_id
    )


def _validate_relationships(events, run_id):
    snapshot = tuple(events)
    if not snapshot:
        raise FinalizationError("cannot finalize an empty ATES event stream")
    if snapshot[0].envelope.event_type is not EventType.RUN_STARTED:
        raise FinalizationError("RUN_STARTED must be the first canonical event")

    run_started = [
        event
        for event in snapshot
        if event.envelope.event_type is EventType.RUN_STARTED
    ]
    if len(run_started) != 1:
        raise FinalizationError(
            "finalization requires exactly one RUN_STARTED event"
        )
    raw_steps = run_started[0].payload.get("steps")
    if (
        isinstance(raw_steps, (str, bytes, bytearray, Mapping))
        or not isinstance(raw_steps, Sequence)
    ):
        raise FinalizationError("RUN_STARTED steps are malformed")
    parsed_steps = tuple(_step_record(item) for item in tuple(raw_steps))
    steps = tuple(item[0] for item in parsed_steps)
    step_kind_by_id = {str(step.step_id): step.kind for step in steps}
    structural_integrity_error = any(item[1] for item in parsed_steps)

    # Preserve the more precise attempt-lifecycle diagnostics before applying
    # the broader target/action/relationship state machines.
    _validate_attempts(snapshot)

    active_attempt = None
    terminal_attempts = []
    observations = []
    assertions = []
    proposals = {}
    action_states = {}
    unresolved_dispatch = False
    lifecycle_error = False

    environment_prepared = None
    target_launched = None
    target_closed = None
    environment_released = None

    for event in snapshot:
        if event.run_id != run_id:
            raise FinalizationError(
                "finalization event history mixes run IDs"
            )
        kind = event.envelope.event_type

        if kind is EventType.RUN_STARTED:
            continue

        if kind is EventType.ENVIRONMENT_PREPARED:
            if (
                environment_prepared is not None
                or target_launched is not None
                or active_attempt is not None
                or environment_released is not None
            ):
                raise FinalizationError(
                    "environment preparation lifecycle is invalid"
                )
            environment_prepared = event.sequence
            continue

        if kind is EventType.TARGET_LAUNCHED:
            if (
                environment_prepared is None
                or target_launched is not None
                or target_closed is not None
                or environment_released is not None
            ):
                raise FinalizationError("target launch lifecycle is invalid")
            target_launched = event.sequence
            continue

        if kind is EventType.STEP_ATTEMPT_STARTED:
            record = _attempt(event.payload.get("attempt"), running=True)
            starts_before_launch = (
                target_launched is None
                and step_kind_by_id.get(str(record.step_id)) != "roam"
            )
            if (
                environment_prepared is None
                or starts_before_launch
                or target_closed is not None
                or environment_released is not None
                or active_attempt is not None
            ):
                raise FinalizationError(
                    "step attempt started outside an active target lifecycle"
                )
            active_attempt = str(record.step_attempt_id)
            continue

        if kind is EventType.STEP_ATTEMPT_COMPLETED:
            record = _attempt(event.payload.get("attempt"), running=False)
            if active_attempt != str(record.step_attempt_id):
                raise FinalizationError(
                    "step attempt completion does not own the active attempt"
                )
            terminal_attempts.append(record)
            active_attempt = None
            continue

        if kind is EventType.OBSERVATION_CAPTURED:
            record = _observation_record(event.payload.get("observation"))
            if target_launched is None or target_closed is not None:
                raise FinalizationError(
                    "observation occurred outside an active target lifecycle"
                )
            if active_attempt != str(record.step_attempt_id):
                raise FinalizationError(
                    "observation occurred outside its active step attempt"
                )
            observations.append(record)
            continue

        if kind in {
            EventType.ACTION_PROPOSED,
            EventType.ACTION_POLICY_VALIDATED,
            EventType.ACTION_DISPATCH_COMMITTED,
        }:
            label = {
                EventType.ACTION_PROPOSED: "proposed",
                EventType.ACTION_POLICY_VALIDATED: "policy-validated",
                EventType.ACTION_DISPATCH_COMMITTED: "dispatch-committed",
            }[kind]
            record = _action_record(event.payload.get("action"), label)
            action_id = str(record.action_id)
            if target_launched is None or target_closed is not None:
                raise FinalizationError(
                    f"{label} action occurred outside an active target lifecycle"
                )
            if active_attempt != str(record.step_attempt_id):
                raise FinalizationError(
                    f"{label} action occurred outside its active step attempt"
                )

            if kind is EventType.ACTION_PROPOSED:
                if action_id in action_states:
                    raise FinalizationError(
                        "canonical action_id was proposed more than once"
                    )
                proposals[action_id] = record
                action_states[action_id] = ("proposed", record, event.sequence)
                continue

            prior = action_states.get(action_id)
            if prior is None or not _same_action_identity(prior[1], record):
                raise FinalizationError(
                    f"{label} action does not match its proposal identity"
                )
            if kind is EventType.ACTION_POLICY_VALIDATED:
                if prior[0] != "proposed":
                    raise FinalizationError(
                        "action policy validation lifecycle is invalid"
                    )
                action_states[action_id] = (
                    "validated",
                    record,
                    event.sequence,
                )
            else:
                if prior[0] != "validated" or record.operation_id is None:
                    raise FinalizationError(
                        "action dispatch commit lifecycle is invalid"
                    )
                if not _canonical_json_equal(prior[1], record):
                    raise FinalizationError(
                        "action dispatch commit differs from policy-validated action"
                    )
                action_states[action_id] = (
                    "committed",
                    record,
                    event.sequence,
                )
            continue

        if kind in {EventType.ACTION_EXECUTED, EventType.ACTION_OUTCOME_UNKNOWN}:
            action_id = event.payload.get("action_id")
            operation_id = event.payload.get("operation_id")
            if not isinstance(action_id, str) or not action_id:
                raise FinalizationError("action terminal identity is invalid")
            prior = action_states.get(action_id)
            if prior is None or prior[0] != "committed":
                structural_integrity_error = True
                unresolved_dispatch = True
                continue
            record = prior[1]
            expected_operation = (
                str(record.operation_id) if record.operation_id is not None else None
            )
            if operation_id != expected_operation:
                raise FinalizationError(
                    "action terminal operation_id does not match dispatch commit"
                )
            if active_attempt != str(record.step_attempt_id):
                raise FinalizationError(
                    "action terminal event occurred outside its active step attempt"
                )
            if kind is EventType.ACTION_OUTCOME_UNKNOWN:
                error_value = event.payload.get("error")
                if error_value is not None:
                    _evidence_value(error_value, "action outcome error")
                unresolved_dispatch = True
                state = "unknown"
            else:
                if event.payload.get("result") != "executed":
                    raise FinalizationError(
                        "ACTION_EXECUTED result is invalid"
                    )
                state = "executed"
            action_states[action_id] = (state, record, event.sequence)
            continue

        if kind is EventType.ASSERTION_EVALUATED:
            record = _assertion_record(event.payload.get("assertion"))
            if target_launched is None or target_closed is not None:
                raise FinalizationError(
                    "assertion occurred outside an active target lifecycle"
                )
            if active_attempt != str(record.step_attempt_id):
                raise FinalizationError(
                    "assertion occurred outside its active step attempt"
                )
            assertions.append(record)
            continue

        if kind is EventType.TARGET_CLOSED:
            if (
                target_launched is None
                or target_closed is not None
                or environment_released is not None
            ):
                raise FinalizationError("target close lifecycle is invalid")
            target_closed = event.sequence
            continue

        if kind is EventType.ENVIRONMENT_RELEASED:
            if (
                environment_prepared is None
                or environment_released is not None
                or active_attempt is not None
            ):
                raise FinalizationError(
                    "environment release lifecycle is invalid"
                )
            if target_launched is not None and target_closed is None:
                lifecycle_error = True
            environment_released = event.sequence
            continue

        if kind is EventType.RUN_MARKED_INCOMPLETE:
            if environment_released is None or active_attempt is not None:
                raise FinalizationError(
                    "RUN_MARKED_INCOMPLETE precedes environment release"
                )
            continue

        if kind is EventType.RUN_COMPLETED:
            raise FinalizationError(
                "pre-finalization history already contains RUN_COMPLETED"
            )

    if environment_prepared is None:
        raise FinalizationError("canonical history lacks ENVIRONMENT_PREPARED")
    if environment_released is None:
        lifecycle_error = True
    if target_launched is None:
        lifecycle_error = True
    elif target_closed is None:
        lifecycle_error = True

    for state, _record, _sequence in action_states.values():
        if state == "committed":
            unresolved_dispatch = True
        elif state == "validated":
            lifecycle_error = True

    try:
        validate_step_evidence_relationships(
            terminal_attempts,
            steps=steps,
            actions=tuple(proposals.values()),
            observations=observations,
            assertions=assertions,
        )
    except ValueError as exc:
        raise FinalizationError(
            f"canonical step evidence relationships are invalid: {exc}"
        ) from exc

    return unresolved_dispatch, lifecycle_error, structural_integrity_error


def _run_source_commitment(raw: object, label: str) -> SourceCommitment | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        _finalization_error(f"{label} is malformed")
    try:
        return SourceCommitment(
            method=raw["method"],
            value=raw["value"],
            canonicalization_profile=raw.get("canonicalization_profile"),
            verification_ref=raw.get("verification_ref"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        _finalization_error(f"{label} is invalid", exc)


def _run_record(raw: object, expected_run_id: RunId) -> RunRecord:
    if not isinstance(raw, Mapping):
        _finalization_error("RUN_STARTED run record is malformed")
    source_raw = raw.get("source")
    if not isinstance(source_raw, Mapping):
        _finalization_error("RUN_STARTED run source is malformed")
    try:
        execution_kind = ExecutionKind(raw["execution_kind"])
        if execution_kind is ExecutionKind.SCRIPTED:
            source = ScriptedSource(
                test_case_id=source_raw["test_case_id"],
                commitment=_run_source_commitment(
                    source_raw.get("commitment"),
                    "scripted source commitment",

                ),
            )
        else:
            source = RoamSource(
                objective_present=source_raw["objective_present"],
                objective_commitment=_run_source_commitment(
                    source_raw.get("objective_commitment"),
                    "roam objective commitment",

                ),
                config_commitment=_run_source_commitment(
                    source_raw.get("config_commitment"),
                    "roam config commitment",

                ),
                policy_ref=source_raw.get("policy_ref"),
            )
        configuration_commitment = _run_source_commitment(
            raw.get("configuration_commitment"),
            "run configuration commitment",

        )
        if configuration_commitment is None:
            raise ValueError("configuration commitment is required")
        record = RunRecord(
            run_id=raw["run_id"],
            execution_kind=execution_kind,
            source=source,
            started_at=_time(raw["started_at"], "run started_at"),
            argus_version=raw["argus_version"],
            adapter_type=raw["adapter_type"],
            environment_type=raw["environment_type"],
            evidence_profile=raw["evidence_profile"],
            configuration_commitment=configuration_commitment,
            provider=raw.get("provider"),
            model_provider=raw.get("model_provider"),
            model=raw.get("model"),
        )
    except FinalizationError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        _finalization_error("RUN_STARTED run record is invalid", exc)

    if record.run_id != expected_run_id:
        _finalization_error(
            "RUN_STARTED run_id does not match the canonical event envelope"
        )
    try:
        persisted = to_json_compatible(raw)
        normalized = to_json_compatible(record)
    except ValueError as exc:
        _finalization_error("RUN_STARTED run record is not JSON-safe", exc)
    if persisted != normalized:
        _finalization_error(
            "RUN_STARTED run record is not in canonical RunRecord form"
        )
    return record


def _validate_provenance_and_terminal_lifecycle(events, run_id: RunId) -> RunRecord:
    snapshot = tuple(events)
    starts = [
        event
        for event in snapshot
        if event.envelope.event_type is EventType.RUN_STARTED
    ]
    if len(starts) != 1:
        _finalization_error(
            "finalization requires exactly one RUN_STARTED event"
        )
    run = _run_record(starts[0].payload.get("run"), run_id)

    raw_steps = starts[0].payload.get("steps")
    if (
        isinstance(raw_steps, (str, bytes, bytearray, Mapping))
        or not isinstance(raw_steps, Sequence)
    ):
        _finalization_error("RUN_STARTED steps are malformed")
    step_kinds: list[str] = []
    for item in tuple(raw_steps):
        if not isinstance(item, Mapping):
            _finalization_error("RUN_STARTED steps must contain objects")
        kind = item.get("kind")
        if not isinstance(kind, str) or not kind.strip():
            _finalization_error("RUN_STARTED step kind is invalid")
        step_kinds.append(kind)

    if run.execution_kind is ExecutionKind.SCRIPTED and "roam" in step_kinds:
        _finalization_error("scripted runs cannot declare roam steps")
    if run.execution_kind is ExecutionKind.ROAM and (
        len(step_kinds) != 1 or step_kinds[0] != "roam"
    ):
        _finalization_error(
            "roam runs must declare exactly one canonical roam step"
        )

    target_launched = False
    target_closed = False
    for event in snapshot:
        if event.run_id != run_id:
            _finalization_error(
                "finalization event history mixes run IDs"
            )
        kind = event.envelope.event_type
        if kind is EventType.TARGET_LAUNCHED:
            target_launched = True
            continue
        if kind is EventType.TARGET_CLOSED:
            target_closed = True
            continue
        if (
            kind is EventType.STEP_ATTEMPT_STARTED
            and not target_launched
            and run.execution_kind is not ExecutionKind.ROAM
        ):
            _finalization_error(
                "scripted step attempt started before TARGET_LAUNCHED"
            )
        if kind in {EventType.ACTION_EXECUTED, EventType.ACTION_OUTCOME_UNKNOWN}:
            if not target_launched or target_closed:
                _finalization_error(

                    "action terminal event occurred outside an active target lifecycle",
                )
    return run


def _has_effective_attempt_execution_error(events) -> bool:
    final_by_step: dict[str, tuple[int, StepAttemptStatus]] = {}
    for event in events:
        if event.envelope.event_type is not EventType.STEP_ATTEMPT_COMPLETED:
            continue
        attempt = event.payload.get("attempt")
        if not isinstance(attempt, Mapping):
            continue
        step_id = attempt.get("step_id")
        ordinal = attempt.get("attempt")
        try:
            status = StepAttemptStatus(attempt.get("status"))
        except (TypeError, ValueError):
            continue
        if (
            not isinstance(step_id, str)
            or isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
        ):
            continue
        previous = final_by_step.get(step_id)
        if previous is None or ordinal > previous[0]:
            final_by_step[step_id] = (ordinal, status)
    return any(
        status in {StepAttemptStatus.ERROR, StepAttemptStatus.OUTCOME_UNKNOWN}
        for _, status in final_by_step.values()
    )


def _validate_finding(value: object) -> None:
    if not isinstance(value, Mapping):
        raise FinalizationError("FINDING_RECORDED finding is malformed")
    refs = value.get("evidence_refs", ())
    if isinstance(refs, (str, bytes, bytearray, Mapping)) or not isinstance(refs, Sequence):
        raise FinalizationError("FINDING_RECORDED evidence_refs are malformed")
    try:
        FindingRecord(
            finding_id=value["finding_id"],
            title=_evidence_value(value["title"], "finding title"),
            description=_evidence_value(value["description"], "finding description"),
            evidence_refs=tuple(refs),
            classification_source=value.get("classification_source", "model"),
            classification=value.get("classification", "unclassified"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, FinalizationError):
            raise
        raise FinalizationError("FINDING_RECORDED finding is invalid") from exc


def _validate_ignored_event_shapes(events) -> None:
    for event in tuple(events):
        kind = event.envelope.event_type
        if kind is EventType.SEQUENCE_TOMBSTONE:
            reason = event.payload.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise FinalizationError(
                    "SEQUENCE_TOMBSTONE requires a non-empty reason"
                )
        elif kind is EventType.FINDING_RECORDED:
            _validate_finding(event.payload.get("finding"))


def _artifact_commitment(value: object) -> SourceCommitment:
    if not isinstance(value, Mapping):
        raise FinalizationError("artifact content commitment is malformed")
    try:
        return SourceCommitment(
            method=value["method"],
            value=value["value"],
            canonicalization_profile=value.get("canonicalization_profile"),
            verification_ref=value.get("verification_ref"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise FinalizationError("artifact content commitment is invalid") from exc


def _artifact_record(value: object) -> ArtifactRecord:
    if not isinstance(value, Mapping):
        raise FinalizationError("retained artifact metadata is malformed")
    try:
        record = ArtifactRecord(
            artifact_id=value["artifact_id"],
            kind=value["kind"],
            path=value["path"],
            sensitivity=value["sensitivity"],
            capture_policy=value["capture_policy"],
            content_digest=_artifact_commitment(value.get("content_digest")),
            size_bytes=value["size_bytes"],
            protection_state=value["protection_state"],
            protected_ref=value.get("protected_ref"),
            access_policy=value.get("access_policy"),
            retention_policy=value.get("retention_policy"),
            authorization_ref=value.get("authorization_ref"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, FinalizationError):
            raise
        raise FinalizationError("retained artifact metadata is invalid") from exc

    commitment = record.content_digest
    if record.protection_state is EvidenceDisposition.PROTECTED_REF:
        if (
            commitment.method != "hmac-sha256"
            or commitment.canonicalization_profile
            != PROTECTED_ARTIFACT_COMMITMENT_PROFILE
            or commitment.verification_ref
            != PROTECTED_ARTIFACT_VERIFICATION_REF
        ):
            raise FinalizationError(
                "protected retained artifact requires the protected HMAC commitment profile"
            )
    elif (
        commitment.method != "sha256"
        or commitment.canonicalization_profile != ARTIFACT_BYTES_PROFILE
        or commitment.verification_ref is not None
    ):
        raise FinalizationError(
            "ordinary retained artifact requires the canonical artifact SHA-256 profile"
        )
    return record


def _active_close_and_independent_lifecycle(events) -> bool:
    """Validate close timing and detect lifecycle errors independent of retention."""
    active_attempt: str | None = None
    action_states: dict[str, str] = {}

    for event in tuple(events):
        kind = event.envelope.event_type
        if kind is EventType.STEP_ATTEMPT_STARTED:
            attempt = event.payload.get("attempt")
            if isinstance(attempt, Mapping):
                attempt_id = attempt.get("step_attempt_id")
                if isinstance(attempt_id, str):
                    active_attempt = attempt_id
        elif kind is EventType.STEP_ATTEMPT_COMPLETED:
            attempt = event.payload.get("attempt")
            if isinstance(attempt, Mapping):
                attempt_id = attempt.get("step_attempt_id")
                if isinstance(attempt_id, str) and active_attempt == attempt_id:
                    active_attempt = None
        elif kind is EventType.TARGET_CLOSED:
            if active_attempt is not None:
                raise FinalizationError(
                    "TARGET_CLOSED cannot occur while a step attempt is active; "
                    "action terminal event occurred outside an active target lifecycle"
                )
        elif kind in {
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
                action_states[action_id] = "proposed"
            elif kind is EventType.ACTION_POLICY_VALIDATED:
                action_states[action_id] = "validated"
            else:
                action_states[action_id] = "committed"
        elif kind in {EventType.ACTION_EXECUTED, EventType.ACTION_OUTCOME_UNKNOWN}:
            action_id = event.payload.get("action_id")
            if isinstance(action_id, str):
                action_states[action_id] = "terminal"

    # The existing relationship validator deliberately treats a policy-validated
    # action with no durable dispatch decision as an execution lifecycle error.
    # Failure-Capsule retention may excuse a missing TARGET_CLOSED only; it must
    # not erase this independent contribution.
    return any(state == "validated" for state in action_states.values())


def _validate_suppression(payload: object):
    if not isinstance(payload, Mapping):
        raise FinalizationError("ARTIFACT_SUPPRESSED payload is malformed")

    keys = set(payload)
    missing = _SUPPRESSION_REQUIRED - keys
    unexpected = keys - _SUPPRESSION_ALLOWED
    if missing:
        raise FinalizationError(
            "ARTIFACT_SUPPRESSED payload is missing required fields: "
            + ", ".join(sorted(missing))
        )
    if unexpected:
        # Fail before reports can copy an unclassified/plaintext extension field.
        raise FinalizationError(
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
        raise FinalizationError(
            "ARTIFACT_SUPPRESSED payload violates the artifact suppression contract"
        ) from exc

    attempt_id = payload.get("step_attempt_id")
    if attempt_id is not None:
        try:
            StepAttemptId(attempt_id)
        except (TypeError, ValueError) as exc:
            raise FinalizationError(
                "ARTIFACT_SUPPRESSED step_attempt_id is invalid"
            ) from exc

    finding_id = payload.get("finding_id")
    if finding_id is not None:
        try:
            FindingId(finding_id)
        except (TypeError, ValueError) as exc:
            raise FinalizationError(
                "ARTIFACT_SUPPRESSED finding_id is invalid"
            ) from exc

    ordinal = payload.get("collection_ordinal")
    if ordinal is not None and (
        isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal <= 0
    ):
        raise FinalizationError(
            "ARTIFACT_SUPPRESSED collection_ordinal must be a positive integer"
        )

    if suppression.kind == "collected_file":
        if suppression.context is not ArtifactContext.COLLECTED_FILE or ordinal is None:
            raise FinalizationError(
                "collected-file suppression requires collected_file context and collection_ordinal"
            )
        if finding_id is not None:
            raise FinalizationError(
                "collected-file suppression cannot claim a Finding relationship"
            )
    else:
        if suppression.context is ArtifactContext.COLLECTED_FILE or ordinal is not None:
            raise FinalizationError(
                "screenshot suppression cannot carry collection metadata"
            )
    return suppression, attempt_id, finding_id


def _validate_lifecycle_payloads(events, run_id) -> None:
    starts = [
        event
        for event in events
        if event.envelope.event_type is EventType.RUN_STARTED
    ]
    if len(starts) != 1:
        raise FinalizationError(
            "finalization requires exactly one RUN_STARTED event"
        )
    run = _run_record(starts[0].payload.get("run"), run_id)

    for event in events:
        kind = event.envelope.event_type
        if kind is EventType.ENVIRONMENT_PREPARED:
            payload = event.payload
            if set(payload) != _ENVIRONMENT_REQUIRED:
                raise FinalizationError(
                    "ENVIRONMENT_PREPARED payload shape is invalid"
                )
            if payload.get("environment_type") != run.environment_type:
                raise FinalizationError(
                    "ENVIRONMENT_PREPARED environment_type contradicts RUN_STARTED"
                )
            if not isinstance(payload.get("isolated"), bool):
                raise FinalizationError(
                    "ENVIRONMENT_PREPARED isolated must be a boolean"
                )
        elif kind is EventType.TARGET_LAUNCHED:
            payload = event.payload
            if set(payload) != _TARGET_ALLOWED:
                raise FinalizationError("TARGET_LAUNCHED payload shape is invalid")
            target = payload.get("target")
            if not isinstance(target, Mapping):
                raise FinalizationError(
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
                raise FinalizationError(
                    "TARGET_LAUNCHED target contains unexpected plaintext fields"
                )
            refs = target.get("secret_refs", ())
            if isinstance(refs, (str, bytes, bytearray, Mapping)):
                raise FinalizationError(
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
                raise FinalizationError(
                    "TARGET_LAUNCHED target is invalid privacy-classified evidence"
                ) from exc


def _validate_suppression_relationships(events) -> None:
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
            suppressions.append(_validate_suppression(event.payload))

    seen_artifact_ids = set(retained_artifact_ids)
    for suppression, attempt_id, finding_id in suppressions:
        artifact_id = str(suppression.artifact_id)
        if artifact_id in seen_artifact_ids:
            raise FinalizationError(
                "artifact_id is duplicated across retained and suppressed outcomes"
            )
        seen_artifact_ids.add(artifact_id)
        if attempt_id is not None and str(attempt_id) not in attempt_ids:
            raise FinalizationError(
                "ARTIFACT_SUPPRESSED references an unknown step_attempt_id"
            )
        if finding_id is not None and str(finding_id) not in finding_ids:
            raise FinalizationError(
                "ARTIFACT_SUPPRESSED references an unknown finding_id"
            )


def _validate_terminal_marker(payload: object) -> None:
    if not isinstance(payload, Mapping):
        raise FinalizationError("RUN_MARKED_INCOMPLETE payload is malformed")
    unexpected = set(payload) - _INCOMPLETE_ALLOWED
    if unexpected:
        raise FinalizationError(
            "RUN_MARKED_INCOMPLETE payload contains unexpected fields"
        )
    reason = payload.get("reason")
    if not isinstance(reason, str) or not _SAFE_INCOMPLETE_REASON.fullmatch(reason):
        raise FinalizationError(
            "RUN_MARKED_INCOMPLETE reason must be a machine-safe reason code"
        )
    value = payload.get("execution_result")
    if reason != "runtime.finalization_pending" and value is None:
        return
    if not isinstance(value, str):
        raise FinalizationError(
            "RUN_MARKED_INCOMPLETE requires a string execution_result"
        )
    normalized = value.strip().lower()
    if normalized not in _SUPPORTED_EXECUTION_RESULTS or value != normalized:
        raise FinalizationError(
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


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise FinalizationError(f"{label} is malformed")
    return value


def _no_extensions(value: object, allowed: frozenset[str], label: str):
    record = _mapping(value, label)
    extra = set(record) - allowed
    if extra:
        raise FinalizationError(
            f"{label} contains unexpected fields: " + ", ".join(sorted(str(item) for item in extra))
        )
    return record


def _schema_evidence(value: object, label: str) -> EvidenceValue:
    raw = _no_extensions(value, _EVIDENCE_FIELDS, label)
    refs = raw.get("secret_refs", ())
    if isinstance(refs, (str, bytes, bytearray, Mapping)) or not isinstance(refs, Sequence):
        raise FinalizationError(f"{label} secret_refs are malformed")
    try:
        return EvidenceValue(
            disposition=raw.get("disposition"),
            value=raw.get("value"),
            reason=raw.get("reason"),
            secret_refs=tuple(refs),
            protected_ref=raw.get("protected_ref"),
        )
    except (TypeError, ValueError) as exc:
        raise FinalizationError(f"{label} is invalid") from exc


def _validate_commitment_fields(value: object, label: str) -> None:
    if value is None:
        return
    _no_extensions(value, _COMMITMENT_FIELDS, label)


def _validate_record_extensions(events) -> None:
    for event in events:
        kind = event.envelope.event_type
        if kind is EventType.RUN_STARTED:
            steps = event.payload.get("steps")
            if isinstance(steps, (str, bytes, bytearray, Mapping)) or not isinstance(steps, Sequence):
                raise FinalizationError("RUN_STARTED steps are malformed")
            for raw in tuple(steps):
                step = _no_extensions(raw, _STEP_FIELDS, "RUN_STARTED step")
                if "instruction" in step:
                    _schema_evidence(step.get("instruction"), "step instruction")
        elif kind in {EventType.STEP_ATTEMPT_STARTED, EventType.STEP_ATTEMPT_COMPLETED}:
            attempt = _no_extensions(event.payload.get("attempt"), _ATTEMPT_FIELDS, "step attempt")
            if attempt.get("retry_reason") is not None:
                _schema_evidence(attempt.get("retry_reason"), "step attempt retry_reason")
        elif kind in {
            EventType.ACTION_PROPOSED,
            EventType.ACTION_POLICY_VALIDATED,
            EventType.ACTION_DISPATCH_COMMITTED,
        }:
            action = _no_extensions(event.payload.get("action"), _ACTION_FIELDS, "action record")
            parameters = _mapping(action.get("parameters"), "action parameters")
            for name, item in parameters.items():
                _schema_evidence(item, f"action parameter {name}")
        elif kind is EventType.OBSERVATION_CAPTURED:
            observation = _no_extensions(
                event.payload.get("observation"), _OBSERVATION_FIELDS, "observation record"
            )
            facts = _mapping(observation.get("facts"), "observation facts")
            for name, item in facts.items():
                _schema_evidence(item, f"observation fact {name}")
        elif kind is EventType.ASSERTION_EVALUATED:
            assertion = _no_extensions(
                event.payload.get("assertion"), _ASSERTION_FIELDS, "assertion record"
            )
            _schema_evidence(assertion.get("expected"), "assertion expected")
            if assertion.get("actual") is not None:
                _schema_evidence(assertion.get("actual"), "assertion actual")
            requirement = assertion.get("requirement")
            if requirement is not None:
                req = _no_extensions(requirement, _REQUIREMENT_FIELDS, "assertion requirement")
                _validate_commitment_fields(req.get("commitment"), "assertion requirement commitment")
        elif kind is EventType.FINDING_RECORDED:
            finding = _no_extensions(
                event.payload.get("finding"), _FINDING_FIELDS, "FINDING_RECORDED finding"
            )
            _schema_evidence(finding.get("title"), "finding title")
            _schema_evidence(finding.get("description"), "finding description")
        elif kind in {EventType.CHECKPOINT_CAPTURED, EventType.ARTIFACT_COLLECTED}:
            artifact = _no_extensions(
                event.payload.get("artifact"), _ARTIFACT_FIELDS, "retained artifact record"
            )
            _validate_commitment_fields(artifact.get("content_digest"), "artifact content commitment")
        elif kind is EventType.FAILURE_CAPSULE_RETAINED:
            payload = _no_extensions(event.payload, _CAPSULE_FIELDS, "FAILURE_CAPSULE_RETAINED payload")
            if payload.get("retained") is not True:
                raise FinalizationError("FAILURE_CAPSULE_RETAINED retained must be true")


def _canonical_relationship_sets(events):
    attempts: set[str] = set()
    observations: set[str] = set()
    retained_artifacts: set[str] = set()
    findings: set[str] = set()
    finding_rows: list[Mapping[str, object]] = []

    for event in events:
        kind = event.envelope.event_type
        if kind in {EventType.STEP_ATTEMPT_STARTED, EventType.STEP_ATTEMPT_COMPLETED}:
            raw = event.payload.get("attempt")
            if isinstance(raw, Mapping) and isinstance(raw.get("step_attempt_id"), str):
                attempts.add(raw["step_attempt_id"])
        elif kind is EventType.OBSERVATION_CAPTURED:
            raw = event.payload.get("observation")
            if isinstance(raw, Mapping) and isinstance(raw.get("observation_id"), str):
                observations.add(raw["observation_id"])
        elif kind in {EventType.CHECKPOINT_CAPTURED, EventType.ARTIFACT_COLLECTED}:
            raw = event.payload.get("artifact")
            if isinstance(raw, Mapping) and isinstance(raw.get("artifact_id"), str):
                retained_artifacts.add(raw["artifact_id"])
        elif kind is EventType.FINDING_RECORDED:
            raw = event.payload.get("finding")
            if isinstance(raw, Mapping):
                finding_rows.append(raw)
                value = raw.get("finding_id")
                if not isinstance(value, str):
                    raise FinalizationError("FINDING_RECORDED finding_id is invalid")
                try:
                    FindingId(value)
                except (TypeError, ValueError) as exc:
                    raise FinalizationError("FINDING_RECORDED finding_id is invalid") from exc
                if value in findings:
                    raise FinalizationError("finding IDs must be unique in canonical evidence")
                findings.add(value)

    allowed_refs = observations | retained_artifacts
    for raw in finding_rows:
        refs = raw.get("evidence_refs", ())
        if isinstance(refs, (str, bytes, bytearray, Mapping)) or not isinstance(refs, Sequence):
            raise FinalizationError("FINDING_RECORDED evidence_refs are malformed")
        for ref in tuple(refs):
            if not isinstance(ref, str) or ref not in allowed_refs:
                raise FinalizationError("FINDING_RECORDED references unknown canonical evidence")
    return attempts, findings


def _validate_retained_relationships(events, attempts: set[str], findings: set[str]) -> None:
    collection_ordinals: set[int] = set()
    for event in events:
        kind = event.envelope.event_type
        if kind is EventType.CHECKPOINT_CAPTURED:
            payload = _no_extensions(
                event.payload, _CHECKPOINT_ALLOWED, "CHECKPOINT_CAPTURED payload"
            )
            missing = _CHECKPOINT_REQUIRED - set(payload)
            if missing:
                raise FinalizationError("CHECKPOINT_CAPTURED payload is missing required fields")
            try:
                context = ArtifactContext(payload.get("context"))
            except (TypeError, ValueError) as exc:
                raise FinalizationError("CHECKPOINT_CAPTURED context is invalid") from exc
            if context is ArtifactContext.COLLECTED_FILE:
                raise FinalizationError("CHECKPOINT_CAPTURED cannot use collected_file context")
            attempt_id = payload.get("step_attempt_id")
            if attempt_id is not None:
                try:
                    StepAttemptId(attempt_id)
                except (TypeError, ValueError) as exc:
                    raise FinalizationError("CHECKPOINT_CAPTURED step_attempt_id is invalid") from exc
                if attempt_id not in attempts:
                    raise FinalizationError("CHECKPOINT_CAPTURED references an unknown step_attempt_id")
            finding_id = payload.get("finding_id")
            if finding_id is not None:
                try:
                    FindingId(finding_id)
                except (TypeError, ValueError) as exc:
                    raise FinalizationError("CHECKPOINT_CAPTURED finding_id is invalid") from exc
                if finding_id not in findings:
                    raise FinalizationError("CHECKPOINT_CAPTURED references an unknown finding_id")
            if context is ArtifactContext.FINDING_SCREENSHOT and finding_id is None:
                raise FinalizationError("finding screenshot checkpoint requires finding_id")
            if context is not ArtifactContext.FINDING_SCREENSHOT and finding_id is not None:
                raise FinalizationError("only finding screenshot checkpoints may carry finding_id")
        elif kind is EventType.ARTIFACT_COLLECTED:
            payload = _no_extensions(
                event.payload, _COLLECTED_FIELDS, "ARTIFACT_COLLECTED payload"
            )
            if set(payload) != _COLLECTED_FIELDS:
                raise FinalizationError("ARTIFACT_COLLECTED payload shape is invalid")

        # One Test Spec collection entry has exactly one retained or suppressed
        # outcome. Both event kinds must claim ordinals from the same namespace.
        if kind is EventType.ARTIFACT_COLLECTED or (
            kind is EventType.ARTIFACT_SUPPRESSED
            and event.payload.get("context") == ArtifactContext.COLLECTED_FILE.value
        ):
            ordinal = event.payload.get("collection_ordinal")
            if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal <= 0:
                raise FinalizationError(
                    f"{kind.name} collection_ordinal must be positive"
                )
            if ordinal in collection_ordinals:
                raise FinalizationError(
                    "collection_ordinal is duplicated across retained and suppressed outcomes"
                )
            collection_ordinals.add(ordinal)


def _normalized_assertion_inputs(events, prior_inputs):
    final_attempt: dict[str, tuple[int, str]] = {}
    step_kind: dict[str, str] = {}
    for event in events:
        if event.envelope.event_type is EventType.RUN_STARTED:
            raw_steps = event.payload.get("steps", ())
            if isinstance(raw_steps, Sequence) and not isinstance(raw_steps, (str, bytes, bytearray, Mapping)):
                for step in tuple(raw_steps):
                    if isinstance(step, Mapping) and isinstance(step.get("step_id"), str):
                        step_kind[step["step_id"]] = str(step.get("kind") or "")
        elif event.envelope.event_type is EventType.STEP_ATTEMPT_COMPLETED:
            raw = event.payload.get("attempt")
            if not isinstance(raw, Mapping):
                continue
            step_id, attempt_id, ordinal = raw.get("step_id"), raw.get("step_attempt_id"), raw.get("attempt")
            if isinstance(step_id, str) and isinstance(attempt_id, str) and isinstance(ordinal, int) and not isinstance(ordinal, bool):
                if step_id not in final_attempt or ordinal > final_attempt[step_id][0]:
                    final_attempt[step_id] = (ordinal, attempt_id)

    results: list[AssertionResult] = []
    required_by_attempt: dict[str, int] = {}
    for event in events:
        if event.envelope.event_type is not EventType.ASSERTION_EVALUATED:
            continue
        raw = event.payload.get("assertion")
        if not isinstance(raw, Mapping) or raw.get("required", True) is not True:
            continue
        step_id, attempt_id = raw.get("step_id"), raw.get("step_attempt_id")
        if not isinstance(step_id, str) or not isinstance(attempt_id, str):
            continue
        if final_attempt.get(step_id, (None, None))[1] != attempt_id:
            continue
        required_by_attempt[attempt_id] = required_by_attempt.get(attempt_id, 0) + 1
        try:
            results.append(AssertionResult(raw.get("result")))
        except (TypeError, ValueError) as exc:
            raise FinalizationError("assertion result is invalid") from exc

    satisfied = True
    for step_id, kind in step_kind.items():
        if kind != "assert":
            continue
        attempt_id = final_attempt.get(step_id, (None, None))[1]
        if attempt_id is None or required_by_attempt.get(attempt_id, 0) < 1:
            satisfied = False
    return replace(
        prior_inputs,
        required_assertion_results=tuple(results),
        required_assertions_satisfied=satisfied,
    )


def _restore_non_capsule_cleanup_error(events, run_id, state):
    if state.status_inputs.execution_error:
        return state
    if any(e.envelope.event_type is EventType.TARGET_CLOSED for e in events):
        return state
    retained = any(
        e.envelope.event_type is EventType.FAILURE_CAPSULE_RETAINED
        and e.payload.get("retained") is True
        for e in events
    )
    if not retained:
        return state
    starts = [e for e in events if e.envelope.event_type is EventType.RUN_STARTED]
    if len(starts) != 1:
        return state
    run = _run_record(starts[0].payload.get("run"), run_id)
    isolated = any(
        e.envelope.event_type is EventType.ENVIRONMENT_PREPARED
        and e.payload.get("isolated") is True
        for e in events
    )
    if run.environment_type != "capsule" or not isolated:
        return replace(
            state,
            status_inputs=replace(state.status_inputs, execution_error=True),
        )
    return state


def _event_payload_object(event: StoredEvent, key: str) -> Optional[Mapping[str, object]]:
    value = event.payload.get(key)
    return value if isinstance(value, Mapping) else None


def _normalize_attempt_status(value: object) -> Optional[StepAttemptStatus]:
    try:
        return StepAttemptStatus(value)
    except (TypeError, ValueError):
        return None


def _normalize_assertion_result(value: object) -> AssertionResult:
    try:
        return AssertionResult(value)
    except (TypeError, ValueError):
        return AssertionResult.UNEVALUATED


def _collect_evidence_state(events: Sequence[StoredEvent], run_id: RunId) -> _EvidenceState:
    snapshot = tuple(events)
    if not snapshot:
        raise FinalizationError("cannot finalize an empty ATES event stream")
    if any(event.run_id != run_id for event in snapshot):
        raise FinalizationError("finalization event history mixes run IDs")
    if tuple(event.sequence for event in snapshot) != tuple(range(1, len(snapshot) + 1)):
        raise FinalizationError("finalization requires gap-free canonical event sequence")

    run_started = [event for event in snapshot if event.envelope.event_type is EventType.RUN_STARTED]
    if len(run_started) != 1:
        raise FinalizationError("finalization requires exactly one RUN_STARTED event")
    if any(event.envelope.event_type is EventType.RUN_COMPLETED for event in snapshot):
        raise FinalizationError("run already contains RUN_COMPLETED; use recovery/verification")

    started_steps = run_started[0].payload.get("steps")
    if isinstance(started_steps, (str, bytes, bytearray, Mapping)) or not isinstance(started_steps, Sequence):
        raise FinalizationError("RUN_STARTED steps are malformed")
    steps: list[Mapping[str, object]] = []
    step_ids: set[str] = set()
    for item in tuple(started_steps):
        if not isinstance(item, Mapping):
            raise FinalizationError("RUN_STARTED steps must contain objects")
        step_id = item.get("step_id")
        if not isinstance(step_id, str) or not step_id or step_id in step_ids:
            raise FinalizationError("RUN_STARTED step identity is invalid or duplicated")
        step_ids.add(step_id)
        steps.append(dict(item))

    attempts_by_step: dict[str, list[Mapping[str, object]]] = {step_id: [] for step_id in step_ids}
    for event in snapshot:
        if event.envelope.event_type is not EventType.STEP_ATTEMPT_COMPLETED:
            continue
        attempt = _event_payload_object(event, "attempt")
        if attempt is None:
            raise FinalizationError("STEP_ATTEMPT_COMPLETED payload is malformed")
        step_id = attempt.get("step_id")
        attempt_id = attempt.get("step_attempt_id")
        ordinal = attempt.get("attempt")
        status = _normalize_attempt_status(attempt.get("status"))
        if (
            not isinstance(step_id, str)
            or step_id not in attempts_by_step
            or not isinstance(attempt_id, str)
            or not attempt_id
            or isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
            or ordinal < 1
            or status is None
            or status is StepAttemptStatus.RUNNING
        ):
            raise FinalizationError("completed step-attempt evidence is invalid")
        attempts_by_step[step_id].append(dict(attempt))

    final_attempt_by_step: dict[str, Mapping[str, object]] = {}
    final_attempt_id_by_step: dict[str, str] = {}
    required_steps_satisfied = True
    deterministic_failure = False
    execution_error = False
    cancelled = False
    for step_id, attempts in attempts_by_step.items():
        if not attempts:
            required_steps_satisfied = False
            continue
        ordered = sorted(attempts, key=lambda item: int(item["attempt"]))
        ordinals = [int(item["attempt"]) for item in ordered]
        if ordinals != list(range(1, len(ordinals) + 1)):
            raise FinalizationError("step attempt ordinals are not contiguous")
        final_attempt = ordered[-1]
        final_attempt_by_step[step_id] = final_attempt
        final_attempt_id_by_step[step_id] = str(final_attempt["step_attempt_id"])
        status = StepAttemptStatus(final_attempt["status"])
        if status is StepAttemptStatus.FAILED:
            deterministic_failure = True
        elif status in (StepAttemptStatus.ERROR, StepAttemptStatus.OUTCOME_UNKNOWN):
            execution_error = True
        elif status is StepAttemptStatus.CANCELLED:
            cancelled = True
        elif status is not StepAttemptStatus.PASSED:
            required_steps_satisfied = False

    assertions: list[Mapping[str, object]] = []
    required_assertion_results: list[AssertionResult] = []
    assertions_by_attempt: dict[str, int] = {}
    for event in snapshot:
        if event.envelope.event_type is not EventType.ASSERTION_EVALUATED:
            continue
        assertion = _event_payload_object(event, "assertion")
        if assertion is None:
            raise FinalizationError("ASSERTION_EVALUATED payload is malformed")
        assertions.append(dict(assertion))
        attempt_id = assertion.get("step_attempt_id")
        if not isinstance(attempt_id, str):
            raise FinalizationError("assertion step_attempt_id is invalid")
        if bool(assertion.get("required", False)):
            assertions_by_attempt[attempt_id] = assertions_by_attempt.get(attempt_id, 0) + 1
            # Only assertions attached to the effective/final attempt of their
            # logical step influence final status. Historical retry failures stay
            # immutable evidence but do not override a later successful attempt.
            step_id = assertion.get("step_id")
            if isinstance(step_id, str) and final_attempt_id_by_step.get(step_id) == attempt_id:
                required_assertion_results.append(
                    _normalize_assertion_result(assertion.get("result"))
                )

    required_assertions_satisfied = True
    for step in steps:
        if step.get("kind") != "assert":
            continue
        step_id = str(step["step_id"])
        attempt_id = final_attempt_id_by_step.get(step_id)
        if attempt_id is None or assertions_by_attempt.get(attempt_id, 0) < 1:
            required_assertions_satisfied = False

    unresolved_action = any(
        event.envelope.event_type is EventType.ACTION_OUTCOME_UNKNOWN for event in snapshot
    )

    incomplete_events = [
        event for event in snapshot
        if event.envelope.event_type is EventType.RUN_MARKED_INCOMPLETE
    ]
    nonprovisional_incomplete = False
    for event in incomplete_events:
        reason = event.payload.get("reason")
        if reason != "runtime.finalization_pending":
            nonprovisional_incomplete = True
            break

    target_launched = any(
        event.envelope.event_type is EventType.TARGET_LAUNCHED for event in snapshot
    )
    environment_released = any(
        event.envelope.event_type is EventType.ENVIRONMENT_RELEASED for event in snapshot
    )
    if target_launched and not environment_released:
        execution_error = True
    if nonprovisional_incomplete:
        execution_error = True

    artifacts: list[Mapping[str, object]] = []
    artifact_ids: set[str] = set()
    artifact_paths: set[str] = set()
    for event in snapshot:
        if event.envelope.event_type not in {
            EventType.CHECKPOINT_CAPTURED,
            EventType.ARTIFACT_COLLECTED,
        }:
            continue
        artifact = _event_payload_object(event, "artifact")
        if artifact is None:
            raise FinalizationError("artifact event payload is malformed")
        artifact_id = artifact.get("artifact_id")
        path = artifact.get("path")
        if not isinstance(artifact_id, str) or not artifact_id:
            raise FinalizationError("artifact identity is invalid")
        if not isinstance(path, str):
            raise FinalizationError("artifact path is invalid")
        validate_artifact_path(path)
        if artifact_id in artifact_ids or path in artifact_paths:
            raise FinalizationError("artifact identity/path is duplicated")
        artifact_ids.add(artifact_id)
        artifact_paths.add(path)
        artifacts.append(dict(artifact))

    status_inputs = StatusInputs(
        required_assertion_results=tuple(required_assertion_results),
        required_steps_satisfied=required_steps_satisfied,
        required_assertions_satisfied=required_assertions_satisfied,
        unresolved_action_outcome=unresolved_action,
        evidence_integrity_error=False,
        execution_error=execution_error,
        deterministic_failure=deterministic_failure,
        cancelled=cancelled,
    )
    return _EvidenceState(
        run_id=run_id,
        steps=tuple(steps),
        final_attempt_by_step=final_attempt_by_step,
        final_attempt_id_by_step=final_attempt_id_by_step,
        assertions=tuple(assertions),
        artifacts=tuple(artifacts),
        status_inputs=status_inputs,
    )


def _preserve_retained_failure(events, state):
    snapshot = tuple(events)
    inputs = state.status_inputs
    if not (inputs.execution_error and inputs.deterministic_failure):
        return state

    launch_index = next(
        (
            index
            for index, event in enumerate(snapshot)
            if event.envelope.event_type is EventType.TARGET_LAUNCHED
        ),
        None,
    )
    close_index = next(
        (
            index
            for index, event in enumerate(snapshot)
            if event.envelope.event_type is EventType.TARGET_CLOSED
        ),
        None,
    )
    release_index = next(
        (
            index
            for index, event in enumerate(snapshot)
            if event.envelope.event_type is EventType.ENVIRONMENT_RELEASED
        ),
        None,
    )
    retained_index = next(
        (
            index
            for index, event in enumerate(snapshot)
            if event.envelope.event_type is EventType.FAILURE_CAPSULE_RETAINED
            and event.payload.get("retained") is True
        ),
        None,
    )
    retention_evidence = (
        launch_index is not None
        and retained_index is not None
        and release_index is not None
        and launch_index < retained_index < release_index
    )
    provisional_fail = any(
        e.envelope.event_type is EventType.RUN_MARKED_INCOMPLETE
        and e.payload.get("reason") == "runtime.finalization_pending"
        and str(e.payload.get("execution_result") or "").strip().lower() in {"fail", "failed"}
        for e in snapshot
    )
    nonprovisional_incomplete = any(
        e.envelope.event_type is EventType.RUN_MARKED_INCOMPLETE
        and e.payload.get("reason") != "runtime.finalization_pending"
        for e in snapshot
    )
    unresolved_action = any(e.envelope.event_type is EventType.ACTION_OUTCOME_UNKNOWN for e in snapshot)
    if (
        launch_index is not None
        and close_index is None
        and release_index is not None
        and retention_evidence
        and provisional_fail
        and not nonprovisional_incomplete
        and not unresolved_action
    ):
        return replace(state, status_inputs=replace(inputs, execution_error=False))
    return state


def derive_evidence_state(events, run_id):
    """Validate the immutable history before deriving its effective outcome."""
    events = tuple(events)
    _validate_record_extensions(events)
    attempts, findings = _canonical_relationship_sets(events)
    _validate_retained_relationships(events, attempts, findings)
    _validate_lifecycle_payloads(events, run_id)
    _validate_suppression_relationships(events)
    for event in events:
        if event.envelope.event_type is EventType.RUN_MARKED_INCOMPLETE:
            _validate_terminal_marker(event.payload)
    dangling_proposal = _has_dangling_proposal(events)
    independent_lifecycle_error = _active_close_and_independent_lifecycle(events)
    _validate_ignored_event_shapes(events)
    _validate_provenance_and_terminal_lifecycle(events, run_id)
    (
        unresolved_dispatch,
        lifecycle_error,
        structural_integrity_error,
    ) = _validate_relationships(events, run_id)
    state = _collect_evidence_state(events, run_id)
    inputs = state.status_inputs
    if unresolved_dispatch:
        inputs = replace(inputs, unresolved_action_outcome=True)
    if lifecycle_error:
        inputs = replace(inputs, execution_error=True)
    if structural_integrity_error:
        inputs = replace(inputs, evidence_integrity_error=True)

    for event in events:
        if event.envelope.event_type is not EventType.RUN_MARKED_INCOMPLETE:
            continue
        if event.payload.get("reason") != "runtime.finalization_pending":
            continue
        execution_result = str(
            event.payload.get("execution_result") or ""
        ).strip().lower()
        if execution_result in {"error", "outcome_unknown"}:
            inputs = replace(inputs, execution_error=True)
        elif execution_result in {"fail", "failed"}:
            inputs = replace(inputs, deterministic_failure=True)
        elif execution_result in {"cancelled", "canceled"}:
            inputs = replace(inputs, cancelled=True)
    state = _preserve_retained_failure(events, replace(state, status_inputs=inputs))
    if _has_effective_attempt_execution_error(events) or independent_lifecycle_error or dangling_proposal:
        state = replace(state, status_inputs=replace(state.status_inputs, execution_error=True))
    state = replace(state, status_inputs=_normalized_assertion_inputs(events, state.status_inputs))
    return _restore_non_capsule_cleanup_error(events, run_id, state)
