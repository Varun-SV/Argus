"""Round-9 canonical privacy and relationship hardening for PR #22.

Every producer-owned record that reports copy from canonical evidence is
validated for its documented field set before finalization.  The layer also
validates retained-checkpoint provenance, Finding references/identity, the
Failure Capsule environment precondition, and the AssertionRecord default for
``required``.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from . import finalization_round3 as _round3
from .artifacts import ArtifactContext
from .core import AssertionResult, EventType, EvidenceValue
from .ids import ArtifactId, FindingId, ObservationId, StepAttemptId

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


def _mapping(value: object, label: str, impl) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise impl.FinalizationError(f"{label} is malformed")
    return value


def _no_extensions(value: object, allowed: frozenset[str], label: str, impl):
    record = _mapping(value, label, impl)
    extra = set(record) - allowed
    if extra:
        raise impl.FinalizationError(
            f"{label} contains unexpected fields: " + ", ".join(sorted(str(item) for item in extra))
        )
    return record


def _evidence(value: object, label: str, impl) -> EvidenceValue:
    raw = _no_extensions(value, _EVIDENCE_FIELDS, label, impl)
    refs = raw.get("secret_refs", ())
    if isinstance(refs, (str, bytes, bytearray, Mapping)) or not isinstance(refs, Sequence):
        raise impl.FinalizationError(f"{label} secret_refs are malformed")
    try:
        return EvidenceValue(
            disposition=raw.get("disposition"),
            value=raw.get("value"),
            reason=raw.get("reason"),
            secret_refs=tuple(refs),
            protected_ref=raw.get("protected_ref"),
        )
    except (TypeError, ValueError) as exc:
        raise impl.FinalizationError(f"{label} is invalid") from exc


def _commitment(value: object, label: str, impl) -> None:
    if value is None:
        return
    _no_extensions(value, _COMMITMENT_FIELDS, label, impl)


def _validate_record_extensions(events, impl) -> None:
    for event in events:
        kind = event.envelope.event_type
        if kind is EventType.RUN_STARTED:
            steps = event.payload.get("steps")
            if isinstance(steps, (str, bytes, bytearray, Mapping)) or not isinstance(steps, Sequence):
                raise impl.FinalizationError("RUN_STARTED steps are malformed")
            for raw in tuple(steps):
                step = _no_extensions(raw, _STEP_FIELDS, "RUN_STARTED step", impl)
                if "instruction" in step:
                    _evidence(step.get("instruction"), "step instruction", impl)
        elif kind in {EventType.STEP_ATTEMPT_STARTED, EventType.STEP_ATTEMPT_COMPLETED}:
            attempt = _no_extensions(event.payload.get("attempt"), _ATTEMPT_FIELDS, "step attempt", impl)
            if attempt.get("retry_reason") is not None:
                _evidence(attempt.get("retry_reason"), "step attempt retry_reason", impl)
        elif kind in {
            EventType.ACTION_PROPOSED,
            EventType.ACTION_POLICY_VALIDATED,
            EventType.ACTION_DISPATCH_COMMITTED,
        }:
            action = _no_extensions(event.payload.get("action"), _ACTION_FIELDS, "action record", impl)
            parameters = _mapping(action.get("parameters"), "action parameters", impl)
            for name, item in parameters.items():
                _evidence(item, f"action parameter {name}", impl)
        elif kind is EventType.OBSERVATION_CAPTURED:
            observation = _no_extensions(
                event.payload.get("observation"), _OBSERVATION_FIELDS, "observation record", impl
            )
            facts = _mapping(observation.get("facts"), "observation facts", impl)
            for name, item in facts.items():
                _evidence(item, f"observation fact {name}", impl)
        elif kind is EventType.ASSERTION_EVALUATED:
            assertion = _no_extensions(
                event.payload.get("assertion"), _ASSERTION_FIELDS, "assertion record", impl
            )
            _evidence(assertion.get("expected"), "assertion expected", impl)
            if assertion.get("actual") is not None:
                _evidence(assertion.get("actual"), "assertion actual", impl)
            requirement = assertion.get("requirement")
            if requirement is not None:
                req = _no_extensions(requirement, _REQUIREMENT_FIELDS, "assertion requirement", impl)
                _commitment(req.get("commitment"), "assertion requirement commitment", impl)
        elif kind is EventType.FINDING_RECORDED:
            finding = _no_extensions(
                event.payload.get("finding"), _FINDING_FIELDS, "FINDING_RECORDED finding", impl
            )
            _evidence(finding.get("title"), "finding title", impl)
            _evidence(finding.get("description"), "finding description", impl)
        elif kind in {EventType.CHECKPOINT_CAPTURED, EventType.ARTIFACT_COLLECTED}:
            artifact = _no_extensions(
                event.payload.get("artifact"), _ARTIFACT_FIELDS, "retained artifact record", impl
            )
            _commitment(artifact.get("content_digest"), "artifact content commitment", impl)
        elif kind is EventType.FAILURE_CAPSULE_RETAINED:
            payload = _no_extensions(event.payload, _CAPSULE_FIELDS, "FAILURE_CAPSULE_RETAINED payload", impl)
            if payload.get("retained") is not True:
                raise impl.FinalizationError("FAILURE_CAPSULE_RETAINED retained must be true")


def _canonical_relationship_sets(events, impl):
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
                    raise impl.FinalizationError("FINDING_RECORDED finding_id is invalid")
                try:
                    FindingId(value)
                except (TypeError, ValueError) as exc:
                    raise impl.FinalizationError("FINDING_RECORDED finding_id is invalid") from exc
                if value in findings:
                    raise impl.FinalizationError("finding IDs must be unique in canonical evidence")
                findings.add(value)

    allowed_refs = observations | retained_artifacts
    for raw in finding_rows:
        refs = raw.get("evidence_refs", ())
        if isinstance(refs, (str, bytes, bytearray, Mapping)) or not isinstance(refs, Sequence):
            raise impl.FinalizationError("FINDING_RECORDED evidence_refs are malformed")
        for ref in tuple(refs):
            if not isinstance(ref, str) or ref not in allowed_refs:
                raise impl.FinalizationError("FINDING_RECORDED references unknown canonical evidence")
    return attempts, findings


def _validate_retained_relationships(events, attempts: set[str], findings: set[str], impl) -> None:
    collection_ordinals: set[int] = set()
    for event in events:
        kind = event.envelope.event_type
        if kind is EventType.CHECKPOINT_CAPTURED:
            payload = _no_extensions(
                event.payload, _CHECKPOINT_ALLOWED, "CHECKPOINT_CAPTURED payload", impl
            )
            missing = _CHECKPOINT_REQUIRED - set(payload)
            if missing:
                raise impl.FinalizationError("CHECKPOINT_CAPTURED payload is missing required fields")
            try:
                context = ArtifactContext(payload.get("context"))
            except (TypeError, ValueError) as exc:
                raise impl.FinalizationError("CHECKPOINT_CAPTURED context is invalid") from exc
            if context is ArtifactContext.COLLECTED_FILE:
                raise impl.FinalizationError("CHECKPOINT_CAPTURED cannot use collected_file context")
            attempt_id = payload.get("step_attempt_id")
            if attempt_id is not None:
                try:
                    StepAttemptId(attempt_id)
                except (TypeError, ValueError) as exc:
                    raise impl.FinalizationError("CHECKPOINT_CAPTURED step_attempt_id is invalid") from exc
                if attempt_id not in attempts:
                    raise impl.FinalizationError("CHECKPOINT_CAPTURED references an unknown step_attempt_id")
            finding_id = payload.get("finding_id")
            if finding_id is not None:
                try:
                    FindingId(finding_id)
                except (TypeError, ValueError) as exc:
                    raise impl.FinalizationError("CHECKPOINT_CAPTURED finding_id is invalid") from exc
                if finding_id not in findings:
                    raise impl.FinalizationError("CHECKPOINT_CAPTURED references an unknown finding_id")
            if context is ArtifactContext.FINDING_SCREENSHOT and finding_id is None:
                raise impl.FinalizationError("finding screenshot checkpoint requires finding_id")
            if context is not ArtifactContext.FINDING_SCREENSHOT and finding_id is not None:
                raise impl.FinalizationError("only finding screenshot checkpoints may carry finding_id")
        elif kind is EventType.ARTIFACT_COLLECTED:
            payload = _no_extensions(
                event.payload, _COLLECTED_FIELDS, "ARTIFACT_COLLECTED payload", impl
            )
            if set(payload) != _COLLECTED_FIELDS:
                raise impl.FinalizationError("ARTIFACT_COLLECTED payload shape is invalid")

        # One Test Spec collection entry has exactly one retained or suppressed
        # outcome. Both event kinds must claim ordinals from the same namespace.
        if kind is EventType.ARTIFACT_COLLECTED or (
            kind is EventType.ARTIFACT_SUPPRESSED
            and event.payload.get("context") == ArtifactContext.COLLECTED_FILE.value
        ):
            ordinal = event.payload.get("collection_ordinal")
            if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal <= 0:
                raise impl.FinalizationError(
                    f"{kind.name} collection_ordinal must be positive"
                )
            if ordinal in collection_ordinals:
                raise impl.FinalizationError(
                    "collection_ordinal is duplicated across retained and suppressed outcomes"
                )
            collection_ordinals.add(ordinal)


def _normalized_assertion_inputs(events, prior_inputs, impl):
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
            raise impl.FinalizationError("assertion result is invalid") from exc

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


def _restore_non_capsule_cleanup_error(events, run_id, state, impl):
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
    run = _round3._run_record(starts[0].payload.get("run"), run_id, impl)
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


def install(impl) -> None:
    previous_derive = impl._derive

    def derive(events, run_id):
        snapshot = tuple(events)
        _validate_record_extensions(snapshot, impl)
        attempts, findings = _canonical_relationship_sets(snapshot, impl)
        _validate_retained_relationships(snapshot, attempts, findings, impl)
        state = previous_derive(snapshot, run_id)
        state = replace(
            state,
            status_inputs=_normalized_assertion_inputs(snapshot, state.status_inputs, impl),
        )
        return _restore_non_capsule_cleanup_error(snapshot, run_id, state, impl)

    impl._derive = derive


__all__ = ["install"]
