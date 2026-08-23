"""Transactional ATES run finalization and manifest verification.

The event stream remains the canonical fact log. A visible ``RUN_COMPLETED``
event alone is deliberately *not* the authority that a run finalized: the
final ``run.json`` binding is published last and acts as the commit marker for
one finalization revision.

Revision 1 uses this order:

1. validate the pre-finalization canonical event history;
2. derive status from evidence, never from a legacy producer status;
3. construct the exact candidate RUN_COMPLETED bytes;
4. build/publish manifests over the exact final evidence candidate;
5. append that exact RUN_COMPLETED event durably;
6. verify persisted evidence bytes match the manifest candidate;
7. publish run.json last as the durable finalization binding.

A crash before step 7 leaves an incomplete/recoverable run. Consumers must not
interpret an unbound RUN_COMPLETED as an authoritative pass.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Mapping, Optional, Sequence

from .core import (
    ATES_VERSION,
    STATUS_POLICY_VERSION,
    AssertionResult,
    EventEnvelope,
    EventType,
    FinalizationId,
    RunId,
    RunOutcomeRevision,
    RunStatus,
    StepAttemptStatus,
    to_json_compatible,
    validate_artifact_path,
)
from .ids import EventId
from .status import StatusInputs, derive_run_status
from .store import AtesEventStore, AtesStoreError, StoredEvent, _PinnedDirectory, _open_regular_file

MANIFEST_VERSION = "ates-manifest-v1"
PACKAGE_MANIFEST_VERSION = "ates-package-manifest-v1"
FINALIZATION_BINDING_VERSION = "ates-finalization-binding-v1"
EVIDENCE_DIGEST_PROFILE = "ates-canonical-evidence-jsonl-v1"
PROTECTED_ARTIFACT_COMMITMENT_PROFILE = "ates-artifact-sha256-hmac-v1"
_ARTIFACT_HMAC_KEY_FILENAME = ".ates-artifact-hmac-key"
_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")


class FinalizationError(RuntimeError):
    """Canonical evidence cannot be finalized safely."""


class FinalizationTrustState(str, Enum):
    REGENERATED_VERIFIED = "regenerated_verified"
    BOUND_VERIFIED = "bound_verified"
    UNVERIFIED_DERIVED = "unverified_derived"
    INVALID = "invalid"


@dataclass(frozen=True)
class FinalizationResult:
    outcome: RunOutcomeRevision
    run_dir: Path
    evidence_manifest_path: Path
    package_manifest_path: Path
    binding_path: Path
    trust_state: FinalizationTrustState


@dataclass(frozen=True)
class _EvidenceState:
    run_id: RunId
    steps: tuple[Mapping[str, object], ...]
    final_attempt_by_step: Mapping[str, Mapping[str, object]]
    final_attempt_id_by_step: Mapping[str, str]
    assertions: tuple[Mapping[str, object], ...]
    artifacts: tuple[Mapping[str, object], ...]
    status_inputs: StatusInputs


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8") + b"\n"
    except (TypeError, ValueError, RecursionError) as exc:
        raise FinalizationError(f"cannot serialize canonical finalization JSON: {exc}") from exc


def _write_all(handle, data: bytes) -> None:
    view = memoryview(data)
    offset = 0
    while offset < len(view):
        written = handle.write(view[offset:])
        if isinstance(written, bool) or not isinstance(written, int) or written <= 0:
            raise FinalizationError("finalization write made no forward progress")
        offset += written


def _publish_no_overwrite(directory: _PinnedDirectory, name: str, data: bytes) -> Path:
    if not _SAFE_NAME_RE.fullmatch(name) or name in {".", ".."}:
        raise FinalizationError("invalid finalization filename")
    temp_name = f".{name}.argus-{uuid.uuid4().hex}.part"
    handle = None
    final_created = False
    try:
        handle, created = _open_regular_file(directory, temp_name)
        if not created:
            raise FinalizationError("finalization temporary filename already exists")
        with handle:
            _write_all(handle, data)
            handle.flush()
            os.fsync(handle.fileno())
        handle = None

        if os.name == "nt":
            try:
                os.rename(directory.path / temp_name, directory.path / name)
            except FileExistsError as exc:
                raise FinalizationError(f"finalization file already exists: {name}") from exc
            final_created = True
        else:
            if directory._fd is None:  # pragma: no cover - defensive
                raise FinalizationError("pinned finalization directory has no descriptor")
            try:
                os.link(
                    temp_name,
                    name,
                    src_dir_fd=directory._fd,
                    dst_dir_fd=directory._fd,
                    follow_symlinks=False,
                )
                final_created = True
                os.unlink(temp_name, dir_fd=directory._fd)
            except FileExistsError as exc:
                raise FinalizationError(f"finalization file already exists: {name}") from exc
        directory.fsync()
        return directory.path / name
    except BaseException as exc:
        cleanup_error: Optional[BaseException] = None
        try:
            if os.name == "nt":
                (directory.path / temp_name).unlink(missing_ok=True)
            elif directory._fd is not None:
                try:
                    os.unlink(temp_name, dir_fd=directory._fd)
                except FileNotFoundError:
                    pass
        except BaseException as item:
            cleanup_error = item
        if final_created:
            try:
                if os.name == "nt":
                    (directory.path / name).unlink(missing_ok=True)
                elif directory._fd is not None:
                    try:
                        os.unlink(name, dir_fd=directory._fd)
                    except FileNotFoundError:
                        pass
                directory.fsync()
            except BaseException as item:
                cleanup_error = cleanup_error or item
        if cleanup_error is not None:
            raise FinalizationError(
                "finalization publication failed and cleanup became ambiguous"
            ) from cleanup_error
        raise exc


def _read_strict_json(path: Path) -> Mapping[str, object]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FinalizationError(f"invalid finalization JSON file {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise FinalizationError(f"finalization JSON file {path.name} must contain an object")
    return value


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


def _derive_evidence_state(events: Sequence[StoredEvent], run_id: RunId) -> _EvidenceState:
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


def _artifact_file_digest(path: Path) -> tuple[int, str]:
    try:
        info = path.lstat()
    except OSError as exc:
        raise FinalizationError(f"cannot inspect retained artifact {path.name}: {exc}") from exc
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise FinalizationError("retained artifact must be a singly-linked regular file")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        raise FinalizationError(f"cannot hash retained artifact {path.name}: {exc}") from exc
    return int(info.st_size), digest.hexdigest()


def _verify_commitment(run_dir: Path, artifact: Mapping[str, object], digest: str) -> None:
    commitment = artifact.get("content_digest")
    if not isinstance(commitment, Mapping):
        raise FinalizationError("artifact content commitment is missing")
    method = commitment.get("method")
    value = commitment.get("value")
    if not isinstance(value, str):
        raise FinalizationError("artifact content commitment value is invalid")
    if method == "sha256":
        if not hmac.compare_digest(value, "sha256:" + digest):
            raise FinalizationError("retained artifact SHA-256 does not match canonical evidence")
        return
    if method == "hmac-sha256":
        if commitment.get("canonicalization_profile") != PROTECTED_ARTIFACT_COMMITMENT_PROFILE:
            raise FinalizationError("protected artifact commitment profile is unsupported")
        key_path = run_dir / _ARTIFACT_HMAC_KEY_FILENAME
        try:
            key = key_path.read_bytes()
        except OSError as exc:
            raise FinalizationError("protected artifact commitment key is unavailable") from exc
        if len(key) != 32:
            raise FinalizationError("protected artifact commitment key has invalid size")
        expected = "hmac:" + hmac.new(
            key, bytes.fromhex(digest), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(value, expected):
            raise FinalizationError("protected artifact HMAC does not match retained bytes")
        return
    raise FinalizationError(f"unsupported artifact commitment method: {method!r}")


def _verified_artifact_entries(run_dir: Path, artifacts: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for artifact in artifacts:
        relative = str(artifact["path"])
        path = run_dir.joinpath(*relative.split("/"))
        try:
            resolved_parent = path.parent.resolve(strict=True)
            artifact_root = (run_dir / "artifacts").resolve(strict=True)
        except OSError as exc:
            raise FinalizationError("artifact namespace cannot be resolved safely") from exc
        if resolved_parent != artifact_root and artifact_root not in resolved_parent.parents:
            raise FinalizationError("artifact path escapes the run artifact namespace")
        size, digest = _artifact_file_digest(path)
        if size != artifact.get("size_bytes"):
            raise FinalizationError("retained artifact size does not match canonical evidence")
        _verify_commitment(run_dir, artifact, digest)
        entries.append(
            {
                "artifact_id": artifact.get("artifact_id"),
                "kind": artifact.get("kind"),
                "path": relative,
                "size_bytes": size,
                "protection_state": artifact.get("protection_state"),
                "content_digest": artifact.get("content_digest"),
            }
        )
    return entries


def _candidate_completion(
    store: AtesEventStore,
    outcome: RunOutcomeRevision,
    finalized_at: datetime,
) -> StoredEvent:
    envelope = EventEnvelope(
        ates_version=ATES_VERSION,
        run_id=store.run_id,
        event_id=EventId.new(),
        sequence=store.next_sequence,
        event_type=EventType.RUN_COMPLETED,
        occurred_at=finalized_at,
    )
    return StoredEvent(
        envelope=envelope,
        payload={
            "finalization": to_json_compatible(outcome),
            "effective_status": outcome.effective_status.value,
            "status_policy_version": outcome.status_policy_version,
            "evidence_revision": outcome.evidence_revision,
        },
    )


def _evidence_bytes(events: Sequence[StoredEvent]) -> bytes:
    return b"".join(event.canonical_line() for event in events)


def _manifest_documents(
    *,
    store: AtesEventStore,
    existing_events: Sequence[StoredEvent],
    completion: StoredEvent,
    outcome: RunOutcomeRevision,
    artifact_entries: Sequence[Mapping[str, object]],
) -> tuple[dict[str, object], dict[str, object], bytes]:
    final_events = tuple(existing_events) + (completion,)
    evidence = _evidence_bytes(final_events)
    evidence_sha = hashlib.sha256(evidence).hexdigest()
    evidence_manifest = {
        "manifest_version": MANIFEST_VERSION,
        "ates_version": ATES_VERSION,
        "run_id": str(store.run_id),
        "finalization_id": str(outcome.finalization_id),
        "evidence_revision": outcome.evidence_revision,
        "status_policy_version": outcome.status_policy_version,
        "effective_status": outcome.effective_status.value,
        "evidence": {
            "path": "evidence.jsonl",
            "size_bytes": len(evidence),
            "sha256": "sha256:" + evidence_sha,
            "event_count": len(final_events),
            "final_sequence": completion.sequence,
            "final_event_id": str(completion.event_id),
            "final_event_type": EventType.RUN_COMPLETED.value,
        },
        "artifacts": list(artifact_entries),
    }
    manifest_bytes = _canonical_json_bytes(evidence_manifest)
    package_manifest = {
        "package_manifest_version": PACKAGE_MANIFEST_VERSION,
        "ates_version": ATES_VERSION,
        "run_id": str(store.run_id),
        "finalization_id": str(outcome.finalization_id),
        "evidence_revision": outcome.evidence_revision,
        "members": [
            {
                "path": "evidence.jsonl",
                "size_bytes": len(evidence),
                "sha256": "sha256:" + evidence_sha,
            },
            {
                "path": "manifests/manifest-0001.json",
                "size_bytes": len(manifest_bytes),
                "sha256": "sha256:" + hashlib.sha256(manifest_bytes).hexdigest(),
            },
        ],
        # Artifact commitments are already secret-safe ATES commitments. Do not
        # replace protected HMAC commitments with raw package SHA-256 values.
        "artifact_members": list(artifact_entries),
    }
    return evidence_manifest, package_manifest, evidence


def _binding_document(
    *,
    outcome: RunOutcomeRevision,
    completion: StoredEvent,
    evidence_manifest_bytes: bytes,
    package_manifest_bytes: bytes,
) -> dict[str, object]:
    return {
        "binding_version": FINALIZATION_BINDING_VERSION,
        "ates_version": ATES_VERSION,
        "run_id": str(outcome.run_id),
        "finalization": to_json_compatible(outcome),
        "completion_event": {
            "event_id": str(completion.event_id),
            "sequence": completion.sequence,
        },
        "manifests": {
            "evidence": {
                "path": "manifests/manifest-0001.json",
                "sha256": "sha256:" + hashlib.sha256(evidence_manifest_bytes).hexdigest(),
            },
            "package": {
                "path": "manifests/package-manifest-0001.json",
                "sha256": "sha256:" + hashlib.sha256(package_manifest_bytes).hexdigest(),
            },
        },
        "trust_state": FinalizationTrustState.BOUND_VERIFIED.value,
    }


def finalize_revision_one(store: AtesEventStore) -> FinalizationResult:
    """Finalize one open run as evidence revision/finalization revision 1.

    The caller must still own the live :class:`AtesEventStore`. This function is
    idempotent only through explicit verification/recovery APIs; it intentionally
    refuses to create a second revision-1 completion in a live stream.
    """
    if not isinstance(store, AtesEventStore):
        raise ValueError("finalization requires an AtesEventStore")
    existing_events = store.events
    state = _derive_evidence_state(existing_events, store.run_id)
    status = derive_run_status(state.status_inputs)
    finalized_at = datetime.now(timezone.utc)
    outcome = RunOutcomeRevision(
        finalization_id=FinalizationId.new(),
        run_id=store.run_id,
        revision=1,
        effective_status=status,
        evidence_revision=1,
        finalized_at=finalized_at,
        status_policy_version=STATUS_POLICY_VERSION,
    )
    artifacts = _verified_artifact_entries(store.run_dir, state.artifacts)
    completion = _candidate_completion(store, outcome, finalized_at)
    evidence_manifest, package_manifest, expected_evidence = _manifest_documents(
        store=store,
        existing_events=existing_events,
        completion=completion,
        outcome=outcome,
        artifact_entries=artifacts,
    )
    evidence_manifest_bytes = _canonical_json_bytes(evidence_manifest)
    package_manifest_bytes = _canonical_json_bytes(package_manifest)

    directories = store._directories
    if directories is None:
        raise FinalizationError("ATES run-directory authority is unavailable")
    directories.assert_authoritative()
    manifests = directories.run.ensure_child("manifests", "ATES manifests directory")
    try:
        manifest_path = _publish_no_overwrite(
            manifests, "manifest-0001.json", evidence_manifest_bytes
        )
        try:
            package_path = _publish_no_overwrite(
                manifests, "package-manifest-0001.json", package_manifest_bytes
            )
        except BaseException:
            try:
                manifest_path.unlink()
                manifests.fsync()
            except OSError:
                pass
            raise

        try:
            committed = store.append_event(completion)
        except BaseException:
            # Append durability can be ambiguous. Never delete manifests here:
            # the exact RUN_COMPLETED may already be durable and the run is now
            # recoverable by binding verification rather than by fabrication.
            raise
        if committed.canonical_line() != completion.canonical_line():
            raise FinalizationError("event store did not commit the exact completion candidate")

        try:
            persisted_evidence = store.path.read_bytes()
        except OSError as exc:
            raise FinalizationError("cannot verify final canonical evidence bytes") from exc
        if persisted_evidence != expected_evidence:
            raise FinalizationError(
                "persisted evidence does not match the manifest-bound completion candidate"
            )

        binding = _binding_document(
            outcome=outcome,
            completion=completion,
            evidence_manifest_bytes=evidence_manifest_bytes,
            package_manifest_bytes=package_manifest_bytes,
        )
        binding_bytes = _canonical_json_bytes(binding)
        binding_path = _publish_no_overwrite(
            directories.run, "run.json", binding_bytes
        )
        directories.assert_authoritative()
        return FinalizationResult(
            outcome=outcome,
            run_dir=store.run_dir,
            evidence_manifest_path=manifest_path,
            package_manifest_path=package_path,
            binding_path=binding_path,
            trust_state=FinalizationTrustState.BOUND_VERIFIED,
        )
    finally:
        manifests.close()


def verify_finalized_run(run_dir: Path) -> FinalizationResult:
    """Verify a revision-1 locally bound ATES package without mutating it."""
    root = Path(run_dir).resolve(strict=True)
    binding_path = root / "run.json"
    manifest_path = root / "manifests" / "manifest-0001.json"
    package_path = root / "manifests" / "package-manifest-0001.json"
    binding = _read_strict_json(binding_path)
    manifest = _read_strict_json(manifest_path)
    package = _read_strict_json(package_path)

    if binding.get("binding_version") != FINALIZATION_BINDING_VERSION:
        raise FinalizationError("unsupported run finalization binding version")
    finalization = binding.get("finalization")
    if not isinstance(finalization, Mapping):
        raise FinalizationError("run binding has no finalization object")
    try:
        run_id = RunId(finalization["run_id"])
        outcome = RunOutcomeRevision(
            finalization_id=FinalizationId(finalization["finalization_id"]),
            run_id=run_id,
            revision=int(finalization["revision"]),
            effective_status=RunStatus(finalization["effective_status"]),
            evidence_revision=int(finalization["evidence_revision"]),
            finalized_at=datetime.fromisoformat(str(finalization["finalized_at"])),
            status_policy_version=str(finalization["status_policy_version"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise FinalizationError("run binding finalization record is invalid") from exc
    if outcome.revision != 1 or outcome.evidence_revision != 1:
        raise FinalizationError("revision-1 verifier received a later finalization")

    expected_run_dir_name = str(run_id)
    if root.name != expected_run_dir_name:
        # Case-preserving encoded directory keys are possible in ATES storage;
        # do not infer identity from the lexical directory name. The manifest and
        # evidence run IDs are authoritative instead.
        pass

    binding_manifests = binding.get("manifests")
    if not isinstance(binding_manifests, Mapping):
        raise FinalizationError("run binding manifest references are missing")
    evidence_ref = binding_manifests.get("evidence")
    package_ref = binding_manifests.get("package")
    if not isinstance(evidence_ref, Mapping) or not isinstance(package_ref, Mapping):
        raise FinalizationError("run binding manifest references are malformed")
    manifest_bytes = manifest_path.read_bytes()
    package_bytes = package_path.read_bytes()
    if evidence_ref.get("sha256") != "sha256:" + hashlib.sha256(manifest_bytes).hexdigest():
        raise FinalizationError("evidence manifest does not match run binding")
    if package_ref.get("sha256") != "sha256:" + hashlib.sha256(package_bytes).hexdigest():
        raise FinalizationError("package manifest does not match run binding")

    if manifest.get("run_id") != str(run_id) or package.get("run_id") != str(run_id):
        raise FinalizationError("manifest run_id does not match run binding")
    if manifest.get("finalization_id") != str(outcome.finalization_id):
        raise FinalizationError("manifest finalization_id does not match run binding")
    evidence_meta = manifest.get("evidence")
    if not isinstance(evidence_meta, Mapping):
        raise FinalizationError("evidence manifest evidence entry is malformed")
    evidence_path = root / "evidence.jsonl"
    evidence_bytes = evidence_path.read_bytes()
    if evidence_meta.get("size_bytes") != len(evidence_bytes):
        raise FinalizationError("evidence size does not match manifest")
    if evidence_meta.get("sha256") != "sha256:" + hashlib.sha256(evidence_bytes).hexdigest():
        raise FinalizationError("evidence digest does not match manifest")

    lines = evidence_bytes.splitlines(keepends=True)
    if not lines or not lines[-1].endswith(b"\n"):
        raise FinalizationError("final canonical evidence is not newline terminated")
    try:
        documents = [json.loads(line.decode("utf-8")) for line in lines]
        events = tuple(StoredEvent.from_document(item) for item in documents)
    except (UnicodeDecodeError, json.JSONDecodeError, AtesStoreError, ValueError) as exc:
        raise FinalizationError("final canonical evidence cannot be parsed safely") from exc
    if any(event.run_id != run_id for event in events):
        raise FinalizationError("final evidence mixes run IDs")
    if tuple(event.sequence for event in events) != tuple(range(1, len(events) + 1)):
        raise FinalizationError("final evidence sequence is not gap-free")
    final_event = events[-1]
    completion_ref = binding.get("completion_event")
    if not isinstance(completion_ref, Mapping):
        raise FinalizationError("run binding completion event reference is malformed")
    if (
        final_event.envelope.event_type is not EventType.RUN_COMPLETED
        or str(final_event.event_id) != completion_ref.get("event_id")
        or final_event.sequence != completion_ref.get("sequence")
    ):
        raise FinalizationError("run binding does not identify the final RUN_COMPLETED event")
    payload_finalization = final_event.payload.get("finalization")
    if not isinstance(payload_finalization, Mapping):
        raise FinalizationError("RUN_COMPLETED finalization payload is malformed")
    if payload_finalization.get("finalization_id") != str(outcome.finalization_id):
        raise FinalizationError("RUN_COMPLETED finalization does not match run binding")

    return FinalizationResult(
        outcome=outcome,
        run_dir=root,
        evidence_manifest_path=manifest_path,
        package_manifest_path=package_path,
        binding_path=binding_path,
        trust_state=FinalizationTrustState.BOUND_VERIFIED,
    )


__all__ = [
    "EVIDENCE_DIGEST_PROFILE",
    "FINALIZATION_BINDING_VERSION",
    "MANIFEST_VERSION",
    "PACKAGE_MANIFEST_VERSION",
    "FinalizationError",
    "FinalizationResult",
    "FinalizationTrustState",
    "finalize_revision_one",
    "verify_finalized_run",
]
