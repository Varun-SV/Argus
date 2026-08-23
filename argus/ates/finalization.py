"""Compatibility layer applying hardened verification/derivation fixes to PR #22 finalization."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

from . import finalization_impl as _impl
from .artifacts import ArtifactCaptureError
from .core import (
    ActionRecord,
    AssertionRecord,
    EventType,
    EvidenceValue,
    ObservationRecord,
    RequirementIdentity,
    SourceCommitment,
    StepRecord,
    to_json_compatible,
    validate_step_evidence_relationships,
)

_raw_derive = _impl._derive
_raw_artifacts = _impl._artifacts
_raw_verify_finalized_run = _impl.verify_finalized_run
_raw_publish = _impl._publish
_raw_finalize_revision_one = _impl.finalize_revision_one


def _evidence_value(value: object, label: str) -> EvidenceValue:
    if not isinstance(value, Mapping):
        raise _impl.FinalizationError(f"{label} is malformed")
    refs = value.get("secret_refs", ())
    if isinstance(refs, (str, bytes, bytearray, Mapping)) or not isinstance(refs, Sequence):
        raise _impl.FinalizationError(f"{label} secret_refs are malformed")
    try:
        return EvidenceValue(
            disposition=value.get("disposition"),
            value=value.get("value"),
            reason=value.get("reason"),
            secret_refs=tuple(refs),
            protected_ref=value.get("protected_ref"),
        )
    except (TypeError, ValueError) as exc:
        raise _impl.FinalizationError(f"{label} is invalid") from exc


def _source_commitment(value: object, label: str):
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise _impl.FinalizationError(f"{label} is malformed")
    try:
        return SourceCommitment(
            method=value["method"],
            value=value["value"],
            canonicalization_profile=value.get("canonicalization_profile"),
            verification_ref=value.get("verification_ref"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise _impl.FinalizationError(f"{label} is invalid") from exc


def _requirement(value: object):
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise _impl.FinalizationError("assertion requirement is malformed")
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
        raise _impl.FinalizationError("assertion requirement is invalid") from exc


def _step_record(value: object):
    if not isinstance(value, Mapping):
        raise _impl.FinalizationError("RUN_STARTED step is malformed")
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
        if isinstance(exc, _impl.FinalizationError):
            raise
        raise _impl.FinalizationError("RUN_STARTED step is invalid") from exc


def _action_record(value: object, label: str) -> ActionRecord:
    if not isinstance(value, Mapping):
        raise _impl.FinalizationError(f"{label} action is malformed")
    parameters = value.get("parameters")
    if not isinstance(parameters, Mapping):
        raise _impl.FinalizationError(f"{label} action parameters are malformed")
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
        if isinstance(exc, _impl.FinalizationError):
            raise
        raise _impl.FinalizationError(f"{label} action is invalid") from exc


def _observation_record(value: object) -> ObservationRecord:
    if not isinstance(value, Mapping):
        raise _impl.FinalizationError("observation is malformed")
    facts = value.get("facts")
    if not isinstance(facts, Mapping):
        raise _impl.FinalizationError("observation facts are malformed")
    try:
        return ObservationRecord(
            observation_id=value["observation_id"],
            step_attempt_id=value["step_attempt_id"],
            source=value["source"],
            captured_at=_impl._time(value["captured_at"], "observation captured_at"),
            capture_policy=value["capture_policy"],
            facts={
                key: _evidence_value(item, f"observation fact {key}")
                for key, item in facts.items()
            },
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, _impl.FinalizationError):
            raise
        raise _impl.FinalizationError("observation is invalid") from exc


def _assertion_record(value: object) -> AssertionRecord:
    if not isinstance(value, Mapping):
        raise _impl.FinalizationError("assertion is malformed")
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
        if isinstance(exc, _impl.FinalizationError):
            raise
        raise _impl.FinalizationError("assertion is invalid") from exc


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
        raise _impl.FinalizationError("cannot finalize an empty ATES event stream")
    if snapshot[0].envelope.event_type is not EventType.RUN_STARTED:
        raise _impl.FinalizationError("RUN_STARTED must be the first canonical event")

    run_started = [
        event
        for event in snapshot
        if event.envelope.event_type is EventType.RUN_STARTED
    ]
    if len(run_started) != 1:
        raise _impl.FinalizationError(
            "finalization requires exactly one RUN_STARTED event"
        )
    raw_steps = run_started[0].payload.get("steps")
    if (
        isinstance(raw_steps, (str, bytes, bytearray, Mapping))
        or not isinstance(raw_steps, Sequence)
    ):
        raise _impl.FinalizationError("RUN_STARTED steps are malformed")
    parsed_steps = tuple(_step_record(item) for item in tuple(raw_steps))
    steps = tuple(item[0] for item in parsed_steps)
    step_kind_by_id = {str(step.step_id): step.kind for step in steps}
    structural_integrity_error = any(item[1] for item in parsed_steps)

    # Preserve the more precise attempt-lifecycle diagnostics before applying
    # the broader target/action/relationship state machines.
    _impl._validate_attempts(snapshot)

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
            raise _impl.FinalizationError(
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
                raise _impl.FinalizationError(
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
                raise _impl.FinalizationError("target launch lifecycle is invalid")
            target_launched = event.sequence
            continue

        if kind is EventType.STEP_ATTEMPT_STARTED:
            record = _impl._attempt(event.payload.get("attempt"), running=True)
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
                raise _impl.FinalizationError(
                    "step attempt started outside an active target lifecycle"
                )
            active_attempt = str(record.step_attempt_id)
            continue

        if kind is EventType.STEP_ATTEMPT_COMPLETED:
            record = _impl._attempt(event.payload.get("attempt"), running=False)
            if active_attempt != str(record.step_attempt_id):
                raise _impl.FinalizationError(
                    "step attempt completion does not own the active attempt"
                )
            terminal_attempts.append(record)
            active_attempt = None
            continue

        if kind is EventType.OBSERVATION_CAPTURED:
            record = _observation_record(event.payload.get("observation"))
            if target_launched is None or target_closed is not None:
                raise _impl.FinalizationError(
                    "observation occurred outside an active target lifecycle"
                )
            if active_attempt != str(record.step_attempt_id):
                raise _impl.FinalizationError(
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
                raise _impl.FinalizationError(
                    f"{label} action occurred outside an active target lifecycle"
                )
            if active_attempt != str(record.step_attempt_id):
                raise _impl.FinalizationError(
                    f"{label} action occurred outside its active step attempt"
                )

            if kind is EventType.ACTION_PROPOSED:
                if action_id in action_states:
                    raise _impl.FinalizationError(
                        "canonical action_id was proposed more than once"
                    )
                proposals[action_id] = record
                action_states[action_id] = ("proposed", record, event.sequence)
                continue

            prior = action_states.get(action_id)
            if prior is None or not _same_action_identity(prior[1], record):
                raise _impl.FinalizationError(
                    f"{label} action does not match its proposal identity"
                )
            if kind is EventType.ACTION_POLICY_VALIDATED:
                if prior[0] != "proposed":
                    raise _impl.FinalizationError(
                        "action policy validation lifecycle is invalid"
                    )
                action_states[action_id] = (
                    "validated",
                    record,
                    event.sequence,
                )
            else:
                if prior[0] != "validated" or record.operation_id is None:
                    raise _impl.FinalizationError(
                        "action dispatch commit lifecycle is invalid"
                    )
                if to_json_compatible(prior[1]) != to_json_compatible(record):
                    raise _impl.FinalizationError(
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
                raise _impl.FinalizationError("action terminal identity is invalid")
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
                raise _impl.FinalizationError(
                    "action terminal operation_id does not match dispatch commit"
                )
            if active_attempt != str(record.step_attempt_id):
                raise _impl.FinalizationError(
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
                    raise _impl.FinalizationError(
                        "ACTION_EXECUTED result is invalid"
                    )
                state = "executed"
            action_states[action_id] = (state, record, event.sequence)
            continue

        if kind is EventType.ASSERTION_EVALUATED:
            record = _assertion_record(event.payload.get("assertion"))
            if target_launched is None or target_closed is not None:
                raise _impl.FinalizationError(
                    "assertion occurred outside an active target lifecycle"
                )
            if active_attempt != str(record.step_attempt_id):
                raise _impl.FinalizationError(
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
                raise _impl.FinalizationError("target close lifecycle is invalid")
            target_closed = event.sequence
            continue

        if kind is EventType.ENVIRONMENT_RELEASED:
            if (
                environment_prepared is None
                or environment_released is not None
                or active_attempt is not None
            ):
                raise _impl.FinalizationError(
                    "environment release lifecycle is invalid"
                )
            if target_launched is not None and target_closed is None:
                lifecycle_error = True
            environment_released = event.sequence
            continue

        if kind is EventType.RUN_MARKED_INCOMPLETE:
            if environment_released is None or active_attempt is not None:
                raise _impl.FinalizationError(
                    "RUN_MARKED_INCOMPLETE precedes environment release"
                )
            continue

        if kind is EventType.RUN_COMPLETED:
            raise _impl.FinalizationError(
                "pre-finalization history already contains RUN_COMPLETED"
            )

    if environment_prepared is None:
        raise _impl.FinalizationError("canonical history lacks ENVIRONMENT_PREPARED")
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
        raise _impl.FinalizationError(
            f"canonical step evidence relationships are invalid: {exc}"
        ) from exc

    return unresolved_dispatch, lifecycle_error, structural_integrity_error


def _derive(events, run_id):
    (
        unresolved_dispatch,
        lifecycle_error,
        structural_integrity_error,
    ) = _validate_relationships(events, run_id)
    state = _raw_derive(events, run_id)
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
    return replace(state, status_inputs=inputs)


def _artifacts(store, records):
    """Return canonical JSON after re-verifying retained artifact bytes."""
    try:
        raw = _raw_artifacts(store, records)
    except ArtifactCaptureError as exc:
        raise _impl.FinalizationError(
            "retained artifact cannot be verified safely"
        ) from exc
    try:
        converted = to_json_compatible(raw)
    except ValueError as exc:
        raise _impl.FinalizationError(
            "artifact manifest projection is malformed"
        ) from exc
    if not isinstance(converted, list):
        raise _impl.FinalizationError("artifact manifest projection is malformed")
    return converted


def _assert_directory_identity(directory, label: str) -> None:
    if os.name == "nt":
        return
    if directory._fd is None:
        raise _impl.FinalizationError(
            f"pinned authority unavailable for {label}"
        )
    try:
        named = os.stat(directory.path, follow_symlinks=False)
        pinned = os.fstat(directory._fd)
    except OSError as exc:
        raise _impl.FinalizationError(
            f"{label} namespace cannot be verified"
        ) from exc
    if (
        not stat.S_ISDIR(named.st_mode)
        or (named.st_dev, named.st_ino) != (pinned.st_dev, pinned.st_ino)
    ):
        raise _impl.FinalizationError(
            f"{label} namespace no longer refers to the pinned directory"
        )


def _cleanup_published(directory, name: str) -> None:
    try:
        if os.name == "nt":
            (directory.path / name).unlink(missing_ok=True)
        elif directory._fd is not None:
            try:
                os.unlink(name, dir_fd=directory._fd)
            except FileNotFoundError:
                pass
        directory.fsync()
    except BaseException:
        pass


def _publish(directory, name, data):
    """Publish no-overwrite bytes and prove the final named object before success."""
    expected_size = len(data)
    expected_digest = hashlib.sha256(data).digest()
    _assert_directory_identity(directory, "finalization directory")
    path = _raw_publish(directory, name, data)
    try:
        _assert_directory_identity(directory, "finalization directory")
        actual = _impl._pinned_bytes(
            directory, name, f"finalization member {name}"
        )
        if (
            len(actual) != expected_size
            or not hmac.compare_digest(
                hashlib.sha256(actual).digest(), expected_digest
            )
            or actual != data
        ):
            raise _impl.FinalizationError(
                f"published finalization member differs from source bytes: {name}"
            )
        directory.fsync()
        _assert_directory_identity(directory, "finalization directory")
        confirmed = _impl._pinned_bytes(
            directory, name, f"finalization member {name}"
        )
        if confirmed != data:
            raise _impl.FinalizationError(
                f"published finalization member changed after durability barrier: {name}"
            )
        return path
    except BaseException:
        _cleanup_published(directory, name)
        raise


def _json_object(raw: bytes, label: str):
    def no_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=no_duplicates,
            parse_constant=lambda item: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant {item}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _impl.FinalizationError(
            f"{label} is not strict JSON"
        ) from exc
    if not isinstance(value, dict):
        raise _impl.FinalizationError(f"{label} must contain an object")
    if _impl._json(value) != raw:
        raise _impl.FinalizationError(
            f"{label} is not in canonical persisted representation"
        )
    return value


def _member_by_path(members, path: str, label: str):
    if (
        isinstance(members, (str, bytes, bytearray, Mapping))
        or not isinstance(members, Sequence)
    ):
        raise _impl.FinalizationError(f"{label} members are malformed")
    matches = [
        item
        for item in tuple(members)
        if isinstance(item, Mapping) and item.get("path") == path
    ]
    if len(matches) != 1:
        raise _impl.FinalizationError(
            f"{label} must contain exactly one member for {path}"
        )
    return matches[0]


def _expect_file_digest(meta, raw: bytes, label: str) -> None:
    if not isinstance(meta, Mapping):
        raise _impl.FinalizationError(f"{label} metadata is malformed")
    expected_size = meta.get("size_bytes")
    expected_digest = meta.get("sha256")
    if expected_size is not None:
        if (
            isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size < 0
        ):
            raise _impl.FinalizationError(f"{label} size is invalid")
        if len(raw) != expected_size:
            raise _impl.FinalizationError(f"{label} size does not match binding")
    if not isinstance(expected_digest, str) or not expected_digest.startswith(
        "sha256:"
    ):
        raise _impl.FinalizationError(f"{label} digest is invalid")
    actual = "sha256:" + hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(actual, expected_digest):
        raise _impl.FinalizationError(f"{label} digest does not match binding")


def _preflight_bound_members(root: Path) -> None:
    """Verify exact persisted finalization member bytes using pinned/no-follow handles."""
    run_pin = None
    manifests = None
    try:
        run_pin = _impl._PinnedDirectory(root)
        manifests = _impl._PinnedDirectory(root / "manifests")
        run_pin.assert_child_identity(
            "manifests", manifests, "ATES manifests directory"
        )

        binding_raw = _impl._pinned_bytes(
            run_pin, "run.json", "finalization binding"
        )
        manifest_raw = _impl._pinned_bytes(
            manifests, "manifest-0001.json", "evidence manifest"
        )
        package_raw = _impl._pinned_bytes(
            manifests, "package-manifest-0001.json", "package manifest"
        )
        evidence_raw = _impl._pinned_bytes(
            run_pin, "evidence.jsonl", "canonical evidence"
        )

        binding = _json_object(binding_raw, "run.json")
        manifest = _json_object(manifest_raw, "manifest-0001.json")
        package = _json_object(package_raw, "package-manifest-0001.json")

        bound_manifests = binding.get("manifests")
        if not isinstance(bound_manifests, Mapping):
            raise _impl.FinalizationError(
                "run binding manifest metadata is malformed"
            )
        evidence_binding = bound_manifests.get("evidence")
        package_binding = bound_manifests.get("package")
        if (
            not isinstance(evidence_binding, Mapping)
            or evidence_binding.get("path")
            != "manifests/manifest-0001.json"
        ):
            raise _impl.FinalizationError(
                "run binding evidence-manifest path is invalid"
            )
        if (
            not isinstance(package_binding, Mapping)
            or package_binding.get("path")
            != "manifests/package-manifest-0001.json"
        ):
            raise _impl.FinalizationError(
                "run binding package-manifest path is invalid"
            )
        _expect_file_digest(
            evidence_binding, manifest_raw, "bound evidence manifest"
        )
        _expect_file_digest(
            package_binding, package_raw, "bound package manifest"
        )

        evidence_meta = manifest.get("evidence")
        if (
            not isinstance(evidence_meta, Mapping)
            or evidence_meta.get("path") != "evidence.jsonl"
        ):
            raise _impl.FinalizationError(
                "evidence manifest member metadata is malformed"
            )
        _expect_file_digest(
            evidence_meta, evidence_raw, "canonical evidence"
        )

        package_evidence = _member_by_path(
            package.get("members"),
            "evidence.jsonl",
            "package manifest",
        )
        package_manifest = _member_by_path(
            package.get("members"),
            "manifests/manifest-0001.json",
            "package manifest",
        )
        _expect_file_digest(
            package_evidence, evidence_raw, "package evidence member"
        )
        _expect_file_digest(
            package_manifest,
            manifest_raw,
            "package evidence-manifest member",
        )

        run_pin.assert_child_identity(
            "manifests", manifests, "ATES manifests directory"
        )
        _assert_directory_identity(run_pin, "ATES run directory")
        _assert_directory_identity(
            manifests, "ATES manifests directory"
        )
    except _impl.FinalizationError:
        raise
    except (OSError, _impl.AtesStoreError, ValueError) as exc:
        raise _impl.FinalizationError(
            "finalization members cannot be verified safely"
        ) from exc
    finally:
        if manifests is not None:
            try:
                manifests.close()
            except BaseException:
                pass
        if run_pin is not None:
            try:
                run_pin.close()
            except BaseException:
                pass


def verify_finalized_run(run_dir):
    try:
        root = Path(run_dir).resolve(strict=True)
    except OSError as exc:
        raise _impl.FinalizationError(
            f"cannot resolve finalized run directory: {exc}"
        ) from exc
    _preflight_bound_members(root)
    result = _raw_verify_finalized_run(root)
    _preflight_bound_members(root)
    return result


def finalize_revision_one(store):
    result = _raw_finalize_revision_one(store)
    _preflight_bound_members(result.run_dir)
    verified = _impl._verify_store(store, result.run_dir)
    _preflight_bound_members(result.run_dir)
    return verified


_impl._derive = _derive
_impl._artifacts = _artifacts
_impl._publish = _publish
_impl.verify_finalized_run = verify_finalized_run
_impl.finalize_revision_one = finalize_revision_one
sys.modules[__name__] = _impl
