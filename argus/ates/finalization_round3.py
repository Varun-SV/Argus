"""Additional PR #22 hardening for recovery, provenance, and publication."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

from .core import (
    EventType,
    ExecutionKind,
    RoamSource,
    RunRecord,
    ScriptedSource,
    SourceCommitment,
    StepAttemptStatus,
    to_json_compatible,
)
from .ids import RunId


def _finalization_error(impl, message: str, cause: BaseException | None = None):
    error = impl.FinalizationError(message)
    if cause is not None:
        raise error from cause
    raise error


def _source_commitment(raw: object, label: str, impl) -> SourceCommitment | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        _finalization_error(impl, f"{label} is malformed")
    try:
        return SourceCommitment(
            method=raw["method"],
            value=raw["value"],
            canonicalization_profile=raw.get("canonicalization_profile"),
            verification_ref=raw.get("verification_ref"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        _finalization_error(impl, f"{label} is invalid", exc)


def _run_record(raw: object, expected_run_id: RunId, impl) -> RunRecord:
    if not isinstance(raw, Mapping):
        _finalization_error(impl, "RUN_STARTED run record is malformed")
    source_raw = raw.get("source")
    if not isinstance(source_raw, Mapping):
        _finalization_error(impl, "RUN_STARTED run source is malformed")
    try:
        execution_kind = ExecutionKind(raw["execution_kind"])
        if execution_kind is ExecutionKind.SCRIPTED:
            source = ScriptedSource(
                test_case_id=source_raw["test_case_id"],
                commitment=_source_commitment(
                    source_raw.get("commitment"),
                    "scripted source commitment",
                    impl,
                ),
            )
        else:
            source = RoamSource(
                objective_present=source_raw["objective_present"],
                objective_commitment=_source_commitment(
                    source_raw.get("objective_commitment"),
                    "roam objective commitment",
                    impl,
                ),
                config_commitment=_source_commitment(
                    source_raw.get("config_commitment"),
                    "roam config commitment",
                    impl,
                ),
                policy_ref=source_raw.get("policy_ref"),
            )
        configuration_commitment = _source_commitment(
            raw.get("configuration_commitment"),
            "run configuration commitment",
            impl,
        )
        if configuration_commitment is None:
            raise ValueError("configuration commitment is required")
        record = RunRecord(
            run_id=raw["run_id"],
            execution_kind=execution_kind,
            source=source,
            started_at=impl._time(raw["started_at"], "run started_at"),
            argus_version=raw["argus_version"],
            adapter_type=raw["adapter_type"],
            environment_type=raw["environment_type"],
            evidence_profile=raw["evidence_profile"],
            configuration_commitment=configuration_commitment,
            provider=raw.get("provider"),
            model_provider=raw.get("model_provider"),
            model=raw.get("model"),
        )
    except impl.FinalizationError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        _finalization_error(impl, "RUN_STARTED run record is invalid", exc)

    if record.run_id != expected_run_id:
        _finalization_error(
            impl, "RUN_STARTED run_id does not match the canonical event envelope"
        )
    try:
        persisted = to_json_compatible(raw)
        normalized = to_json_compatible(record)
    except ValueError as exc:
        _finalization_error(impl, "RUN_STARTED run record is not JSON-safe", exc)
    if persisted != normalized:
        _finalization_error(
            impl, "RUN_STARTED run record is not in canonical RunRecord form"
        )
    return record


def _validate_provenance_and_terminal_lifecycle(events, run_id: RunId, impl) -> RunRecord:
    snapshot = tuple(events)
    starts = [
        event
        for event in snapshot
        if event.envelope.event_type is EventType.RUN_STARTED
    ]
    if len(starts) != 1:
        _finalization_error(
            impl, "finalization requires exactly one RUN_STARTED event"
        )
    run = _run_record(starts[0].payload.get("run"), run_id, impl)

    raw_steps = starts[0].payload.get("steps")
    if (
        isinstance(raw_steps, (str, bytes, bytearray, Mapping))
        or not isinstance(raw_steps, Sequence)
    ):
        _finalization_error(impl, "RUN_STARTED steps are malformed")
    step_kinds: list[str] = []
    for item in tuple(raw_steps):
        if not isinstance(item, Mapping):
            _finalization_error(impl, "RUN_STARTED steps must contain objects")
        kind = item.get("kind")
        if not isinstance(kind, str) or not kind.strip():
            _finalization_error(impl, "RUN_STARTED step kind is invalid")
        step_kinds.append(kind)

    if run.execution_kind is ExecutionKind.SCRIPTED and "roam" in step_kinds:
        _finalization_error(impl, "scripted runs cannot declare roam steps")
    if run.execution_kind is ExecutionKind.ROAM and (
        len(step_kinds) != 1 or step_kinds[0] != "roam"
    ):
        _finalization_error(
            impl, "roam runs must declare exactly one canonical roam step"
        )

    target_launched = False
    target_closed = False
    for event in snapshot:
        if event.run_id != run_id:
            _finalization_error(
                impl, "finalization event history mixes run IDs"
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
                impl, "scripted step attempt started before TARGET_LAUNCHED"
            )
        if kind in {EventType.ACTION_EXECUTED, EventType.ACTION_OUTCOME_UNKNOWN}:
            if not target_launched or target_closed:
                _finalization_error(
                    impl,
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


def _assert_directory_identity(directory, label: str, impl) -> None:
    if os.name == "nt":
        return
    if directory._fd is None:
        _finalization_error(impl, f"pinned authority unavailable for {label}")
    try:
        named = os.stat(directory.path, follow_symlinks=False)
        pinned = os.fstat(directory._fd)
    except OSError as exc:
        _finalization_error(impl, f"{label} namespace cannot be verified", exc)
    if (
        not stat.S_ISDIR(named.st_mode)
        or (named.st_dev, named.st_ino) != (pinned.st_dev, pinned.st_ino)
    ):
        _finalization_error(
            impl, f"{label} namespace no longer refers to the pinned directory"
        )


def _durable_remove(directory, name: str, impl) -> None:
    try:
        if os.name == "nt":
            try:
                (directory.path / name).unlink()
            except FileNotFoundError:
                pass
        else:
            if directory._fd is None:
                _finalization_error(
                    impl, "pinned authority unavailable during publication rollback"
                )
            try:
                os.unlink(name, dir_fd=directory._fd)
            except FileNotFoundError:
                pass
        directory.fsync()

        if os.name == "nt":
            try:
                (directory.path / name).lstat()
            except FileNotFoundError:
                pass
            else:
                _finalization_error(
                    impl,
                    f"published finalization member still exists after rollback: {name}",
                )
        else:
            try:
                os.stat(name, dir_fd=directory._fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                _finalization_error(
                    impl,
                    f"published finalization member still exists after rollback: {name}",
                )
        _assert_directory_identity(directory, "finalization directory", impl)
    except impl.FinalizationError:
        raise
    except BaseException as exc:
        _finalization_error(
            impl,
            f"durable rollback of published finalization member failed: {name}",
            exc,
        )


def _strict_json_object(raw: bytes, label: str, impl) -> Mapping[str, object]:
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
        _finalization_error(impl, f"{label} is not strict JSON", exc)
    if not isinstance(value, dict):
        _finalization_error(impl, f"{label} must contain an object")
    if impl._json(value) != raw:
        _finalization_error(
            impl, f"{label} is not in canonical persisted representation"
        )
    return value


def _entry_exists(directory, name: str, impl) -> bool:
    try:
        if os.name == "nt":
            (directory.path / name).lstat()
        else:
            if directory._fd is None:
                _finalization_error(
                    impl, "pinned authority unavailable while inspecting recovery state"
                )
            os.stat(name, dir_fd=directory._fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False
    except impl.FinalizationError:
        raise
    except OSError as exc:
        _finalization_error(
            impl, f"recovery member {name} cannot be inspected safely", exc
        )


def _preflight_recovery_members(project_dir, run_id, impl) -> None:
    try:
        rid = run_id if isinstance(run_id, RunId) else RunId(run_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("run_id must be a valid RunId") from exc
    try:
        project = Path(project_dir).resolve(strict=True)
    except OSError as exc:
        _finalization_error(impl, "recovery project directory is unavailable", exc)

    root = project / ".argus" / "runs" / str(rid)
    if not root.exists():
        return

    run_pin = None
    manifests = None
    store = None
    try:
        run_pin = impl._PinnedDirectory(root)
        if _entry_exists(run_pin, "run.json", impl):
            return

        if not _entry_exists(run_pin, "manifests", impl):
            return
        manifests_path = root / "manifests"
        try:
            manifests = impl._PinnedDirectory(manifests_path)
        except impl.AtesStoreError as exc:
            _finalization_error(
                impl, "ATES manifests recovery namespace is unsafe", exc
            )
        run_pin.assert_child_identity(
            "manifests", manifests, "ATES manifests directory"
        )
        if not _entry_exists(manifests, "manifest-0001.json", impl):
            return

        manifest_raw = impl._pinned_bytes(
            manifests, "manifest-0001.json", "recovery evidence manifest"
        )
        manifest = _strict_json_object(
            manifest_raw, "recovery evidence manifest", impl
        )

        store = impl.AtesEventStore(project, rid)
        outcome, completion = impl._candidate_from_manifest(manifest, rid)
        finals = [
            event
            for event in store.events
            if event.envelope.event_type is EventType.RUN_COMPLETED
        ]
        if finals:
            if (
                len(finals) != 1
                or store.events[-1].canonical_line() != completion.canonical_line()
            ):
                _finalization_error(
                    impl, "existing completion differs from recovery candidate"
                )
            pre = store.events[:-1]
        else:
            pre = store.events
        if completion.sequence != len(pre) + 1:
            _finalization_error(
                impl, "recovery completion sequence is inconsistent"
            )

        state = impl._derive(pre, rid)
        if (
            outcome.status_policy_version != impl.STATUS_POLICY_VERSION
            or impl.derive_run_status(state.status_inputs)
            is not outcome.effective_status
        ):
            _finalization_error(
                impl, "recovery outcome differs from canonical derivation"
            )
        artifacts = impl._artifacts(store, state.artifacts)
        expected_manifest, expected_package, expected_evidence = impl._documents(
            pre, completion, outcome, artifacts
        )
        expected_manifest_raw = impl._json(expected_manifest)
        if manifest_raw != expected_manifest_raw:
            _finalization_error(
                impl,
                "recovery evidence manifest bytes differ from regenerated candidate",
            )

        package_exists = _entry_exists(
            manifests, "package-manifest-0001.json", impl
        )
        if package_exists:
            package_raw = impl._pinned_bytes(
                manifests, "package-manifest-0001.json", "recovery package manifest"
            )
            _strict_json_object(package_raw, "recovery package manifest", impl)
            if package_raw != impl._json(expected_package):
                _finalization_error(
                    impl,
                    "recovery package manifest bytes differ from regenerated candidate",
                )

        if finals and store._read_all() != expected_evidence:
            _finalization_error(
                impl, "recovered evidence differs from manifest-bound candidate"
            )

        run_pin.assert_child_identity(
            "manifests", manifests, "ATES manifests directory"
        )
        _assert_directory_identity(run_pin, "ATES run directory", impl)
        _assert_directory_identity(manifests, "ATES manifests directory", impl)
    except impl.FinalizationError:
        raise
    except (OSError, impl.AtesStoreError, ValueError) as exc:
        _finalization_error(
            impl, "recovery members cannot be preflighted safely", exc
        )
    finally:
        if store is not None:
            try:
                store.close()
            except BaseException:
                pass
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


def install(impl) -> None:
    """Install hardening after the PR #22 compatibility layer is initialized."""
    base_derive = impl._derive
    base_recover = impl.recover_revision_one

    def derive(events, run_id):
        snapshot = tuple(events)
        _validate_provenance_and_terminal_lifecycle(snapshot, run_id, impl)
        state = base_derive(snapshot, run_id)
        if _has_effective_attempt_execution_error(snapshot):
            state = replace(
                state,
                status_inputs=replace(
                    state.status_inputs,
                    execution_error=True,
                ),
            )
        return state

    def publish(directory, name, data):
        expected_size = len(data)
        expected_digest = hashlib.sha256(data).digest()
        _assert_directory_identity(directory, "finalization directory", impl)
        path = impl._old._publish_no_overwrite(directory, name, data)
        try:
            _assert_directory_identity(directory, "finalization directory", impl)
            actual = impl._pinned_bytes(
                directory, name, f"finalization member {name}"
            )
            if (
                len(actual) != expected_size
                or not hmac.compare_digest(
                    hashlib.sha256(actual).digest(), expected_digest
                )
                or actual != data
            ):
                _finalization_error(
                    impl,
                    f"published finalization member differs from source bytes: {name}",
                )
            directory.fsync()
            _assert_directory_identity(directory, "finalization directory", impl)
            confirmed = impl._pinned_bytes(
                directory, name, f"finalization member {name}"
            )
            if confirmed != data:
                _finalization_error(
                    impl,
                    f"published finalization member changed after durability barrier: {name}",
                )
            return path
        except BaseException as primary:
            try:
                _durable_remove(directory, name, impl)
            except BaseException as cleanup:
                raise impl.FinalizationError(
                    "finalization publication verification failed and durable "
                    f"rollback is incomplete or ambiguous: {name}"
                ) from cleanup
            raise primary

    def recover(project_dir, run_id):
        _preflight_recovery_members(project_dir, run_id, impl)
        return base_recover(project_dir, run_id)

    impl._derive = derive
    impl._publish = publish
    impl.recover_revision_one = recover
