"""Transactional ATES revision-1 finalization, verification, and recovery.

Evidence is validated before deriving status; exact manifests precede the
durable RUN_COMPLETED event, and run.json is the final commit marker. Recovery
never repairs evidence that already has a final binding.
"""
from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from .artifacts import (
    ARTIFACT_BYTES_PROFILE,
    ArtifactCaptureError,
    PROTECTED_ARTIFACT_COMMITMENT_PROFILE,
    PROTECTED_ARTIFACT_VERIFICATION_REF,
    _AtesArtifactTree,
)
from .core import (
    ATES_VERSION,
    EventEnvelope,
    EventType,
    RunOutcomeRevision,
    RunStatus,
    STATUS_POLICY_VERSION,
    to_json_compatible,
    validate_artifact_path,
)
from .evidence_validation import (
    _artifact_record,
    _time,
    derive_evidence_state as _derive,
)
from .finalization_io import (
    _assert_directory_identity,
    _entry_exists,
    _json,
    _pinned_bytes,
    _preflight_bound_members,
    _publish,
    _read,
    _strict_json_object,
)
from .finalization_types import (
    EVIDENCE_DIGEST_PROFILE,
    FINALIZATION_BINDING_VERSION,
    FinalizationError,
    FinalizationResult,
    FinalizationTrustState,
    MANIFEST_VERSION,
    PACKAGE_MANIFEST_VERSION,
    _finalization_error,
)
from .ids import EventId, FinalizationId, RunId
from .status import derive_run_status
from .store import (
    AtesAppendError,
    AtesEventStore,
    AtesStoreError,
    StoredEvent,
    _PinnedDirectory,
    _run_directory_key,
)

_HMAC_KEY = ".ates-artifact-hmac-key"



def _verify_commitment(store, artifact, digest):
    c = artifact.get("content_digest")
    if not isinstance(c, Mapping) or not isinstance(c.get("value"), str): raise FinalizationError("artifact content commitment is invalid")
    if c.get("method") == "sha256":
        if c.get("canonicalization_profile") != ARTIFACT_BYTES_PROFILE or not hmac.compare_digest(c["value"], "sha256:" + digest):
            raise FinalizationError("retained artifact SHA-256 does not match canonical evidence")
        return
    if c.get("method") == "hmac-sha256":
        if c.get("canonicalization_profile") != PROTECTED_ARTIFACT_COMMITMENT_PROFILE or c.get("verification_ref") != PROTECTED_ARTIFACT_VERIFICATION_REF:
            raise FinalizationError("protected artifact commitment profile is unsupported")
        dirs = store._directories
        if dirs is None: raise FinalizationError("run authority unavailable for artifact verification")
        key = _pinned_bytes(dirs.run, _HMAC_KEY, "protected artifact commitment key")
        if len(key) != 32: raise FinalizationError("protected artifact commitment key has invalid size")
        expected = "hmac:" + hmac.new(key, bytes.fromhex(digest), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(c["value"], expected): raise FinalizationError("protected artifact HMAC does not match retained bytes")
        return
    raise FinalizationError("unsupported artifact commitment method")


def _verified_artifact_bytes(store, records):
    if not records: return []
    rels = []
    for a in records:
        try: path = validate_artifact_path(a.get("path"))
        except (TypeError, ValueError) as exc: raise FinalizationError("artifact path is invalid") from exc
        rels.append(path[len("artifacts/"):])
    tree = _AtesArtifactTree(store, tuple(rels)); error = None
    try:
        out = []
        for a, rel in zip(records, rels):
            try: size, digest = tree.digest_existing(rel)
            except (OSError, AtesStoreError, ValueError) as exc: raise FinalizationError("retained artifact cannot be verified safely") from exc
            if isinstance(a.get("size_bytes"), bool) or not isinstance(a.get("size_bytes"), int) or size != a.get("size_bytes"):
                raise FinalizationError("retained artifact size does not match canonical evidence")
            _verify_commitment(store, a, digest)
            out.append({k: a.get(k) for k in ("artifact_id", "kind", "path", "size_bytes", "protection_state", "content_digest")})
        return out
    except BaseException as exc: error = exc; raise
    finally:
        try: tree.close(suppress_errors=error is not None)
        except (OSError, AtesStoreError) as exc: raise FinalizationError("artifact namespace authority changed during verification") from exc


def _artifacts(store, records):
    """Return canonical JSON after re-verifying retained artifact bytes."""
    normalized = tuple(_artifact_record(record) for record in tuple(records))
    try:
        raw = _verified_artifact_bytes(store, records)
    except ArtifactCaptureError as exc:
        raise FinalizationError(
            "retained artifact cannot be verified safely"
        ) from exc
    try:
        converted = to_json_compatible(raw)
    except ValueError as exc:
        raise FinalizationError(
            "artifact manifest projection is malformed"
        ) from exc
    if not isinstance(converted, list):
        raise FinalizationError("artifact manifest projection is malformed")
    return [to_json_compatible(record) for record in normalized]


def _outdoc(outcome):
    value = to_json_compatible(outcome)
    if not isinstance(value, dict): raise FinalizationError("finalization outcome serialization failed")
    return value


def _outcome(value):
    if not isinstance(value, Mapping): raise FinalizationError("finalization record is missing/malformed")
    try:
        corr = value.get("correction_ids", ())
        if isinstance(corr, (str, bytes, bytearray, Mapping)): raise ValueError
        return RunOutcomeRevision(
            finalization_id=FinalizationId(value["finalization_id"]), run_id=RunId(value["run_id"]),
            revision=int(value["revision"]), effective_status=RunStatus(value["effective_status"]),
            evidence_revision=int(value["evidence_revision"]), finalized_at=_time(value["finalized_at"], "finalized_at"),
            status_policy_version=str(value["status_policy_version"]),
            supersedes_finalization_id=None if value.get("supersedes_finalization_id") is None else FinalizationId(value.get("supersedes_finalization_id")),
            correction_ids=tuple(corr),
        )
    except (KeyError, TypeError, ValueError) as exc: raise FinalizationError("finalization record is invalid") from exc


def _roots(o):
    return {"run_id": str(o.run_id), "finalization_id": str(o.finalization_id), "revision": o.revision,
            "evidence_revision": o.evidence_revision, "effective_status": o.effective_status.value,
            "status_policy_version": o.status_policy_version, "finalized_at": o.finalized_at.isoformat()}


def _assert_out(document, outcome, label):
    if _outdoc(_outcome(document.get("finalization"))) != _outdoc(outcome): raise FinalizationError(f"{label} finalization does not match bound outcome")
    for k, v in _roots(outcome).items():
        if document.get(k) != v: raise FinalizationError(f"{label} root {k} does not match bound outcome")


def _payload(o): return {"finalization": _outdoc(o), **_roots(o)}


def _completion(rid, seq, o, eid=None): return StoredEvent(EventEnvelope(ATES_VERSION, rid, eid or EventId.new(), seq, EventType.RUN_COMPLETED, o.finalized_at), _payload(o))


def _documents(events, completion, outcome, artifacts):
    final = tuple(events) + (completion,); evidence = b"".join(e.canonical_line() for e in final); digest = hashlib.sha256(evidence).hexdigest()
    manifest = {"manifest_version": MANIFEST_VERSION, "ates_version": ATES_VERSION, "finalization": _outdoc(outcome), **_roots(outcome),
                "evidence": {"path": "evidence.jsonl", "size_bytes": len(evidence), "sha256": "sha256:" + digest,
                             "event_count": len(final), "final_sequence": completion.sequence, "final_event_id": str(completion.event_id), "final_event_type": EventType.RUN_COMPLETED.value},
                "artifacts": list(artifacts)}
    mb = _json(manifest)
    package = {"package_manifest_version": PACKAGE_MANIFEST_VERSION, "ates_version": ATES_VERSION, "finalization": _outdoc(outcome), **_roots(outcome),
               "members": [{"path": "evidence.jsonl", "size_bytes": len(evidence), "sha256": "sha256:" + digest},
                           {"path": "manifests/manifest-0001.json", "size_bytes": len(mb), "sha256": "sha256:" + hashlib.sha256(mb).hexdigest()}],
               "artifact_members": list(artifacts)}
    return manifest, package, evidence


def _binding(outcome, completion, mb, pb):
    return {"binding_version": FINALIZATION_BINDING_VERSION, "ates_version": ATES_VERSION, "finalization": _outdoc(outcome), **_roots(outcome),
            "completion_event": {"event_id": str(completion.event_id), "sequence": completion.sequence},
            "manifests": {"evidence": {"path": "manifests/manifest-0001.json", "sha256": "sha256:" + hashlib.sha256(mb).hexdigest()},
                          "package": {"path": "manifests/package-manifest-0001.json", "sha256": "sha256:" + hashlib.sha256(pb).hexdigest()}},
            "trust_state": FinalizationTrustState.BOUND_VERIFIED.value}


def _same(actual, expected, label):
    if _json(actual) != _json(expected): raise FinalizationError(f"{label} does not match canonical finalization candidate")


def _commit_revision_one(store):
    if not isinstance(store, AtesEventStore): raise ValueError("finalization requires an AtesEventStore")
    events = store.events; state = _derive(events, store.run_id)
    o = RunOutcomeRevision(FinalizationId.new(), store.run_id, 1, derive_run_status(state.status_inputs), 1, datetime.now(timezone.utc), STATUS_POLICY_VERSION)
    arts = _artifacts(store, state.artifacts); completion = _completion(store.run_id, store.next_sequence, o)
    manifest, package, expected = _documents(events, completion, o, arts); mb, pb = _json(manifest), _json(package)
    dirs = store._directories
    if dirs is None: raise FinalizationError("ATES run-directory authority is unavailable")
    dirs.assert_authoritative(); manifests = dirs.run.ensure_child("manifests", "ATES manifests directory")
    try:
        mp = _publish(manifests, "manifest-0001.json", mb)
        pp = _publish(manifests, "package-manifest-0001.json", pb)
        committed = store.append_event(completion)
        if committed.canonical_line() != completion.canonical_line(): raise FinalizationError("event store committed a different completion")
        if store._read_all() != expected: raise FinalizationError("persisted evidence differs from manifest-bound candidate")
        bp = _publish(dirs.run, "run.json", _json(_binding(o, completion, mb, pb))); dirs.assert_authoritative()
        return FinalizationResult(o, store.run_dir, mp, pp, bp, FinalizationTrustState.BOUND_VERIFIED)
    finally: manifests.close()


def finalize_revision_one(store):
    result = _commit_revision_one(store)
    _preflight_bound_members(result.run_dir)
    verified = _verify_store(store, result.run_dir)
    _preflight_bound_members(result.run_dir)
    return verified


def _project(root):
    if root.parent.name != "runs" or root.parent.parent.name != ".argus": raise FinalizationError("run directory is not beneath .argus/runs")
    return root.parent.parent.parent


def _verify_store(store, root):
    bp, mp, pp = root / "run.json", root / "manifests/manifest-0001.json", root / "manifests/package-manifest-0001.json"
    binding, manifest, package = _read(bp), _read(mp), _read(pp)
    if binding.get("binding_version") != FINALIZATION_BINDING_VERSION or manifest.get("manifest_version") != MANIFEST_VERSION or package.get("package_manifest_version") != PACKAGE_MANIFEST_VERSION:
        raise FinalizationError("unsupported finalization/manifest version")
    events = store.events
    if not events or events[-1].envelope.event_type is not EventType.RUN_COMPLETED: raise FinalizationError("final canonical event is not RUN_COMPLETED")
    final = events[-1]; o = _outcome(final.payload.get("finalization"))
    if final.envelope.occurred_at != o.finalized_at or to_json_compatible(final.payload) != _payload(o): raise FinalizationError("RUN_COMPLETED does not match normalized outcome")
    if o.run_id != store.run_id or o.revision != 1 or o.evidence_revision != 1 or o.status_policy_version != STATUS_POLICY_VERSION: raise FinalizationError("RUN_COMPLETED is not supported revision 1")
    _assert_out(binding, o, "run binding"); _assert_out(manifest, o, "evidence manifest"); _assert_out(package, o, "package manifest")
    cref, emeta = binding.get("completion_event"), manifest.get("evidence")
    if not isinstance(cref, Mapping) or not isinstance(emeta, Mapping) or cref.get("event_id") != str(final.event_id) or cref.get("sequence") != final.sequence or emeta.get("final_event_id") != str(final.event_id) or emeta.get("final_sequence") != final.sequence or emeta.get("event_count") != len(events):
        raise FinalizationError("binding/manifest completion identity is inconsistent")
    state = _derive(events[:-1], store.run_id)
    if derive_run_status(state.status_inputs) is not o.effective_status: raise FinalizationError("bound status does not match canonical derivation")
    arts = _artifacts(store, state.artifacts); xm, xp, xe = _documents(events[:-1], final, o, arts)
    actual = store._read_all()
    if actual != xe: raise FinalizationError("canonical evidence differs from regenerated finalization")
    _same(manifest, xm, "evidence manifest"); _same(package, xp, "package manifest")
    mb, pb = _json(xm), _json(xp); _same(binding, _binding(o, final, mb, pb), "run binding")
    return FinalizationResult(o, root, mp, pp, bp, FinalizationTrustState.BOUND_VERIFIED)


def verify_finalized_run(run_dir):
    try: root = Path(run_dir).resolve(strict=True)
    except OSError as exc: raise FinalizationError(f"cannot resolve finalized run directory: {exc}") from exc
    _preflight_bound_members(root)
    binding = _read(root / "run.json"); rid = binding.get("run_id")
    if not isinstance(rid, str) and isinstance(binding.get("finalization"), Mapping): rid = binding["finalization"].get("run_id")
    try: run_id = RunId(rid)
    except (TypeError, ValueError) as exc: raise FinalizationError("run binding has no valid run_id") from exc
    project = _project(root)
    expected_root = project / ".argus" / "runs" / _run_directory_key(run_id)
    if root != expected_root:
        raise FinalizationError("run binding resolves to another run directory")
    try: store = AtesEventStore(project, run_id)
    except (OSError, AtesStoreError, ValueError) as exc: raise FinalizationError("cannot acquire authoritative run state for verification") from exc
    try:
        result = _verify_store(store, root)
    finally: store.close()
    _preflight_bound_members(root)
    return result


def _candidate_from_manifest(manifest, rid):
    if manifest.get("manifest_version") != MANIFEST_VERSION: raise FinalizationError("unsupported evidence manifest version")
    o = _outcome(manifest.get("finalization")); _assert_out(manifest, o, "evidence manifest")
    if o.run_id != rid or o.revision != 1 or o.evidence_revision != 1: raise FinalizationError("recovery manifest is not revision 1 for this run")
    meta = manifest.get("evidence")
    if not isinstance(meta, Mapping) or meta.get("final_event_type") != EventType.RUN_COMPLETED.value or not isinstance(meta.get("final_event_id"), str) or isinstance(meta.get("final_sequence"), bool) or not isinstance(meta.get("final_sequence"), int):
        raise FinalizationError("recovery manifest completion identity is invalid")
    try: eid = EventId(meta["final_event_id"])
    except ValueError as exc: raise FinalizationError("recovery completion event_id is invalid") from exc
    return o, _completion(rid, meta["final_sequence"], o, eid)


def _reopen(project, rid, completion):
    store = AtesEventStore(project, rid, repair_trailing_partial=True)
    finals = [e for e in store.events if e.envelope.event_type is EventType.RUN_COMPLETED]
    if finals:
        if len(finals) != 1 or store.events[-1].canonical_line() != completion.canonical_line(): store.close(); raise FinalizationError("ambiguous completion reconciled differently")
        return store
    try: store.append_event(completion)
    except BaseException: store.close(); raise
    return store


class _BoundRunDetected(RuntimeError):
    """Internal control flow: a final binding exists and must be verified strictly."""

    def __init__(self, run_dir: Path) -> None:
        super().__init__(f"finalized run binding already exists: {run_dir}")
        self.run_dir = run_dir


def _normalize_project_and_run_id(project_dir, run_id) -> tuple[Path, RunId]:
    try:
        rid = run_id if isinstance(run_id, RunId) else RunId(run_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("run_id must be a valid RunId") from exc
    try:
        project = Path(project_dir).resolve(strict=True)
    except OSError as exc:
        _finalization_error(
            "recovery project directory is unavailable", exc
        )
    return project, rid


def _bound_run_root(project_dir, run_id) -> Path | None:
    """Detect a final binding without opening or repairing canonical evidence.

    Recovery's trailing-partial repair is valid only before ``run.json`` exists.
    Probe that marker through a pinned/no-follow run directory first so a bound
    package is always routed to strict verification with its evidence untouched.
    Any filesystem object at the binding name counts as bound here; malformed,
    linked, or otherwise unsafe bindings are for the strict verifier to reject.
    """
    project, rid = _normalize_project_and_run_id(project_dir, run_id)
    root = project / ".argus" / "runs" / _run_directory_key(rid)
    if not root.exists():
        return None

    run_pin = None
    try:
        run_pin = _PinnedDirectory(root)
        _assert_directory_identity(run_pin, "ATES run directory")
        if not _entry_exists(run_pin, "run.json"):
            return None
        _assert_directory_identity(run_pin, "ATES run directory")
        return root
    except FinalizationError:
        raise
    except (OSError, AtesStoreError, ValueError) as exc:
        _finalization_error(
            "bound recovery state cannot be inspected safely", exc
        )
    finally:
        if run_pin is not None:
            try:
                run_pin.close()
            except BaseException:
                pass


def _preflight_recovery_members(project_dir, run_id) -> None:
    """Verify crash-state members before recovery publishes new state.

    AtesEventStore owns both the canonical RunId→directory encoding and the
    narrow repair contract for an unterminated trailing JSONL record. Reuse
    those authorities instead of reconstructing the run pathname or opening
    evidence with stricter semantics than the recovery path itself.

    Crucially, the final binding is detected *before* opening the store with
    repair semantics. Once ``run.json`` exists, recovery must be read/verify
    only: post-finalization corruption is never healed by the incomplete-run
    tail-repair contract.
    """
    project, rid = _normalize_project_and_run_id(project_dir, run_id)

    # Do not create a new run merely to preflight recovery. The existence probe
    # must use the same encoding as _RunDirectoryChain so supported uppercase
    # and underscore RunIds cannot skip exact-byte validation.
    root = project / ".argus" / "runs" / _run_directory_key(rid)
    if not root.exists():
        return

    # Re-check immediately before the repair-capable store open. The public
    # recovery wrapper performs the same check on entry; this second pinned
    # check closes the sibling path where a binding appears between wrapper
    # dispatch and crash-state preflight.
    bound_root = _bound_run_root(project, rid)
    if bound_root is not None:
        raise _BoundRunDetected(bound_root)

    store = None
    manifests = None
    try:
        # This may trim only an unterminated trailing record for an *unbound*
        # run. That tail is not a canonical event; no new manifest, completion,
        # or binding is published until the persisted candidate below has been
        # proven byte-for-byte.
        store = AtesEventStore(
            project,
            rid,
            repair_trailing_partial=True,
        )
        directories = store._directories
        if directories is None:
            _finalization_error(
                "run authority unavailable during recovery preflight"
            )
        directories.assert_authoritative()
        run_pin = directories.run
        root = store.run_dir

        # A cooperating Argus writer cannot publish this binding while the
        # store authority above is held. If a binding nevertheless appears,
        # fail closed instead of continuing an incomplete-run recovery path.
        if _entry_exists(run_pin, "run.json"):
            _finalization_error(

                "run became bound while incomplete-run recovery authority was held",
            )
        if not _entry_exists(run_pin, "manifests"):
            return

        try:
            manifests = _PinnedDirectory(root / "manifests")
        except AtesStoreError as exc:
            _finalization_error(
                "ATES manifests recovery namespace is unsafe", exc
            )
        run_pin.assert_child_identity(
            "manifests", manifests, "ATES manifests directory"
        )
        if not _entry_exists(manifests, "manifest-0001.json"):
            return

        manifest_raw = _pinned_bytes(
            manifests,
            "manifest-0001.json",
            "recovery evidence manifest",
        )
        manifest = _strict_json_object(
            manifest_raw,
            "recovery evidence manifest",

        )

        outcome, completion = _candidate_from_manifest(manifest, rid)
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
                    "existing completion differs from recovery candidate"
                )
            pre = store.events[:-1]
        else:
            pre = store.events

        if completion.sequence != len(pre) + 1:
            _finalization_error(
                "recovery completion sequence is inconsistent"
            )

        state = _derive(pre, rid)
        if (
            outcome.status_policy_version != STATUS_POLICY_VERSION
            or derive_run_status(state.status_inputs)
            is not outcome.effective_status
        ):
            _finalization_error(
                "recovery outcome differs from canonical derivation"
            )

        artifacts = _artifacts(store, state.artifacts)
        expected_manifest, expected_package, expected_evidence = _documents(
            pre,
            completion,
            outcome,
            artifacts,
        )
        if manifest_raw != _json(expected_manifest):
            _finalization_error(

                "recovery evidence manifest bytes differ from regenerated candidate",
            )

        if _entry_exists(
            manifests,
            "package-manifest-0001.json",

        ):
            package_raw = _pinned_bytes(
                manifests,
                "package-manifest-0001.json",
                "recovery package manifest",
            )
            _strict_json_object(
                package_raw,
                "recovery package manifest",

            )
            if package_raw != _json(expected_package):
                _finalization_error(

                    "recovery package manifest bytes differ from regenerated candidate",
                )

        if finals and store._read_all() != expected_evidence:
            _finalization_error(
                "recovered evidence differs from manifest-bound candidate"
            )

        directories.assert_authoritative()
        run_pin.assert_child_identity(
            "manifests", manifests, "ATES manifests directory"
        )
        _assert_directory_identity(
            run_pin,
            "ATES run directory",

        )
        _assert_directory_identity(
            manifests,
            "ATES manifests directory",

        )
    except FinalizationError:
        raise
    except (OSError, AtesStoreError, ValueError) as exc:
        _finalization_error(
            "recovery members cannot be preflighted safely", exc
        )
    finally:
        if manifests is not None:
            try:
                manifests.close()
            except BaseException:
                pass
        if store is not None:
            try:
                store.close()
            except BaseException:
                pass


def _recover_unbound_revision(project_dir, run_id):
    try: rid = run_id if isinstance(run_id, RunId) else RunId(run_id)
    except (TypeError, ValueError) as exc: raise ValueError("run_id must be a valid RunId") from exc
    project = Path(project_dir).resolve(strict=True); store = AtesEventStore(project, rid, repair_trailing_partial=True); root = store.run_dir
    try:
        bp, mp, pp = root / "run.json", root / "manifests/manifest-0001.json", root / "manifests/package-manifest-0001.json"
        if bp.exists(): store.close(); return verify_finalized_run(root)
        if not mp.exists():
            if pp.exists() or any(e.envelope.event_type is EventType.RUN_COMPLETED for e in store.events): raise FinalizationError("recovery state is missing evidence manifest")
            result = finalize_revision_one(store); store.close(); return verify_finalized_run(result.run_dir)
        manifest = _read(mp); o, completion = _candidate_from_manifest(manifest, rid)
        finals = [e for e in store.events if e.envelope.event_type is EventType.RUN_COMPLETED]
        if finals:
            if len(finals) != 1 or store.events[-1].canonical_line() != completion.canonical_line(): raise FinalizationError("existing completion differs from recovery candidate")
            pre = store.events[:-1]
        else: pre = store.events
        if completion.sequence != len(pre) + 1: raise FinalizationError("recovery completion sequence is inconsistent")
        state = _derive(pre, rid)
        if o.status_policy_version != STATUS_POLICY_VERSION or derive_run_status(state.status_inputs) is not o.effective_status: raise FinalizationError("recovery outcome differs from canonical derivation")
        arts = _artifacts(store, state.artifacts); xm, xp, xe = _documents(pre, completion, o, arts); _same(manifest, xm, "recovery evidence manifest")
        mb, pb = _json(xm), _json(xp); dirs = store._directories
        if dirs is None: raise FinalizationError("run authority unavailable during recovery")
        manifests = dirs.run.ensure_child("manifests", "ATES manifests directory")
        try:
            if pp.exists(): _same(_read(pp), xp, "recovery package manifest")
            else: _publish(manifests, "package-manifest-0001.json", pb)
        finally: manifests.close()
        if not finals:
            try: store.append_event(completion)
            except AtesAppendError: store.close(); store = _reopen(project, rid, completion); root = store.run_dir
        if store._read_all() != xe: raise FinalizationError("recovered evidence differs from manifest-bound candidate")
        dirs = store._directories
        if dirs is None: raise FinalizationError("run authority unavailable for recovered binding")
        _publish(dirs.run, "run.json", _json(_binding(o, completion, mb, pb)))
    finally:
        try: store.close()
        except Exception: pass
    return verify_finalized_run(root)


def recover_revision_one(project_dir, run_id):
    """Reconcile an existing run, then materialize its detached report package."""
    project, rid = _normalize_project_and_run_id(project_dir, run_id)
    root = project / ".argus" / "runs" / _run_directory_key(rid)
    if not root.exists():
        raise FinalizationError("cannot recover an absent ATES run")
    bound_root = _bound_run_root(project, rid)
    if bound_root is not None:
        result = verify_finalized_run(bound_root)
    else:
        try:
            _preflight_recovery_members(project, rid)
            result = _recover_unbound_revision(project, rid)
        except _BoundRunDetected as detected:
            result = verify_finalized_run(detected.run_dir)
    # Import here to keep the canonical layer independent of derived reports
    # during package initialization. The event-store writer is already closed.
    from .package import complete_run_package
    complete_run_package(result)
    return result


__all__ = [
    "EVIDENCE_DIGEST_PROFILE", "FINALIZATION_BINDING_VERSION", "MANIFEST_VERSION",
    "PACKAGE_MANIFEST_VERSION", "FinalizationError", "FinalizationResult",
    "FinalizationTrustState", "finalize_revision_one", "recover_revision_one",
    "verify_finalized_run",
]
