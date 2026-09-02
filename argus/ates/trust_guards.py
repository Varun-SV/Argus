"""Cross-cutting ATES trust-boundary guards.

These guards harden recovery, incomplete-report validation, detached audit data,
and evidence-map provenance without creating review-round-specific source files.
They wrap existing internal validators so the public ATES API stays unchanged.
"""
from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from .core import (
    EventType,
    EvidenceValue,
    ExecutionKind,
    VerificationStatus,
    to_json_compatible,
    validate_step_attempt_history,
)
from .finalization_types import FinalizationError
from .ids import RunId
from .store import AtesEventStore


# Structural names emitted by Argus's built-in actions plus the explicitly
# supported test/extension action used by the canonical JSON type boundary.
# Every action type therefore has a policy-owned parameter-name contract.
ACTION_PARAMETER_KEYS: dict[str, frozenset[str]] = {
    "click": frozenset({"element_id", "x", "y"}),
    "double_click": frozenset({"element_id", "x", "y"}),
    "right_click": frozenset({"element_id", "x", "y"}),
    "type": frozenset({"text", "element_id"}),
    "key": frozenset({"keys"}),
    "scroll": frozenset({"direction", "amount"}),
    "menu": frozenset({"path"}),
    "wait": frozenset({"seconds"}),
    "done": frozenset({"success"}),
    "navigate": frozenset({"url"}),
    "run": frozenset({"command"}),
    "execute": frozenset({"command"}),
    "report_bug": frozenset({"title", "severity", "expected", "actual", "why"}),
    "toggle": frozenset({"enabled"}),
}

OBSERVATION_FACT_KEYS = frozenset(
    {
        "process_alive",
        "element_count",
        "dialog_count",
        "has_error",
        "has_stdout",
        "has_stderr",
        "has_url",
        "screenshot_present",
        "window_title",
        "ui_tree",
        "dialogs",
        "error",
        "stdout",
        "stderr",
        "url",
    }
)

_EVIDENCE_FIELDS = frozenset(
    {"disposition", "value", "reason", "secret_refs", "protected_ref"}
)
_SAFE_DETAIL_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_AUDIT_EVENT_TYPE = re.compile(
    r"^[a-z][a-z0-9_-]{0,31}(?:\.[a-z][a-z0-9_-]{0,31}){0,3}$"
)
_CUSTOM_DEDUPE_KEY = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
# Approval semantics validate the canonical APPROVAL-* namespace separately.
# The audit layer needs only a bounded structural identifier here so malformed
# relationship targets remain safe to render while the approval validator can
# classify them as invalid rather than turning them into audit-chain corruption.
_APPROVAL_ID = re.compile(r"^(?:APPROVAL|APR)-[0-9a-f]{32}$")
_FINALIZATION_ID = re.compile(r"^FINAL-[0-9a-f]{32}$")

# A fresh finalization may be synthesized only after a producer has returned
# through one of these synchronous terminal handoffs.  In particular,
# runtime.execution_interrupted is deliberately absent: that marker describes
# an exception/crash path and must remain incomplete unless a manifest-backed
# finalization transaction had already begun and recovery is merely resuming it.
_FRESH_FINALIZATION_HANDOFF_REASONS = frozenset(
    {
        "runtime.finalization_pending",
        "runtime.provider_check_failed",
        "runtime.transfer_prepare_failed",
        "runtime.target_launch_failed",
    }
)

_APPROVAL_AUDIT_KEYS = frozenset(
    {
        "approval_id",
        "approval_record_digest",
        "action",
        "supersedes_approval_id",
        "verification_status",
    }
)
_FINALIZATION_AUDIT_KEYS = frozenset(
    {
        "run_id",
        "finalization_id",
        "revision",
        "evidence_revision",
        "effective_status",
        "manifest_digest",
    }
)


def _validate_audit_identifiers(event_type: object, dedupe_key: object) -> None:
    """Require persisted audit routing/idempotency strings to be structural."""
    if not isinstance(event_type, str) or not _AUDIT_EVENT_TYPE.fullmatch(event_type):
        raise ValueError("audit event_type must be a bounded machine-safe identifier")
    if dedupe_key is None:
        return
    if not isinstance(dedupe_key, str):
        raise ValueError("audit dedupe_key must be a machine-safe identifier")
    if dedupe_key.startswith("approval:"):
        approval_id = dedupe_key.removeprefix("approval:")
        if not _APPROVAL_ID.fullmatch(approval_id):
            raise ValueError("approval audit dedupe_key is invalid")
        return
    if dedupe_key.startswith("finalization:"):
        finalization_id = dedupe_key.removeprefix("finalization:")
        if not _FINALIZATION_ID.fullmatch(finalization_id):
            raise ValueError("finalization audit dedupe_key is invalid")
        return
    if not _CUSTOM_DEDUPE_KEY.fullmatch(dedupe_key):
        raise ValueError("audit dedupe_key must be a bounded machine-safe identifier")


def _evidence_json(value: object, label: str) -> dict[str, object] | None:
    """Return canonical EvidenceValue JSON when *value* is classified evidence."""
    if isinstance(value, EvidenceValue):
        converted = to_json_compatible(value)
        if not isinstance(converted, dict):
            raise ValueError(f"{label} did not normalize to an evidence object")
        return converted
    if not isinstance(value, Mapping) or "disposition" not in value:
        return None
    if set(value) - _EVIDENCE_FIELDS:
        raise ValueError(f"{label} contains unexpected evidence fields")
    refs = value.get("secret_refs", ())
    if isinstance(refs, (str, bytes, bytearray, Mapping)) or not isinstance(refs, Sequence):
        raise ValueError(f"{label} secret_refs are malformed")
    try:
        record = EvidenceValue(
            disposition=value.get("disposition"),
            value=value.get("value"),
            reason=value.get("reason"),
            secret_refs=tuple(refs),
            protected_ref=value.get("protected_ref"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} is invalid privacy-classified evidence") from exc
    converted = to_json_compatible(record)
    if not isinstance(converted, dict):
        raise ValueError(f"{label} did not normalize to an evidence object")
    return converted


def _redacted_audit_text() -> dict[str, object]:
    value = to_json_compatible(EvidenceValue.redacted("privacy.audit_detail"))
    if not isinstance(value, dict):
        raise ValueError("audit redaction did not normalize to an evidence object")
    return value


def _safe_custom_audit_value(value: object, label: str) -> object:
    """Normalize custom audit data without persisting unclassified text.

    Callers that want text preserved must explicitly supply EvidenceValue. Raw
    strings stay API-compatible but are redacted before they ever reach disk.
    """
    evidence = _evidence_json(value, label)
    if evidence is not None:
        return evidence
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{label} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, child in value.items():
            if not isinstance(key, str) or not _SAFE_DETAIL_KEY.fullmatch(key):
                raise ValueError(f"{label} contains an unsafe structural key")
            result[key] = _safe_custom_audit_value(child, f"{label}.{key}")
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _safe_custom_audit_value(child, f"{label}[{index}]")
            for index, child in enumerate(value)
        ]
    if isinstance(value, str):
        return _redacted_audit_text()
    raise ValueError(f"{label} contains an unsupported audit value")


def _validate_known_audit_details(event_type: str, details: Mapping[str, object]) -> bool:
    if event_type == "approval.changed":
        if set(details) != _APPROVAL_AUDIT_KEYS:
            raise ValueError("approval.changed audit details have unexpected fields")
        approval_id = details.get("approval_id")
        digest = details.get("approval_record_digest")
        supersedes = details.get("supersedes_approval_id")
        if not isinstance(approval_id, str) or not _APPROVAL_ID.fullmatch(approval_id):
            raise ValueError("approval.changed approval_id is invalid")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise ValueError("approval.changed approval_record_digest is invalid")
        if supersedes is not None and (
            not isinstance(supersedes, str) or not _APPROVAL_ID.fullmatch(supersedes)
        ):
            raise ValueError("approval.changed supersedes_approval_id is invalid")
        if details.get("action") not in {"approved", "rejected", "revoked"}:
            raise ValueError("approval.changed action is invalid")
        if details.get("verification_status") not in {"verified", "unverified", "invalid"}:
            raise ValueError("approval.changed verification_status is invalid")
        return True
    if event_type == "finalization.bound":
        if set(details) != _FINALIZATION_AUDIT_KEYS:
            raise ValueError("finalization.bound audit details have unexpected fields")
        run_id = details.get("run_id")
        finalization_id = details.get("finalization_id")
        try:
            RunId(run_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("finalization.bound run_id is invalid") from exc
        if not isinstance(finalization_id, str) or not _FINALIZATION_ID.fullmatch(finalization_id):
            raise ValueError("finalization.bound finalization_id is invalid")
        for key in ("revision", "evidence_revision"):
            value = details.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"finalization.bound {key} is invalid")
        if details.get("effective_status") not in {"passed", "failed", "error", "cancelled"}:
            raise ValueError("finalization.bound effective_status is invalid")
        digest = details.get("manifest_digest")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise ValueError("finalization.bound manifest_digest is invalid")
        return True
    return False


def _normalize_audit_details(event_type: str, details: Mapping[str, object]) -> dict[str, object]:
    converted = to_json_compatible(dict(details))
    if not isinstance(converted, dict):
        raise ValueError("audit details did not normalize to an object")
    if _validate_known_audit_details(event_type, converted):
        return converted
    result: dict[str, object] = {}
    for key, value in details.items():
        if not isinstance(key, str) or not _SAFE_DETAIL_KEY.fullmatch(key):
            raise ValueError("audit details contain an unsafe structural key")
        result[key] = _safe_custom_audit_value(value, f"audit detail {key}")
    return result


def _validate_evidence_map_keys(events, ev) -> None:
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
            action_type = action.get("action_type")
            parameters = action.get("parameters")
            if not isinstance(action_type, str) or not isinstance(parameters, Mapping):
                continue
            if action_type == "invalid":
                if parameters:
                    raise FinalizationError("invalid action type cannot carry parameter names")
                continue
            allowed = ACTION_PARAMETER_KEYS.get(action_type)
            if allowed is None:
                raise FinalizationError("action_type has no supported parameter-key policy")
            unexpected = set(parameters) - allowed
            if unexpected:
                raise FinalizationError(
                    "action parameters contain unsupported or unsafe structural keys: "
                    + ", ".join(sorted(str(item) for item in unexpected))
                )
        elif kind is EventType.OBSERVATION_CAPTURED:
            observation = event.payload.get("observation")
            if not isinstance(observation, Mapping):
                continue
            facts = observation.get("facts")
            if not isinstance(facts, Mapping):
                continue
            unexpected = set(facts) - OBSERVATION_FACT_KEYS
            if unexpected:
                raise FinalizationError(
                    "observation facts contain unsupported or unsafe structural keys: "
                    + ", ".join(sorted(str(item) for item in unexpected))
                )


def _validate_incomplete_prefix(events, run_id, ev) -> None:
    """Validate every emitted record while allowing only future termination gaps."""
    snapshot = tuple(events)
    if not snapshot or snapshot[0].envelope.event_type is not EventType.RUN_STARTED:
        raise FinalizationError("RUN_STARTED must be the first incomplete event")
    starts = [e for e in snapshot if e.envelope.event_type is EventType.RUN_STARTED]
    if len(starts) != 1:
        raise FinalizationError("incomplete evidence requires exactly one RUN_STARTED")
    run = ev._run_record(starts[0].payload.get("run"), run_id)
    raw_steps = starts[0].payload.get("steps")
    if isinstance(raw_steps, (str, bytes, bytearray, Mapping)) or not isinstance(raw_steps, Sequence):
        raise FinalizationError("RUN_STARTED steps are malformed")
    steps = [ev._step_record(item)[0] for item in tuple(raw_steps)]
    step_by_id: dict[str, object] = {}
    for step in steps:
        key = str(step.step_id)
        if key in step_by_id:
            raise FinalizationError("RUN_STARTED step_id is duplicated")
        step_by_id[key] = step

    environment_prepared = False
    target_launched = False
    target_closed = False
    environment_released = False
    terminal_marker_seen = False
    active_attempt: str | None = None
    opened: dict[str, tuple[object, int]] = {}
    closed: dict[str, tuple[object, int]] = {}
    schedules: dict[str, tuple[str, int, int]] = {}
    observations: dict[str, object] = {}
    assertions: set[str] = set()
    actions: dict[str, tuple[str, object, int]] = {}

    for event in snapshot:
        if event.run_id != run_id:
            raise FinalizationError("incomplete evidence mixes run IDs")
        kind = event.envelope.event_type
        if kind is EventType.RUN_STARTED:
            continue
        if terminal_marker_seen:
            raise FinalizationError("events occur after terminal incomplete marker")

        if kind is EventType.ENVIRONMENT_PREPARED:
            if environment_prepared or target_launched or active_attempt is not None or environment_released:
                raise FinalizationError("environment preparation lifecycle is invalid")
            environment_prepared = True
            continue
        if kind is EventType.TARGET_LAUNCHED:
            if not environment_prepared or target_launched or target_closed or environment_released:
                raise FinalizationError("target launch lifecycle is invalid")
            target_launched = True
            continue
        if kind is EventType.STEP_RETRY_SCHEDULED:
            sid = event.payload.get("step_id")
            previous = event.payload.get("previous_step_attempt_id")
            next_id = event.payload.get("next_step_attempt_id")
            ordinal = event.payload.get("next_attempt")
            prior = closed.get(previous) if isinstance(previous, str) else None
            if (
                active_attempt is not None
                or not isinstance(sid, str)
                or sid not in step_by_id
                or not isinstance(next_id, str)
                or not next_id
                or isinstance(ordinal, bool)
                or not isinstance(ordinal, int)
                or ordinal < 2
                or next_id in schedules
                or next_id in opened
                or prior is None
            ):
                raise FinalizationError("STEP_RETRY_SCHEDULED payload/causality is invalid")
            prior_rec, prior_seq = prior
            if str(prior_rec.step_id) != sid or ordinal != prior_rec.attempt + 1 or prior_seq >= event.sequence:
                raise FinalizationError("retry scheduling causality is invalid")
            schedules[next_id] = (sid, ordinal, event.sequence)
            continue
        if kind is EventType.STEP_ATTEMPT_STARTED:
            rec = ev._attempt(event.payload.get("attempt"), running=True)
            aid, sid = str(rec.step_attempt_id), str(rec.step_id)
            if sid not in step_by_id or aid in opened or active_attempt is not None:
                raise FinalizationError("started step-attempt identity/lifecycle is invalid")
            if not environment_prepared or target_closed or environment_released:
                raise FinalizationError("step attempt started outside prepared environment")
            if run.execution_kind is not ExecutionKind.ROAM and not target_launched:
                raise FinalizationError("scripted step attempt started before target launch")
            if rec.attempt > 1:
                scheduled = schedules.get(aid)
                if scheduled is None or scheduled[:2] != (sid, rec.attempt) or scheduled[2] >= event.sequence:
                    raise FinalizationError("retry start does not match STEP_RETRY_SCHEDULED")
            elif aid in schedules:
                raise FinalizationError("first attempt cannot be retry-scheduled")
            opened[aid] = (rec, event.sequence)
            active_attempt = aid
            continue
        if kind is EventType.STEP_ATTEMPT_COMPLETED:
            rec = ev._attempt(event.payload.get("attempt"), running=False)
            aid = str(rec.step_attempt_id)
            start = opened.get(aid)
            if start is None:
                raise FinalizationError("step attempt completed without a matching start")
            if active_attempt != aid or aid in closed:
                raise FinalizationError("step attempt completion lifecycle is invalid")
            srec, sseq = start
            if (
                rec.step_id != srec.step_id
                or rec.attempt != srec.attempt
                or rec.started_at != srec.started_at
                or not ev._canonical_json_equal(rec.retry_reason, srec.retry_reason)
                or sseq >= event.sequence
            ):
                raise FinalizationError("step attempt completion does not match its start")
            closed[aid] = (rec, event.sequence)
            active_attempt = None
            continue
        if kind is EventType.OBSERVATION_CAPTURED:
            rec = ev._observation_record(event.payload.get("observation"))
            oid = str(rec.observation_id)
            if oid in observations:
                raise FinalizationError("observation_id is duplicated")
            if not target_launched or target_closed or active_attempt != str(rec.step_attempt_id):
                raise FinalizationError("observation occurred outside its active target/attempt")
            observations[oid] = rec
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
            rec = ev._action_record(event.payload.get("action"), label)
            action_id = str(rec.action_id)
            if not target_launched or target_closed or active_attempt != str(rec.step_attempt_id):
                raise FinalizationError(f"{label} action occurred outside its active target/attempt")
            current = opened.get(active_attempt) if active_attempt is not None else None
            if current is None or str(rec.step_id) != str(current[0].step_id):
                raise FinalizationError(f"{label} action step_id disagrees with active attempt")
            prior = actions.get(action_id)
            if kind is EventType.ACTION_PROPOSED:
                if prior is not None:
                    raise FinalizationError("canonical action_id was proposed more than once")
                actions[action_id] = ("proposed", rec, event.sequence)
            elif prior is None or not ev._same_action_identity(prior[1], rec):
                raise FinalizationError(f"{label} action does not match its proposal identity")
            elif kind is EventType.ACTION_POLICY_VALIDATED:
                if prior[0] != "proposed":
                    raise FinalizationError("action policy validation lifecycle is invalid")
                actions[action_id] = ("validated", rec, event.sequence)
            else:
                if prior[0] != "validated" or rec.operation_id is None:
                    raise FinalizationError("action dispatch commit lifecycle is invalid")
                if not ev._canonical_json_equal(prior[1], rec):
                    raise FinalizationError("dispatch commit differs from policy-validated action")
                actions[action_id] = ("committed", rec, event.sequence)
            continue
        if kind in {EventType.ACTION_EXECUTED, EventType.ACTION_OUTCOME_UNKNOWN}:
            action_id = event.payload.get("action_id")
            prior = actions.get(action_id) if isinstance(action_id, str) else None
            if prior is None or prior[0] != "committed":
                raise FinalizationError("action terminal event has no committed dispatch")
            rec = prior[1]
            expected_operation = str(rec.operation_id) if rec.operation_id is not None else None
            if event.payload.get("operation_id") != expected_operation:
                raise FinalizationError("action terminal operation_id disagrees with dispatch")
            if not target_launched or target_closed or active_attempt != str(rec.step_attempt_id):
                raise FinalizationError("action terminal event occurred outside its active target/attempt")
            if kind is EventType.ACTION_EXECUTED and event.payload.get("result") != "executed":
                raise FinalizationError("ACTION_EXECUTED result is invalid")
            actions[action_id] = ("terminal", rec, event.sequence)
            continue
        if kind is EventType.ASSERTION_EVALUATED:
            rec = ev._assertion_record(event.payload.get("assertion"))
            assertion_id = str(rec.assertion_id)
            if assertion_id in assertions:
                raise FinalizationError("assertion_id is duplicated")
            if not target_launched or target_closed or active_attempt != str(rec.step_attempt_id):
                raise FinalizationError("assertion occurred outside its active target/attempt")
            current = opened.get(active_attempt) if active_attempt is not None else None
            if current is None or str(rec.step_id) != str(current[0].step_id):
                raise FinalizationError("assertion step_id disagrees with active attempt")
            if rec.observation_id is not None and str(rec.observation_id) not in observations:
                raise FinalizationError("assertion references an unknown observation")
            assertions.add(assertion_id)
            continue
        if kind is EventType.TARGET_CLOSED:
            if not target_launched or target_closed or environment_released or active_attempt is not None:
                raise FinalizationError("target close lifecycle is invalid")
            target_closed = True
            continue
        if kind is EventType.ENVIRONMENT_RELEASED:
            if not environment_prepared or environment_released or active_attempt is not None:
                raise FinalizationError("environment release lifecycle is invalid")
            environment_released = True
            continue
        if kind is EventType.RUN_MARKED_INCOMPLETE:
            if not environment_released or active_attempt is not None:
                raise FinalizationError("RUN_MARKED_INCOMPLETE precedes environment release")
            terminal_marker_seen = True
            continue
        if kind is EventType.RUN_COMPLETED:
            raise FinalizationError("incomplete prefix contains RUN_COMPLETED")

    completed_records = tuple(
        rec for rec, _seq in sorted(closed.values(), key=lambda item: item[1])
    )
    if completed_records:
        try:
            validate_step_attempt_history(completed_records)
        except ValueError as exc:
            raise FinalizationError(f"completed attempt prefix is invalid: {exc}") from exc
    unclosed = set(opened) - set(closed)
    if unclosed != ({active_attempt} if active_attempt is not None else set()):
        raise FinalizationError("incomplete evidence contains orphan step attempts")
    started_retry_ids = {aid for aid, (rec, _seq) in opened.items() if rec.attempt > 1}
    if not started_retry_ids.issubset(set(schedules)):
        raise FinalizationError("retry attempt lacks its durable schedule")


def install() -> None:
    """Install idempotent guards around the existing internal ATES validators."""
    from . import audit as audit
    from . import evidence_validation as ev
    from . import finalization as finalization
    from . import reports as reports

    if getattr(finalization, "_ates_trust_guards_installed", False):
        return

    base_recover = finalization._recover_unbound_revision
    base_record_extensions = ev._validate_record_extensions
    base_retained_failure = ev._preserve_retained_failure
    base_incomplete_events = reports._incomplete_events
    base_normalize_audit_inputs = audit._normalize_audit_inputs
    base_validate_audit_records = audit._validate_audit_records

    def guarded_recover(project_dir, run_id):
        rid = run_id if isinstance(run_id, RunId) else RunId(run_id)
        project = Path(project_dir).resolve(strict=True)
        root = project / ".argus" / "runs" / finalization._run_directory_key(rid)
        manifest = root / "manifests" / "manifest-0001.json"
        package_manifest = root / "manifests" / "package-manifest-0001.json"
        if not os.path.lexists(manifest) and not os.path.lexists(package_manifest):
            store = AtesEventStore(project, rid, repair_trailing_partial=True)
            try:
                events = tuple(store.events)
                terminal = events[-1] if events else None
                if (
                    terminal is None
                    or terminal.envelope.event_type is not EventType.RUN_MARKED_INCOMPLETE
                    or terminal.payload.get("reason")
                    not in _FRESH_FINALIZATION_HANDOFF_REASONS
                ):
                    raise FinalizationError(
                        "recovery cannot start a fresh finalization without an "
                        "explicit completion-ready producer terminal marker"
                    )
            finally:
                store.close()
        return base_recover(project, rid)

    def guarded_record_extensions(events):
        base_record_extensions(events)
        _validate_evidence_map_keys(events, ev)

    def guarded_retained_failure(events, state):
        result = base_retained_failure(events, state)
        if state.status_inputs.execution_error and not result.status_inputs.execution_error:
            snapshot = tuple(events)
            retained = [
                index
                for index, event in enumerate(snapshot)
                if event.envelope.event_type is EventType.FAILURE_CAPSULE_RETAINED
                and event.payload.get("retained") is True
            ]
            completed = [
                index
                for index, event in enumerate(snapshot)
                if event.envelope.event_type is EventType.STEP_ATTEMPT_COMPLETED
            ]
            # Retention may excuse only cleanup uncertainty that follows settled
            # execution. A predeclared retention event cannot erase a later error.
            if not retained or not completed or retained[0] <= max(completed):
                return state
        return result

    def guarded_incomplete_events(root):
        events, run_id, raw = base_incomplete_events(root)
        try:
            _validate_incomplete_prefix(events, run_id, ev)
            # Prefix incompleteness relaxes only future terminal requirements;
            # every retained artifact already emitted must satisfy the complete
            # ArtifactRecord metadata/commitment contract before reports copy it.
            for event in events:
                if event.envelope.event_type in {
                    EventType.CHECKPOINT_CAPTURED,
                    EventType.ARTIFACT_COLLECTED,
                }:
                    ev._artifact_record(event.payload.get("artifact"))
            attempts, findings = ev._canonical_relationship_sets(events)
            ev._validate_retained_relationships(events, attempts, findings)
            ev._validate_suppression_relationships(events)
        except FinalizationError as exc:
            raise reports.ReportError(
                "incomplete evidence violates canonical record/relationship invariants"
            ) from exc
        return events, run_id, raw

    def guarded_authentication_status(record, resolver):
        auth = record.get("authentication")
        if not isinstance(auth, Mapping):
            return VerificationStatus.INVALID, "authentication metadata is malformed"
        declared = auth.get("status")
        method = auth.get("method")
        key_id = auth.get("key_id")
        signature = auth.get("signature")
        if declared == VerificationStatus.UNVERIFIED.value:
            if method is not None or key_id is not None or signature is not None:
                return VerificationStatus.INVALID, "unverified record carries authentication material"
            return VerificationStatus.UNVERIFIED, None
        if declared != VerificationStatus.VERIFIED.value or method != audit.APPROVAL_AUTH_METHOD:
            return VerificationStatus.INVALID, "unsupported approval authentication state"
        if not isinstance(key_id, str) or not key_id or not isinstance(signature, str):
            return VerificationStatus.INVALID, "authenticated approval is missing key/signature metadata"
        if resolver is None:
            return VerificationStatus.UNVERIFIED, "trusted reviewer credential was not supplied"
        try:
            credential = resolver(key_id)
        except Exception:
            # Ordinary resolver failures degrade to unverified. Process/task
            # cancellation (BaseException subclasses) deliberately propagates.
            return VerificationStatus.UNVERIFIED, "trusted reviewer credential lookup failed"
        if credential is None:
            return VerificationStatus.UNVERIFIED, "trusted reviewer credential is unavailable"
        if not isinstance(credential, audit.ApprovalCredential):
            return VerificationStatus.UNVERIFIED, "resolver did not bind key material to an actor/role policy"
        if credential.key_id != key_id:
            return VerificationStatus.INVALID, "resolved reviewer credential has another key_id"
        if record.get("actor") != credential.actor:
            return VerificationStatus.INVALID, "approval actor is not authenticated by this credential"
        role = record.get("role")
        if not isinstance(role, str) or role not in credential.roles:
            return VerificationStatus.INVALID, "approval role is not authorized by this credential"
        try:
            expected = audit._sign_record(record, credential.key)
        except audit.ApprovalError as exc:
            return VerificationStatus.INVALID, str(exc)
        if not audit.hmac.compare_digest(signature, expected):
            return VerificationStatus.INVALID, "approval authentication signature does not verify"
        return VerificationStatus.VERIFIED, None

    def guarded_normalize_audit_inputs(event_type, actor, details, occurred_at, dedupe_key):
        try:
            _validate_audit_identifiers(event_type, dedupe_key)
        except (TypeError, ValueError) as exc:
            raise audit.ApprovalError(str(exc)) from exc
        when, _converted = base_normalize_audit_inputs(
            event_type, actor, details, occurred_at, dedupe_key
        )
        try:
            normalized = _normalize_audit_details(event_type, details)
        except (TypeError, ValueError) as exc:
            raise audit.ApprovalError(str(exc)) from exc
        return when, normalized

    def guarded_validate_audit_records(records):
        validated = base_validate_audit_records(records)
        for index, record in enumerate(validated, 1):
            try:
                _validate_audit_identifiers(
                    record.get("event_type"), record.get("dedupe_key")
                )
            except (TypeError, ValueError) as exc:
                raise audit.ApprovalError(
                    f"audit record {index} has unsafe structural identifiers: {exc}"
                ) from exc
            details = record.get("details")
            if not isinstance(details, Mapping):
                continue
            try:
                normalized = _normalize_audit_details(str(record.get("event_type")), details)
            except (TypeError, ValueError) as exc:
                raise audit.ApprovalError(
                    f"audit record {index} has unsafe details: {exc}"
                ) from exc
            # Read-side validation must not silently reinterpret persisted legacy
            # plaintext. Only canonical already-sanitized detail bytes are valid.
            if to_json_compatible(dict(details)) != normalized:
                raise audit.ApprovalError(
                    f"audit record {index} contains unclassified free-form detail text"
                )
        return validated

    finalization._recover_unbound_revision = guarded_recover
    ev._validate_record_extensions = guarded_record_extensions
    ev._preserve_retained_failure = guarded_retained_failure
    reports._incomplete_events = guarded_incomplete_events
    audit._authentication_status = guarded_authentication_status
    audit._normalize_audit_inputs = guarded_normalize_audit_inputs
    audit._validate_audit_records = guarded_validate_audit_records
    finalization._ates_trust_guards_installed = True


__all__ = ["ACTION_PARAMETER_KEYS", "OBSERVATION_FACT_KEYS", "install"]