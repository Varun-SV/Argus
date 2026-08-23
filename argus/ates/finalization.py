"""Hardened ATES revision-1 finalization, verification, and recovery."""
from __future__ import annotations

import hashlib, hmac, os
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional, Sequence

from . import finalization_legacy as _old
from .artifacts import (
    ARTIFACT_BYTES_PROFILE, PROTECTED_ARTIFACT_COMMITMENT_PROFILE,
    PROTECTED_ARTIFACT_VERIFICATION_REF, _AtesArtifactTree,
)
from .core import (
    ATES_VERSION, STATUS_POLICY_VERSION, EventEnvelope, EventType, EvidenceValue,
    FinalizationId, RunId, RunOutcomeRevision, RunStatus, StepAttemptRecord,
    StepAttemptStatus, to_json_compatible, validate_artifact_path,
    validate_step_attempt_history,
)
from .ids import EventId
from .status import derive_run_status
from .store import (
    AtesAppendError, AtesEventStore, AtesStoreError, StoredEvent,
    _PinnedDirectory, _validate_regular_file_descriptor, _windows_handle_info,
)

MANIFEST_VERSION = _old.MANIFEST_VERSION
PACKAGE_MANIFEST_VERSION = _old.PACKAGE_MANIFEST_VERSION
FINALIZATION_BINDING_VERSION = _old.FINALIZATION_BINDING_VERSION
EVIDENCE_DIGEST_PROFILE = _old.EVIDENCE_DIGEST_PROFILE
FinalizationError = _old.FinalizationError
FinalizationTrustState = _old.FinalizationTrustState
FinalizationResult = _old.FinalizationResult
_HMAC_KEY = ".ates-artifact-hmac-key"


def _json(value): return _old._canonical_json_bytes(value)
def _read(path): return _old._read_strict_json(path)
def _publish(directory, name, data): return _old._publish_no_overwrite(directory, name, data)


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


def _validate_attempts(events, run_id):
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
            if (rec.step_id != srec.step_id or rec.attempt != srec.attempt or rec.started_at != srec.started_at
                or rec.retry_reason != srec.retry_reason or sseq >= event.sequence):
                raise FinalizationError("step attempt completion does not match its start")
            closed[aid] = (rec, event.sequence); active = None
    if active is not None or set(opened) != set(closed): raise FinalizationError("canonical history contains an unfinished step attempt")
    retry_ids = {aid for aid, (rec, _) in opened.items() if rec.attempt > 1}
    if set(schedules) != retry_ids: raise FinalizationError("canonical history contains an orphan/missing retry schedule")
    records = tuple(rec for rec, _ in sorted(closed.values(), key=lambda x: x[1]))
    try: validate_step_attempt_history(records)
    except ValueError as exc: raise FinalizationError(f"step attempt history is invalid: {exc}") from exc


def _derive(events, run_id):
    _validate_attempts(events, run_id)
    return _old._derive_evidence_state(events, run_id)


def _pinned_bytes(directory, name, label):
    path = directory.path / name
    if os.name == "nt":
        try: kernel32, raw, _ = _windows_handle_info(path, directory=False, create=False)
        except (OSError, AtesStoreError) as exc: raise FinalizationError(f"{label} is unavailable") from exc
        keep = raw
        try:
            import msvcrt
            fd = msvcrt.open_osfhandle(raw, os.O_RDONLY | getattr(os, "O_BINARY", 0)); keep = None
            try:
                _validate_regular_file_descriptor(fd, path)
                with os.fdopen(fd, "rb", buffering=0) as handle: return handle.read()
            except BaseException:
                try: os.close(fd)
                except OSError: pass
                raise
        finally:
            if keep is not None: kernel32.CloseHandle(keep)
    if directory._fd is None: raise FinalizationError(f"pinned authority unavailable for {label}")
    try: fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0), dir_fd=directory._fd)
    except OSError as exc: raise FinalizationError(f"{label} is unavailable") from exc
    try:
        _validate_regular_file_descriptor(fd, path)
        with os.fdopen(fd, "rb", buffering=0, closefd=False) as handle: data = handle.read()
        directory.assert_file_identity(name, fd, label); return data
    except (OSError, AtesStoreError) as exc: raise FinalizationError(f"{label} cannot be verified safely") from exc
    finally: os.close(fd)


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


def _artifacts(store, records):
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


def finalize_revision_one(store):
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
    binding = _read(root / "run.json"); rid = binding.get("run_id")
    if not isinstance(rid, str) and isinstance(binding.get("finalization"), Mapping): rid = binding["finalization"].get("run_id")
    try: run_id = RunId(rid)
    except (TypeError, ValueError) as exc: raise FinalizationError("run binding has no valid run_id") from exc
    try: store = AtesEventStore(_project(root), run_id)
    except (OSError, AtesStoreError, ValueError) as exc: raise FinalizationError("cannot acquire authoritative run state for verification") from exc
    try:
        if store.run_dir.resolve(strict=True) != root: raise FinalizationError("run binding resolves to another run directory")
        return _verify_store(store, root)
    finally: store.close()


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


def recover_revision_one(project_dir, run_id):
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


def __getattr__(name):
    if name.startswith("_") and hasattr(_old, name): return getattr(_old, name)
    raise AttributeError(name)

__all__ = ["EVIDENCE_DIGEST_PROFILE", "FINALIZATION_BINDING_VERSION", "MANIFEST_VERSION", "PACKAGE_MANIFEST_VERSION", "FinalizationError", "FinalizationResult", "FinalizationTrustState", "finalize_revision_one", "recover_revision_one", "verify_finalized_run"]