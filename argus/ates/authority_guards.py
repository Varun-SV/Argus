"""Authority, namespace, and structural-identifier guards for ATES.

This module centralizes validation rules that must agree across canonical evidence,
finalization verification, report publication, and detached approval authority.
It patches internal hooks only so existing public APIs remain stable.
"""
from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace

from .core import (
    EventType,
    ExecutionKind,
    StepAttemptStatus,
    VerificationStatus,
    to_json_compatible,
)
from .finalization_types import FinalizationError
from .ids import RunId


_MACHINE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@:/+#~-]{0,127}$")
_RETRYABLE_PREDECESSORS = frozenset(
    {StepAttemptStatus.FAILED, StepAttemptStatus.ERROR}
)
_SCRIPTED_STEP_KINDS = frozenset({"step", "setup", "teardown", "assert"})
_APPROVAL_HISTORY: ContextVar[tuple[Mapping[str, object], ...]] = ContextVar(
    "ates_approval_history", default=()
)
_EXPECTED_BOUND_CHAIN: ContextVar[object | None] = ContextVar(
    "ates_expected_bound_chain", default=None
)
_ACTIVE_REPORT_AUTHORITY: ContextVar[dict[str, object] | None] = ContextVar(
    "ates_report_authority", default=None
)


def _validate_machine_identifier(value: object, label: str) -> None:
    if not isinstance(value, str) or not _MACHINE_IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} must be a bounded machine-safe identifier")


def _validate_optional_machine_identifier(value: object, label: str) -> None:
    if value is None:
        return
    _validate_machine_identifier(value, label)


def _validate_retry_predecessors(events, ev) -> None:
    """Allow retries only after terminal states Argus actually treats as retryable."""
    snapshot = tuple(events)
    completed: dict[str, object] = {}
    started_ids: set[str] = set()
    for event in snapshot:
        kind = event.envelope.event_type
        if kind is EventType.STEP_ATTEMPT_STARTED:
            rec = ev._attempt(event.payload.get("attempt"), running=True)
            started_ids.add(str(rec.step_attempt_id))
        elif kind is EventType.STEP_ATTEMPT_COMPLETED:
            rec = ev._attempt(event.payload.get("attempt"), running=False)
            completed[str(rec.step_attempt_id)] = rec

    for event in snapshot:
        if event.envelope.event_type is not EventType.STEP_RETRY_SCHEDULED:
            continue
        previous = event.payload.get("previous_step_attempt_id")
        prior = completed.get(previous) if isinstance(previous, str) else None
        if prior is None or prior.status in _RETRYABLE_PREDECESSORS:
            continue
        next_id = event.payload.get("next_step_attempt_id")
        if (
            isinstance(next_id, str)
            and next_id in started_ids
            and next_id not in completed
        ):
            raise FinalizationError(
                "unfinished retry follows non-retryable terminal step attempt "
                f"status {prior.status.value}"
            )
        raise FinalizationError(
            f"step attempt status {prior.status.value} is not retryable by an ordinary retry"
        )


def _validate_full_terminal_handoff(events) -> None:
    """Full finalization accepts exactly one producer handoff and nothing after it."""
    snapshot = tuple(events)
    markers = [
        index
        for index, event in enumerate(snapshot)
        if event.envelope.event_type is EventType.RUN_MARKED_INCOMPLETE
    ]
    if len(markers) != 1:
        raise FinalizationError(
            "full finalization requires exactly one RUN_MARKED_INCOMPLETE producer handoff"
        )
    if markers[0] != len(snapshot) - 1:
        raise FinalizationError(
            "RUN_MARKED_INCOMPLETE must be the final pre-completion producer event"
        )


def _validate_structural_evidence_identifiers(events) -> None:
    """Prevent report-visible structural fields from becoming plaintext channels."""
    for event in tuple(events):
        kind = event.envelope.event_type
        if kind is EventType.RUN_STARTED:
            run = event.payload.get("run")
            if isinstance(run, Mapping):
                for field in ("adapter_type", "environment_type", "evidence_profile"):
                    _validate_machine_identifier(
                        run.get(field), f"RUN_STARTED {field}"
                    )
                for field in ("provider", "model_provider"):
                    _validate_optional_machine_identifier(
                        run.get(field), f"RUN_STARTED {field}"
                    )
                source = run.get("source")
                if isinstance(source, Mapping):
                    _validate_machine_identifier(
                        source.get("kind"), "RUN_STARTED source kind"
                    )
                    _validate_optional_machine_identifier(
                        source.get("policy_ref"), "RUN_STARTED source policy_ref"
                    )

            raw_steps = event.payload.get("steps")
            if isinstance(raw_steps, (list, tuple)):
                execution_kind = run.get("execution_kind") if isinstance(run, Mapping) else None
                kinds = [
                    item.get("kind")
                    for item in raw_steps
                    if isinstance(item, Mapping)
                ]
                if execution_kind == ExecutionKind.SCRIPTED.value:
                    unexpected = [item for item in kinds if item not in _SCRIPTED_STEP_KINDS]
                    if unexpected:
                        raise FinalizationError(
                            "scripted runs may use only step/setup/teardown/assert step kinds"
                        )
                elif execution_kind == ExecutionKind.ROAM.value and kinds != ["roam"]:
                    raise FinalizationError(
                        "roam runs must declare exactly one canonical roam step"
                    )

        elif kind is EventType.OBSERVATION_CAPTURED:
            record = event.payload.get("observation")
            if isinstance(record, Mapping):
                _validate_machine_identifier(
                    record.get("source"), "observation source"
                )
                _validate_machine_identifier(
                    record.get("capture_policy"), "observation capture_policy"
                )

        elif kind is EventType.FINDING_RECORDED:
            record = event.payload.get("finding")
            if isinstance(record, Mapping):
                _validate_machine_identifier(
                    record.get("classification_source", "model"),
                    "finding classification_source",
                )
                _validate_machine_identifier(
                    record.get("classification", "unclassified"),
                    "finding classification",
                )

        elif kind in {EventType.CHECKPOINT_CAPTURED, EventType.ARTIFACT_COLLECTED}:
            record = event.payload.get("artifact")
            if isinstance(record, Mapping):
                for field in ("kind", "sensitivity", "capture_policy"):
                    _validate_machine_identifier(
                        record.get(field), f"retained artifact {field}"
                    )
                for field in ("access_policy", "retention_policy", "authorization_ref"):
                    _validate_optional_machine_identifier(
                        record.get(field), f"retained artifact {field}"
                    )

        elif kind is EventType.ARTIFACT_SUPPRESSED:
            for field in ("context", "kind", "capture_policy", "reason"):
                _validate_machine_identifier(
                    event.payload.get(field), f"artifact suppression {field}"
                )


def _build_candidate_result(template):
    try:
        rid = RunId(template.get("run_id"))
    except (TypeError, ValueError):
        return None
    evidence_revision = template.get("evidence_revision")
    finalization_id = template.get("finalization_id")
    if (
        isinstance(evidence_revision, bool)
        or not isinstance(evidence_revision, int)
        or evidence_revision < 1
    ):
        return None
    return SimpleNamespace(
        outcome=SimpleNamespace(
            run_id=rid,
            finalization_id=finalization_id,
            evidence_revision=evidence_revision,
        )
    )


def _candidate_current_package_error(candidate, template, audit):
    """Validate retry candidates against real chronological approval history."""
    manifest_digest = template.get("manifest_digest")
    if not isinstance(manifest_digest, str):
        return "approval candidate manifest binding is invalid"
    result = _build_candidate_result(template)
    if result is None:
        return "approval candidate is bound to an invalid current package"

    history = _APPROVAL_HISTORY.get()
    candidate_index = None
    for index, record in enumerate(history):
        if record is candidate:
            candidate_index = index
            break
    if candidate_index is None and isinstance(candidate, Mapping):
        approval_id = candidate.get("approval_id")
        matches = [
            index
            for index, record in enumerate(history)
            if record.get("approval_id") == approval_id
        ]
        if len(matches) == 1:
            candidate_index = matches[0]
    if candidate_index is None:
        return "approval candidate is not present in the current ledger history"

    seen: dict[str, Mapping[str, object]] = {}
    for record in history[:candidate_index]:
        # The canonical structural validator adds an ID to `seen` only when the
        # row is fully valid, including its own supersession/generation anchors.
        audit._approval_structural_error(
            record,
            result=result,
            manifest_digest=manifest_digest,
            seen=seen,
        )
    return audit._approval_structural_error(
        candidate,
        result=result,
        manifest_digest=manifest_digest,
        seen=seen,
    )


def _pin_identity(pin) -> tuple[int, int]:
    if os.name != "nt" and getattr(pin, "_fd", None) is not None:
        info = os.fstat(pin._fd)
    else:
        info = os.stat(pin.path, follow_symlinks=False)
    return info.st_dev, info.st_ino


def _chain_snapshot(chain) -> tuple[tuple[Path, tuple[int, int]], ...]:
    return (
        (Path(chain.project.path), _pin_identity(chain.project)),
        (Path(chain.argus.path), _pin_identity(chain.argus)),
        (Path(chain.runs.path), _pin_identity(chain.runs)),
        (Path(chain.run.path), _pin_identity(chain.run)),
    )


def _report_chain_snapshot(context: Mapping[str, object]):
    return (
        (Path(context["project_pin"].path), _pin_identity(context["project_pin"])),
        (Path(context["argus_pin"].path), _pin_identity(context["argus_pin"])),
        (Path(context["runs_pin"].path), _pin_identity(context["runs_pin"])),
        (Path(context["run_pin"].path), _pin_identity(context["run_pin"])),
    )


def _assert_chain_snapshot(snapshot, store_module) -> dict[str, object]:
    project_path, argus_path, runs_path, run_path = [item[0] for item in snapshot]
    pins: dict[str, object] = {}
    try:
        pins["project"] = store_module._PinnedDirectory(project_path)
        pins["argus"] = store_module._PinnedDirectory(argus_path)
        pins["runs"] = store_module._PinnedDirectory(runs_path)
        pins["run"] = store_module._PinnedDirectory(run_path)
        for pin, (_path, expected) in zip(
            (pins["project"], pins["argus"], pins["runs"], pins["run"]),
            snapshot,
        ):
            if _pin_identity(pin) != expected:
                raise FinalizationError(
                    "canonical run namespace identity changed during verification"
                )
        pins["project"].assert_child_identity(".argus", pins["argus"], ".argus")
        pins["argus"].assert_child_identity("runs", pins["runs"], ".argus/runs")
        pins["runs"].assert_child_identity(
            run_path.name, pins["run"], "ATES run directory"
        )
        return pins
    except BaseException:
        for pin in reversed(tuple(pins.values())):
            try:
                pin.close()
            except BaseException:
                pass
        raise


def _preflight_bound_members_with_snapshot(root, snapshot, finalization, fio, store_module):
    """Run the final byte preflight through the exact captured namespace identity."""
    pins = _assert_chain_snapshot(snapshot, store_module)
    manifests = None
    try:
        run_pin = pins["run"]
        if Path(run_pin.path) != Path(root):
            raise FinalizationError("bound preflight resolved to another run directory")
        manifests = store_module._PinnedDirectory(Path(root) / "manifests")
        run_pin.assert_child_identity(
            "manifests", manifests, "ATES manifests directory"
        )

        binding_raw = fio._pinned_bytes(run_pin, "run.json", "finalization binding")
        manifest_raw = fio._pinned_bytes(
            manifests, "manifest-0001.json", "evidence manifest"
        )
        package_raw = fio._pinned_bytes(
            manifests, "package-manifest-0001.json", "package manifest"
        )
        evidence_raw = fio._pinned_bytes(
            run_pin, "evidence.jsonl", "canonical evidence"
        )
        binding = fio._json_object(binding_raw, "run.json")
        manifest = fio._json_object(manifest_raw, "manifest-0001.json")
        package = fio._json_object(package_raw, "package-manifest-0001.json")

        bound_manifests = binding.get("manifests")
        if not isinstance(bound_manifests, Mapping):
            raise FinalizationError("run binding manifest metadata is malformed")
        evidence_binding = bound_manifests.get("evidence")
        package_binding = bound_manifests.get("package")
        if (
            not isinstance(evidence_binding, Mapping)
            or evidence_binding.get("path") != "manifests/manifest-0001.json"
        ):
            raise FinalizationError("run binding evidence-manifest path is invalid")
        if (
            not isinstance(package_binding, Mapping)
            or package_binding.get("path") != "manifests/package-manifest-0001.json"
        ):
            raise FinalizationError("run binding package-manifest path is invalid")
        fio._expect_file_digest(
            evidence_binding, manifest_raw, "bound evidence manifest"
        )
        fio._expect_file_digest(
            package_binding, package_raw, "bound package manifest"
        )

        evidence_meta = manifest.get("evidence")
        if (
            not isinstance(evidence_meta, Mapping)
            or evidence_meta.get("path") != "evidence.jsonl"
        ):
            raise FinalizationError("evidence manifest member metadata is malformed")
        fio._expect_file_digest(evidence_meta, evidence_raw, "canonical evidence")
        package_evidence = fio._member_by_path(
            package.get("members"), "evidence.jsonl", "package manifest"
        )
        package_manifest = fio._member_by_path(
            package.get("members"),
            "manifests/manifest-0001.json",
            "package manifest",
        )
        fio._expect_file_digest(
            package_evidence, evidence_raw, "package evidence member"
        )
        fio._expect_file_digest(
            package_manifest, manifest_raw, "package evidence-manifest member"
        )

        run_pin.assert_child_identity(
            "manifests", manifests, "ATES manifests directory"
        )
        fio._assert_directory_identity(run_pin, "ATES run directory")
        fio._assert_directory_identity(manifests, "ATES manifests directory")
        # Re-prove the complete ancestry after all member reads.
        pins["project"].assert_child_identity(".argus", pins["argus"], ".argus")
        pins["argus"].assert_child_identity("runs", pins["runs"], ".argus/runs")
        pins["runs"].assert_child_identity(
            Path(root).name, run_pin, "ATES run directory"
        )
    finally:
        if manifests is not None:
            try:
                manifests.close()
            except BaseException:
                pass
        for pin in reversed(tuple(pins.values())):
            try:
                pin.close()
            except BaseException:
                pass


def _active_report_pin(directory: Path, finalization, store_module):
    context = _ACTIVE_REPORT_AUTHORITY.get()
    if context is None:
        return None
    root = Path(context["root"])
    directory = Path(directory)
    if directory == root:
        return context["run_pin"]
    if directory == root / "reports":
        return context["reports_pin"]
    if directory == root / "manifests":
        pin = context.get("manifests_pin")
        if pin is None:
            pin = store_module._PinnedDirectory(root / "manifests")
            context["run_pin"].assert_child_identity(
                "manifests", pin, "ATES manifests directory"
            )
            context["manifests_pin"] = pin
        return pin
    return None


def install() -> None:
    """Install idempotent authority guards after the base trust guards."""
    from . import audit as audit
    from . import evidence_validation as ev
    from . import finalization as finalization
    from . import finalization_io as fio
    from . import reports as reports
    from . import store as store_module
    from . import trust_guards as trust_guards

    if getattr(audit, "_ates_authority_guards_installed", False):
        return

    base_retry_semantics = trust_guards._validate_retry_semantics
    base_incomplete_prefix = trust_guards._validate_incomplete_prefix
    base_record_extensions = ev._validate_record_extensions
    base_derive = finalization._derive
    base_outcome = finalization._outcome
    base_verify_store = finalization._verify_store
    base_preflight_bound_members = finalization._preflight_bound_members
    base_credential_post_init = audit.ApprovalCredential.__post_init__
    base_new_approval_record = audit._new_approval_record
    base_approval_structural_error = audit._approval_structural_error
    base_authentication_status = audit._authentication_status
    base_candidate_state = audit._candidate_state
    base_approval_request_matches = audit._approval_request_matches
    base_read_jsonl = audit._read_jsonl
    base_audit_pinned_bytes = audit._pinned_bytes
    base_report_transaction = reports._report_transaction
    base_report_pinned_bytes = reports._pinned_bytes
    base_member_snapshot = reports._member_snapshot

    def guarded_retry_semantics(events, validator):
        base_retry_semantics(events, validator)
        _validate_retry_predecessors(events, validator)

    def guarded_incomplete_prefix(events, run_id, validator):
        base_incomplete_prefix(events, run_id, validator)
        _validate_retry_predecessors(events, validator)

    def guarded_record_extensions(events):
        base_record_extensions(events)
        try:
            _validate_structural_evidence_identifiers(events)
        except ValueError as exc:
            raise FinalizationError(str(exc)) from exc

    def guarded_derive(events, run_id):
        _validate_full_terminal_handoff(events)
        return base_derive(events, run_id)

    def guarded_outcome(value):
        if not isinstance(value, Mapping):
            raise FinalizationError("finalization record is missing/malformed")
        for field in ("revision", "evidence_revision"):
            raw = value.get(field)
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
                raise FinalizationError(
                    f"finalization {field} must be a positive JSON integer"
                )
        if not isinstance(value.get("status_policy_version"), str):
            raise FinalizationError("finalization status_policy_version must be a string")
        return base_outcome(value)

    def guarded_verify_store(store, root):
        context = _ACTIVE_REPORT_AUTHORITY.get()
        if context is not None and Path(root) == Path(context["root"]):
            directories = store._directories
            if directories is None or _chain_snapshot(directories) != _report_chain_snapshot(context):
                raise FinalizationError(
                    "report verification resolved to another canonical run namespace"
                )

        events = store.events
        if events and events[-1].envelope.event_type is EventType.RUN_COMPLETED:
            final = events[-1]
            outcome = guarded_outcome(final.payload.get("finalization"))
            if finalization._json(to_json_compatible(final.payload)) != finalization._json(
                finalization._payload(outcome)
            ):
                raise FinalizationError(
                    "RUN_COMPLETED does not match normalized outcome with canonical JSON types"
                )
        result = base_verify_store(store, root)
        directories = store._directories
        if directories is None:
            raise FinalizationError("run authority unavailable after verification")
        _EXPECTED_BOUND_CHAIN.set((Path(root), _chain_snapshot(directories)))
        return result

    def guarded_preflight_bound_members(root):
        root = Path(root)
        expected = _EXPECTED_BOUND_CHAIN.get()
        context = _ACTIVE_REPORT_AUTHORITY.get()
        snapshot = None
        consume_expected = False
        if expected is not None and expected[0] == root:
            snapshot = expected[1]
            consume_expected = True
        if context is not None and Path(context["root"]) == root:
            report_snapshot = _report_chain_snapshot(context)
            if snapshot is not None and snapshot != report_snapshot:
                if consume_expected:
                    _EXPECTED_BOUND_CHAIN.set(None)
                raise FinalizationError(
                    "bound verification and report transaction disagree on run namespace"
                )
            snapshot = report_snapshot
        if snapshot is None:
            return base_preflight_bound_members(root)
        try:
            return _preflight_bound_members_with_snapshot(
                root, snapshot, finalization, fio, store_module
            )
        finally:
            if consume_expected:
                _EXPECTED_BOUND_CHAIN.set(None)

    def guarded_credential_post_init(self):
        base_credential_post_init(self)
        try:
            trust_guards._validate_audit_actor(self.actor)
            _validate_machine_identifier(self.key_id, "approval credential key_id")
            for role in self.roles:
                _validate_machine_identifier(role, "approval credential role")
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

    def guarded_new_approval_record(*args, **kwargs):
        try:
            _validate_machine_identifier(kwargs.get("role"), "approval role")
            key_id = kwargs.get("key_id")
            if key_id is not None:
                _validate_machine_identifier(key_id, "approval key_id")
        except ValueError as exc:
            raise audit.ApprovalError(str(exc)) from exc
        return base_new_approval_record(*args, **kwargs)

    def guarded_approval_structural_error(record, *args, **kwargs):
        if isinstance(record, Mapping):
            try:
                _validate_machine_identifier(record.get("role"), "approval role")
                auth = record.get("authentication")
                if isinstance(auth, Mapping):
                    key_id = auth.get("key_id")
                    if key_id is not None:
                        _validate_machine_identifier(key_id, "approval key_id")
            except ValueError as exc:
                return str(exc)
        return base_approval_structural_error(record, *args, **kwargs)

    def guarded_authentication_status(record, resolver):
        if isinstance(record, Mapping):
            try:
                _validate_machine_identifier(record.get("role"), "approval role")
                auth = record.get("authentication")
                if isinstance(auth, Mapping):
                    key_id = auth.get("key_id")
                    if key_id is not None:
                        _validate_machine_identifier(key_id, "approval key_id")
            except ValueError as exc:
                return VerificationStatus.INVALID, str(exc)
        return base_authentication_status(record, resolver)

    def guarded_read_jsonl(root, name):
        records = base_read_jsonl(root, name)
        if name == "approvals.jsonl":
            _APPROVAL_HISTORY.set(tuple(records))
        return records

    def guarded_approval_request_matches(record, template, authentication_key):
        if not base_approval_request_matches(record, template, authentication_key):
            return False
        # The preliminary live-generation scan has not yet derived its anchor.
        # Once the request template carries one, it is immutable request identity.
        if "request_generation_after_approval_id" in template:
            return record.get("request_generation_after_approval_id") == template.get(
                "request_generation_after_approval_id"
            )
        return True

    def guarded_candidate_state(candidate, template, authentication_key, audits_by_approval):
        if not audit._approval_request_matches(candidate, template, authentication_key):
            return "conflict"
        structural_error = _candidate_current_package_error(candidate, template, audit)
        if structural_error is not None:
            return "conflict"
        return base_candidate_state(
            candidate, template, authentication_key, audits_by_approval
        )

    @contextmanager
    def guarded_report_transaction(root):
        project_pin = argus_pin = runs_pin = None
        with base_report_transaction(root) as (run_pin, reports_pin, lock):
            try:
                root = Path(root)
                project = root.parent.parent.parent
                project_pin = store_module._PinnedDirectory(project)
                argus_pin = store_module._PinnedDirectory(project / ".argus")
                runs_pin = store_module._PinnedDirectory(project / ".argus" / "runs")
                project_pin.assert_child_identity(".argus", argus_pin, ".argus")
                argus_pin.assert_child_identity("runs", runs_pin, ".argus/runs")
                runs_pin.assert_child_identity(root.name, run_pin, "ATES run directory")
                run_pin.assert_child_identity(
                    "reports", reports_pin, "ATES reports directory"
                )
                context: dict[str, object] = {
                    "root": root,
                    "project_pin": project_pin,
                    "argus_pin": argus_pin,
                    "runs_pin": runs_pin,
                    "run_pin": run_pin,
                    "reports_pin": reports_pin,
                    "manifests_pin": None,
                }
                token = _ACTIVE_REPORT_AUTHORITY.set(context)
                try:
                    yield run_pin, reports_pin, lock
                    lock.assert_authoritative()
                    run_pin.assert_child_identity(
                        "reports", reports_pin, "ATES reports directory"
                    )
                    runs_pin.assert_child_identity(root.name, run_pin, "ATES run directory")
                    argus_pin.assert_child_identity("runs", runs_pin, ".argus/runs")
                    project_pin.assert_child_identity(".argus", argus_pin, ".argus")
                finally:
                    _ACTIVE_REPORT_AUTHORITY.reset(token)
                    manifests_pin = context.get("manifests_pin")
                    if manifests_pin is not None:
                        try:
                            manifests_pin.close()
                        except BaseException:
                            pass
            except reports.ReportError:
                raise
            except (OSError, store_module.AtesStoreError) as exc:
                raise reports.ReportError(
                    "canonical run namespace changed during report publication"
                ) from exc
            finally:
                for pin in (runs_pin, argus_pin, project_pin):
                    if pin is not None:
                        try:
                            pin.close()
                        except BaseException:
                            pass

    def guarded_report_pinned_bytes(directory, name, label):
        pin = _active_report_pin(Path(directory), finalization, store_module)
        if pin is None:
            return base_report_pinned_bytes(directory, name, label)
        try:
            return fio._pinned_bytes(pin, name, label)
        except (OSError, store_module.AtesStoreError, FinalizationError) as exc:
            raise reports.ReportError(f"{label} cannot be read safely") from exc

    def guarded_audit_pinned_bytes(root, name, *, missing_ok=False):
        pin = _active_report_pin(Path(root), finalization, store_module)
        if pin is None:
            return base_audit_pinned_bytes(root, name, missing_ok=missing_ok)
        try:
            if not fio._entry_exists(pin, name):
                if missing_ok:
                    return b""
                raise audit.ApprovalError(
                    f"cannot read detached ledger {name} safely"
                )
            return fio._pinned_bytes(pin, name, f"detached ledger {name}")
        except audit.ApprovalError:
            raise
        except (OSError, store_module.AtesStoreError, FinalizationError) as exc:
            raise audit.ApprovalError(
                f"cannot read detached ledger {name} safely"
            ) from exc

    def guarded_member_snapshot(root, name):
        context = _ACTIVE_REPORT_AUTHORITY.get()
        if context is None or Path(root) != Path(context["root"]):
            return base_member_snapshot(root, name)
        run_pin = context["run_pin"]
        if not fio._entry_exists(run_pin, name):
            return {"path": name, "state": "absent", "size_bytes": 0, "sha256": None}
        raw = fio._pinned_bytes(run_pin, name, f"detached ledger snapshot {name}")
        return {
            "path": name,
            "state": "present",
            "size_bytes": len(raw),
            "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
        }

    def guarded_verified_events(root):
        result = reports.verify_finalized_run(root)
        store = None
        try:
            store = store_module.AtesEventStore(
                Path(root).parent.parent.parent, result.outcome.run_id
            )
            context = _ACTIVE_REPORT_AUTHORITY.get()
            if context is not None and Path(root) == Path(context["root"]):
                directories = store._directories
                if directories is None or _chain_snapshot(directories) != _report_chain_snapshot(context):
                    raise reports.ReportError(
                        "verified evidence resolved to another canonical run namespace"
                    )
            if store.run_dir.resolve(strict=True) != Path(root):
                raise reports.ReportError("run binding resolves to another run directory")
            return result, tuple(store.events)
        except reports.ReportError:
            raise
        except (OSError, store_module.AtesStoreError, ValueError) as exc:
            raise reports.ReportError("cannot open verified canonical evidence") from exc
        finally:
            if store is not None:
                try:
                    store.close()
                except BaseException:
                    pass

    trust_guards._validate_retry_semantics = guarded_retry_semantics
    trust_guards._validate_incomplete_prefix = guarded_incomplete_prefix
    ev._validate_record_extensions = guarded_record_extensions
    ev.derive_evidence_state = guarded_derive
    finalization._derive = guarded_derive
    finalization._outcome = guarded_outcome
    finalization._verify_store = guarded_verify_store
    finalization._preflight_bound_members = guarded_preflight_bound_members
    audit.ApprovalCredential.__post_init__ = guarded_credential_post_init
    audit._new_approval_record = guarded_new_approval_record
    audit._approval_structural_error = guarded_approval_structural_error
    audit._authentication_status = guarded_authentication_status
    audit._read_jsonl = guarded_read_jsonl
    audit._approval_request_matches = guarded_approval_request_matches
    audit._candidate_state = guarded_candidate_state
    audit._pinned_bytes = guarded_audit_pinned_bytes
    reports._report_transaction = guarded_report_transaction
    reports._pinned_bytes = guarded_report_pinned_bytes
    reports._member_snapshot = guarded_member_snapshot
    reports._verified_events = guarded_verified_events
    audit._ates_authority_guards_installed = True


__all__ = ["install"]
