"""Portable, context-safe ATES v0.1 report implementation."""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import time
import uuid
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence

from .audit import ApprovalError, KeyResolver, validate_approvals, validate_audit_chain
from .core import EventType, RunStatus, VerificationStatus, to_json_compatible
from .finalization import (
    FinalizationError,
    FinalizationTrustState,
    verify_finalized_run,
)
from .store import (
    AtesEventStore,
    AtesStoreBusy,
    AtesStoreError,
    _PinnedDirectory,
    _WriterLock,
    _open_regular_file,
)

REPORT_VERSION = "ates-report-v1"
REPORT_RENDERER_ID = "argus-ates-stdlib-renderer-v1"
REPORT_MANIFEST_VERSION = "ates-report-manifest-v1"
_REPORT_NAMES = ("report.json", "report.md", "report.html", "junit.xml")



_LOCK_WAIT_SECONDS = 5.0
_LOCK_RETRY_SECONDS = 0.01



class ReportError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReportBundle:
    run_dir: Path
    report_dir: Path
    json_path: Path
    markdown_path: Path
    html_path: Path
    junit_path: Path
    manifest_path: Path
    trust_state: FinalizationTrustState


@dataclass(frozen=True)
class ReportVerificationResult:
    trust_state: FinalizationTrustState
    report_dir: Path
    manifest_path: Optional[Path]
    error: Optional[str] = None


@dataclass(frozen=True)
class FinalizationTrustInspection:
    trust_state: FinalizationTrustState
    run_dir: Path
    error: Optional[str] = None


def _json(value: object, *, pretty: bool = False) -> bytes:
    try:
        if pretty:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        else:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ReportError(f"report model is not JSON serializable: {exc}") from exc
    return text.encode("utf-8") + b"\n"


def _root(run_dir: Path | str) -> Path:
    try:
        root = Path(run_dir).resolve(strict=True)
    except OSError as exc:
        raise ReportError(f"cannot resolve ATES run directory: {exc}") from exc
    if root.parent.name != "runs" or root.parent.parent.name != ".argus":
        raise ReportError("report source is not beneath .argus/runs")
    return root


def _pinned_bytes(directory: Path, name: str, label: str) -> bytes:
    from . import finalization
    pin = None
    try:
        pin = _PinnedDirectory(directory)
        return finalization._pinned_bytes(pin, name, label)
    except (OSError, AtesStoreError, finalization.FinalizationError) as exc:
        raise ReportError(f"{label} cannot be read safely") from exc
    finally:
        if pin is not None:
            try: pin.close()
            except BaseException: pass


def _strict_object(raw: bytes, label: str) -> dict[str, object]:
    def pairs(items):
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate key {key}")
            result[key] = value
        return result
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                           parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ReportError(f"{label} is not strict JSON") from exc
    if not isinstance(value, dict) or _json(value) != raw:
        raise ReportError(f"{label} is not canonical JSON")
    return value


def inspect_finalization_trust(run_dir: Path | str) -> FinalizationTrustInspection:
    root = _root(run_dir)
    if not (root / "run.json").exists():
        state = FinalizationTrustState.UNVERIFIED_DERIVED if (root / "evidence.jsonl").exists() else FinalizationTrustState.INVALID
        return FinalizationTrustInspection(state, root, "final binding is absent")
    try:
        verify_finalized_run(root)
    except (FinalizationError, OSError, ValueError) as exc:
        return FinalizationTrustInspection(FinalizationTrustState.INVALID, root, str(exc))
    return FinalizationTrustInspection(FinalizationTrustState.BOUND_VERIFIED, root)


def _verified_events(root: Path):
    result = verify_finalized_run(root)
    store = None
    try:
        store = AtesEventStore(root.parent.parent.parent, result.outcome.run_id)
        if store.run_dir.resolve(strict=True) != root:
            raise ReportError("run binding resolves to another run directory")
        return result, tuple(store.events)
    except (OSError, AtesStoreError, ValueError) as exc:
        raise ReportError("cannot open verified canonical evidence") from exc
    finally:
        if store is not None:
            try: store.close()
            except BaseException: pass


def _payload(event, key: str) -> Optional[dict[str, object]]:
    value = event.payload.get(key)
    if not isinstance(value, Mapping):
        return None
    converted = to_json_compatible(value)
    return converted if isinstance(converted, dict) else None


def _requirement_digest(value: Mapping[str, object]) -> str:
    return "sha256:" + hashlib.sha256(_json(to_json_compatible(value)).rstrip(b"\n")).hexdigest()


def _member_snapshot(root: Path, name: str) -> dict[str, object]:
    path = root / name
    # Missing detached ledgers are a valid read-only state immediately after
    # canonical finalization.  Distinguish true absence from an unsafe existing
    # filesystem object (including dangling symlinks) without creating anything.
    if not os.path.lexists(path):
        return {
            "path": name,
            "state": "absent",
            "size_bytes": 0,
            "sha256": None,
        }

    raw = _pinned_bytes(root, name, f"detached ledger snapshot {name}")
    return {
        "path": name,
        "state": "present",
        "size_bytes": len(raw),
        "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
    }


def _detached_snapshot(root: Path) -> dict[str, object]:
    return {
        "snapshot_version": "ates-detached-ledger-snapshot-v1",
        "members": [
            _member_snapshot(root, "approvals.jsonl"),
            _member_snapshot(root, "audit.jsonl"),
        ],
        "freshness_note": (
            "These entries identify the detached-ledger state used to render this report. "
            "A member may be explicitly absent; present-member digests do not prove the files "
            "are still current. Call verify_report_bundle()."
        ),
    }


def _model(root: Path, approval_key_resolver: Optional[KeyResolver]) -> dict[str, object]:
    result, events = _verified_events(root)
    start = next((event for event in events if event.envelope.event_type is EventType.RUN_STARTED), None)
    if start is None:
        raise ReportError("verified evidence has no RUN_STARTED")
    run = _payload(start, "run") or {}
    raw_steps = start.payload.get("steps", ())
    steps = [to_json_compatible(item) for item in tuple(raw_steps)] if isinstance(raw_steps, Sequence) and not isinstance(raw_steps, (str, bytes, bytearray, Mapping)) else []

    attempts: list[dict[str, object]] = []
    assertions: list[dict[str, object]] = []
    observations: list[dict[str, object]] = []
    findings: list[dict[str, object]] = []
    artifacts: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []
    timeline: list[dict[str, object]] = []

    for event in events:
        kind = event.envelope.event_type
        timeline.append({"sequence": event.sequence, "event_id": str(event.event_id), "event_type": kind.value, "occurred_at": event.envelope.occurred_at.isoformat()})
        if kind is EventType.STEP_ATTEMPT_COMPLETED:
            record = _payload(event, "attempt")
            if record is not None:
                attempts.append(record)
                if record.get("status") != "passed": failures.append({"type": "step_attempt", "sequence": event.sequence, "record": record})
        elif kind is EventType.ASSERTION_EVALUATED:
            record = _payload(event, "assertion")
            if record is not None:
                assertions.append(record)
                if record.get("result") not in {"passed", "skipped"}: failures.append({"type": "assertion", "sequence": event.sequence, "record": record})
        elif kind is EventType.OBSERVATION_CAPTURED:
            record = _payload(event, "observation")
            if record is not None: observations.append(record)
        elif kind is EventType.FINDING_RECORDED:
            record = _payload(event, "finding")
            if record is not None: findings.append(record)
        elif kind in {EventType.CHECKPOINT_CAPTURED, EventType.ARTIFACT_COLLECTED}:
            record = _payload(event, "artifact")
            if record is not None:
                attempt_id = event.payload.get("step_attempt_id")
                item = {"record": record, "sequence": event.sequence, "step_attempt_id": attempt_id}
                if kind is EventType.CHECKPOINT_CAPTURED:
                    item["context"] = event.payload.get("context")
                    item["finding_id"] = event.payload.get("finding_id")
                artifacts.append(item)
        elif kind is EventType.ARTIFACT_SUPPRESSED:
            artifacts.append({"suppressed": to_json_compatible(event.payload), "sequence": event.sequence})
        elif kind is EventType.ACTION_OUTCOME_UNKNOWN:
            failures.append({"type": "action_outcome_unknown", "sequence": event.sequence, "action_id": event.payload.get("action_id")})
        elif kind is EventType.RUN_MARKED_INCOMPLETE and event.payload.get("reason") != "runtime.finalization_pending":
            failures.append({"type": "run_incomplete", "sequence": event.sequence, "reason": event.payload.get("reason")})

    traceability: list[dict[str, object]] = []
    for assertion in assertions:
        requirement = assertion.get("requirement")
        if not isinstance(requirement, Mapping):
            continue
        attempt_id = assertion.get("step_attempt_id")
        source = run.get("source")
        traceability.append({
            "requirement_identity": to_json_compatible(requirement),
            "requirement_identity_digest": _requirement_digest(requirement),
            "test_case_id": source.get("test_case_id") if isinstance(source, Mapping) else None,
            "run_id": str(result.outcome.run_id), "step_id": assertion.get("step_id"),
            "step_attempt_id": attempt_id, "assertion_id": assertion.get("assertion_id"),
            "observation_id": assertion.get("observation_id"),
            # Attempt co-residence does not establish assertion-level evidence.
            "artifact_ids": [],
            "artifact_binding_state": "unbound_no_explicit_assertion_relation",
        })

    try:
        approval_state = validate_approvals(root, key_resolver=approval_key_resolver)
        approval_records = [{"record": to_json_compatible(item.record), "verification_status": item.verification_status.value,
                             "effective": item.effective, "verification_reason": item.reason} for item in approval_state.records]
        verified_count = len(approval_state.verified_approvals)
    except ApprovalError as exc:
        approval_records = [{"verification_status": VerificationStatus.INVALID.value, "effective": False, "verification_reason": str(exc)}]
        verified_count = 0
    try:
        audit_records = [to_json_compatible(item) for item in validate_audit_chain(root)]
        audit_state = "locally_chain_verified"
    except ApprovalError as exc:
        audit_records, audit_state = [], "invalid: " + str(exc)

    manifest = _pinned_bytes(root / "manifests", "manifest-0001.json", "source evidence manifest")
    snapshot = _detached_snapshot(root)
    return {
        "detached_ledger_snapshot": snapshot,
        "report_version": REPORT_VERSION,
        "renderer": {"id": REPORT_RENDERER_ID, "active_artifact_links": False},
        "evidence_trust_state": FinalizationTrustState.BOUND_VERIFIED.value,
        "report_trust_state": FinalizationTrustState.UNVERIFIED_DERIVED.value,
        "source": {"detached_ledger_snapshot": snapshot, "run_id": str(result.outcome.run_id), "finalization_id": str(result.outcome.finalization_id),
                   "evidence_revision": result.outcome.evidence_revision, "evidence_manifest_path": "manifests/manifest-0001.json",
                   "evidence_manifest_sha256": "sha256:" + hashlib.sha256(manifest).hexdigest()},
        "outcome": to_json_compatible(result.outcome), "run": run, "steps": steps, "attempts": attempts,
        "assertions": assertions, "observations": observations, "findings": findings, "artifacts": artifacts,
        "failures_and_ambiguities": failures, "traceability": traceability,
        "approvals": {"verified_effective_approval_count": verified_count, "records": approval_records},
        "audit": {"local_chain_state": audit_state, "records": audit_records,
                  "trust_note": "The local chain is not an external tamper-evidence boundary."},
        "timeline": timeline,
    }


def _indented(value: object) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
    return "\n".join("    " + line for line in text.splitlines())


def _markdown(model: Mapping[str, object]) -> bytes:
    source, outcome = model.get("source", {}), model.get("outcome", {})
    run_id = source.get("run_id") if isinstance(source, Mapping) else None
    status = outcome.get("effective_status") if isinstance(outcome, Mapping) else None
    safe_run, safe_status = str(run_id).replace("`", "\\`"), str(status).replace("`", "\\`")
    lines = ["# Argus ATES Test Execution Report", "", f"**Run:** `{safe_run}`", f"**Canonical status:** `{safe_status}`",
             f"**Evidence trust:** `{model.get('evidence_trust_state')}`", f"**Report trust:** `{model.get('report_trust_state')}`", "",
             "> Derived view only; canonical ATES evidence remains authoritative.", ""]
    sections = (("Execution / source", model.get("run")), ("Outcome", model.get("outcome")), ("Logical steps", model.get("steps")),
                ("Attempt history", model.get("attempts")), ("Assertions", model.get("assertions")), ("Observations", model.get("observations")),
                ("Artifacts (inert references only)", model.get("artifacts")), ("Findings", model.get("findings")),
                ("Failures / ambiguous outcomes", model.get("failures_and_ambiguities")), ("Requirement traceability", model.get("traceability")),
                ("Approvals", model.get("approvals")), ("Audit", model.get("audit")), ("Timeline", model.get("timeline")),
                ("Integrity / source binding", model.get("source")))
    for heading, value in sections: lines.extend(("## " + heading, "", _indented(value), ""))
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def _html(model: Mapping[str, object]) -> bytes:
    source, outcome = model.get("source", {}), model.get("outcome", {})
    run_id = source.get("run_id") if isinstance(source, Mapping) else None
    status = outcome.get("effective_status") if isinstance(outcome, Mapping) else None
    sections = (("Execution / source", model.get("run")), ("Outcome", model.get("outcome")), ("Logical steps", model.get("steps")),
                ("Attempt history", model.get("attempts")), ("Assertions", model.get("assertions")), ("Observations", model.get("observations")),
                ("Artifacts (inert references only)", model.get("artifacts")), ("Findings", model.get("findings")),
                ("Failures / ambiguous outcomes", model.get("failures_and_ambiguities")), ("Requirement traceability", model.get("traceability")),
                ("Approvals", model.get("approvals")), ("Audit", model.get("audit")), ("Timeline", model.get("timeline")),
                ("Integrity / source binding", model.get("source")))
    body = []
    for heading, value in sections:
        body.append("<section><h2>" + html.escape(heading, quote=True) + "</h2><pre>" +
                    html.escape(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False), quote=True) + "</pre></section>")
    doc = "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
    doc += "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; img-src 'none'; media-src 'none'; object-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'none'; style-src 'unsafe-inline'\">"
    doc += "<meta name=\"referrer\" content=\"no-referrer\"><title>Argus ATES report</title><style>body{font-family:system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f5f5;padding:1rem}</style></head><body>"
    doc += "<h1>Argus ATES Test Execution Report</h1><p><strong>Run:</strong> <code>" + html.escape(str(run_id), quote=True) + "</code></p>"
    doc += "<p><strong>Canonical status:</strong> <code>" + html.escape(str(status), quote=True) + "</code></p>"
    doc += "<p><strong>Evidence trust:</strong> " + html.escape(str(model.get("evidence_trust_state")), quote=True) + " &middot; <strong>Report trust:</strong> " + html.escape(str(model.get("report_trust_state")), quote=True) + "</p>"
    doc += "<p>Evidence-controlled URLs and artifact paths are inert text; no active links or embeds are generated.</p>" + "".join(body) + "</body></html>\n"
    return doc.encode("utf-8")


def _xml_text(value: object) -> str:
    """Filter text to XML 1.0 legal characters without regex range ambiguity."""
    out: list[str] = []
    for char in str(value):
        code = ord(char)
        legal = code in (0x09, 0x0A, 0x0D) or 0x20 <= code <= 0xD7FF or 0xE000 <= code <= 0xFFFD or 0x10000 <= code <= 0x10FFFF
        out.append(char if legal else "\uFFFD")
    return "".join(out)


def _junit(model: Mapping[str, object]) -> bytes:
    """Render one authoritative testcase whose result equals canonical ATES status.

    Historical attempts remain available in JSON/Markdown/HTML reports, but
    JUnit is consumed as a CI outcome protocol.  Emitting historical retries as
    independently failing testcases can invert the canonical result, so the
    suite contains exactly one synthetic canonical-outcome testcase.
    """
    source = model.get("source", {})
    outcome = model.get("outcome", {})
    run_id = source.get("run_id") if isinstance(source, Mapping) else "unknown"
    status = outcome.get("effective_status") if isinstance(outcome, Mapping) else None
    if status not in {
        RunStatus.PASSED.value,
        RunStatus.FAILED.value,
        RunStatus.ERROR.value,
        RunStatus.CANCELLED.value,
    }:
        status = RunStatus.ERROR.value

    failures = 1 if status == RunStatus.FAILED.value else 0
    errors = 1 if status == RunStatus.ERROR.value else 0
    skipped = 1 if status == RunStatus.CANCELLED.value else 0
    suite = ET.Element(
        "testsuite",
        {
            "name": _xml_text("Argus ATES " + str(run_id)),
            "tests": "1",
            "failures": str(failures),
            "errors": str(errors),
            "skipped": str(skipped),
        },
    )
    props = ET.SubElement(suite, "properties")
    for name, value in (
        ("ates.evidence_trust", model.get("evidence_trust_state")),
        ("ates.report_trust", model.get("report_trust_state")),
        ("ates.run_status", status),
    ):
        ET.SubElement(
            props,
            "property",
            {"name": name, "value": _xml_text(value)},
        )

    case = ET.SubElement(
        suite,
        "testcase",
        {
            "classname": "argus.ates.canonical",
            "name": _xml_text(f"canonical outcome for {run_id}"),
        },
    )
    if status == RunStatus.FAILED.value:
        ET.SubElement(
            case,
            "failure",
            {"message": "canonical ATES run outcome is failed"},
        )
    elif status == RunStatus.ERROR.value:
        ET.SubElement(
            case,
            "error",
            {"message": "canonical ATES run outcome is error"},
        )
    elif status == RunStatus.CANCELLED.value:
        ET.SubElement(
            case,
            "skipped",
            {"message": "canonical ATES run outcome is cancelled"},
        )

    # Preserve diagnostic visibility without giving historical attempts JUnit
    # pass/fail authority.  XML serializers keep this inert and escaped.
    raw_attempts = model.get("attempts", ())
    attempt_count = (
        len(tuple(raw_attempts))
        if isinstance(raw_attempts, Sequence)
        and not isinstance(raw_attempts, (str, bytes, bytearray, Mapping))
        else 0
    )
    ET.SubElement(
        props,
        "property",
        {"name": "ates.historical_attempt_count", "value": str(attempt_count)},
    )

    return (
        ET.tostring(
            suite,
            encoding="utf-8",
            xml_declaration=True,
            short_empty_elements=True,
        )
        + b"\n"
    )


def _rendered(model: Mapping[str, object]) -> dict[str, bytes]:
    return {"report.json": _json(model, pretty=True), "report.md": _markdown(model), "report.html": _html(model), "junit.xml": _junit(model)}


def _write(directory: _PinnedDirectory, name: str, data: bytes) -> Path:
    temp = f".{name}.argus-{uuid.uuid4().hex}.part"; handle = None
    try:
        handle, created = _open_regular_file(directory, temp)
        if not created: raise ReportError("report temporary file unexpectedly exists")
        if handle.write(data) != len(data): raise ReportError(f"short report write for {name}")
        handle.flush(); os.fsync(handle.fileno()); directory.assert_file_identity(temp, handle.fileno(), "report temporary file")
        handle.close(); handle = None
        if os.name == "nt": os.replace(directory.path / temp, directory.path / name)
        else:
            if directory._fd is None: raise ReportError("pinned reports directory has no descriptor")
            os.replace(temp, name, src_dir_fd=directory._fd, dst_dir_fd=directory._fd)
        directory.fsync()
        if _pinned_bytes(directory.path, name, "rendered report") != data: raise ReportError(f"rendered report {name} changed during publication")
        return directory.path / name
    except ReportError: raise
    except (OSError, AtesStoreError) as exc: raise ReportError(f"cannot publish {name} safely") from exc
    finally:
        if handle is not None:
            try: handle.close()
            except BaseException: pass
        try:
            if os.name == "nt": (directory.path / temp).unlink(missing_ok=True)
            elif directory._fd is not None:
                try: os.unlink(temp, dir_fd=directory._fd)
                except FileNotFoundError: pass
        except BaseException: pass


def _manifest(root: Path, members: Mapping[str, bytes]) -> dict[str, object]:
    source = _pinned_bytes(root / "manifests", "manifest-0001.json", "source evidence manifest")
    return {"detached_ledger_snapshot": _detached_snapshot(root), "report_manifest_version": REPORT_MANIFEST_VERSION, "renderer": {"id": REPORT_RENDERER_ID},
            "source_evidence_manifest": {"path": "manifests/manifest-0001.json", "sha256": "sha256:" + hashlib.sha256(source).hexdigest()},
            "members": [{"path": "reports/" + name, "size_bytes": len(data), "sha256": "sha256:" + hashlib.sha256(data).hexdigest()} for name, data in sorted(members.items())],
            "trust_note": "Local report hashes are not an independent trust boundary unless the report-manifest digest is verified externally."}


def _exists(directory: _PinnedDirectory, name: str) -> bool:
    try:
        if os.name == "nt":
            return os.path.lexists(directory.path / name)
        if directory._fd is None:
            raise ReportError("pinned reports directory has no descriptor")
        os.stat(name, dir_fd=directory._fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ReportError(f"cannot inspect report member {name}") from exc


def _replace(directory: _PinnedDirectory, source: str, target: str) -> None:
    try:
        if os.name == "nt":
            os.replace(directory.path / source, directory.path / target)
        else:
            if directory._fd is None:
                raise ReportError("pinned reports directory has no descriptor")
            os.replace(
                source,
                target,
                src_dir_fd=directory._fd,
                dst_dir_fd=directory._fd,
            )
    except ReportError:
        raise
    except OSError as exc:
        raise ReportError(
            f"cannot replace report member {source} -> {target}"
        ) from exc


def _unlink(directory: _PinnedDirectory, name: str) -> None:
    try:
        if os.name == "nt":
            (directory.path / name).unlink(missing_ok=True)
        else:
            if directory._fd is None:
                raise ReportError("pinned reports directory has no descriptor")
            try:
                os.unlink(name, dir_fd=directory._fd)
            except FileNotFoundError:
                pass
    except ReportError:
        raise
    except OSError as exc:
        raise ReportError(f"cannot remove report member {name}") from exc


def _rollback(
    reports: _PinnedDirectory,
    *,
    staged: dict[str, str],
    backups: dict[str, str],
    published: list[str],
    token: str,
) -> None:
    """Restore the exact previously named bundle or fail explicitly ambiguous."""
    trash: list[str] = []
    try:
        # First move every newly published target out of the way.  We keep the
        # bytes until the prior names are restored so a failed rollback never
        # destroys the only remaining copy silently.
        for name in reversed(published):
            if not _exists(reports, name):
                raise ReportError(
                    f"new report member disappeared during rollback: {name}"
                )
            trash_name = f".{name}.failed-{token}"
            if _exists(reports, trash_name):
                raise ReportError("report rollback trash name unexpectedly exists")
            _replace(reports, name, trash_name)
            trash.append(trash_name)

        # Restore every member that existed before the transaction. Members that
        # did not previously exist remain absent because their new copy was moved
        # to trash above.
        for name, backup_name in backups.items():
            if not _exists(reports, backup_name):
                raise ReportError(
                    f"report rollback backup disappeared for {name}"
                )
            if _exists(reports, name):
                raise ReportError(
                    f"report rollback target unexpectedly exists for {name}"
                )
            _replace(reports, backup_name, name)

        reports.fsync()

        # Once the old bundle is durable again, temporary/new bytes may be
        # discarded. Any failure here is surfaced rather than hidden.
        for stage_name in staged.values():
            if _exists(reports, stage_name):
                _unlink(reports, stage_name)
        for trash_name in trash:
            if _exists(reports, trash_name):
                _unlink(reports, trash_name)
        reports.fsync()
    except BaseException as exc:
        raise ReportError(
            "report publication failed and rollback is incomplete or ambiguous"
        ) from exc


@contextmanager
def _report_transaction(root: Path):
    """Hold a report-directory-scoped writer lock for one whole generation."""
    deadline = time.monotonic() + _LOCK_WAIT_SECONDS
    run_pin = reports = lock = None
    while True:
        run_pin = _PinnedDirectory(root)
        try:
            reports = run_pin.ensure_child("reports", "ATES reports directory")
            run_pin.assert_child_identity("reports", reports, "ATES reports directory")
            lock = _WriterLock(reports)
            lock.assert_authoritative()
            break
        except AtesStoreBusy as exc:
            if reports is not None:
                reports.close()
            run_pin.close()
            reports = run_pin = None
            if time.monotonic() >= deadline:
                raise ReportError(
                    "timed out waiting for report writer authority"
                ) from exc
            time.sleep(_LOCK_RETRY_SECONDS)
        except BaseException:
            if reports is not None:
                reports.close()
            if run_pin is not None:
                run_pin.close()
            raise
    try:
        yield run_pin, reports, lock
    finally:
        if lock is not None:
            try:
                lock.close()
            except BaseException:
                pass
        if reports is not None:
            try:
                reports.close()
            except BaseException:
                pass
        if run_pin is not None:
            try:
                run_pin.close()
            except BaseException:
                pass


def _render_reports_locked(
    root: Path,
    run_pin: _PinnedDirectory,
    reports: _PinnedDirectory,
    lock: _WriterLock,
    *,
    approval_key_resolver=None,
):
    """Render while the caller owns the report-scoped writer authority."""
    # Build the model only after acquiring authority. Detached-ledger changes
    # remain detectable by the post-publication verifier, while other report
    # writers cannot interfere with this transaction's names or backups.
    lock.assert_authoritative()
    model = _model(root, approval_key_resolver)
    members = _rendered(model)
    manifest_bytes = _json(_manifest(root, members))
    desired = dict(members)
    desired["report-manifest-0001.json"] = manifest_bytes

    token = uuid.uuid4().hex
    staged: dict[str, str] = {}
    backups: dict[str, str] = {}
    published: list[str] = []
    paths: dict[str, Path] = {}

    try:
        run_pin.assert_child_identity("reports", reports, "ATES reports directory")
        lock.assert_authoritative()

        # Stage *all* bytes under private names first. A late renderer/write
        # failure cannot touch the previously published bundle.
        for name, data in desired.items():
            stage_name = f".{name}.stage-{token}"
            if _exists(reports, stage_name):
                raise ReportError("report staging name unexpectedly exists")
            _write(reports, stage_name, data)
            if _pinned_bytes(
                reports.path, stage_name, f"staged report {name}"
            ) != data:
                raise ReportError(f"staged report changed before commit: {name}")
            staged[name] = stage_name

        run_pin.assert_child_identity("reports", reports, "ATES reports directory")

        # Preserve every currently named member before replacing anything.
        for name in desired:
            if not _exists(reports, name):
                continue
            # Refuse to move unsafe/symlinked old state under the guise of a
            # transactional backup.
            _pinned_bytes(reports.path, name, f"existing report {name}")
            backup_name = f".{name}.backup-{token}"
            if _exists(reports, backup_name):
                raise ReportError("report backup name unexpectedly exists")
            _replace(reports, name, backup_name)
            backups[name] = backup_name

        reports.fsync()

        # Commit the complete staged generation. Any failure after the first
        # replacement is rolled back to the exact preserved generation below.
        for name, stage_name in staged.items():
            _replace(reports, stage_name, name)
            published.append(name)
            paths[name] = reports.path / name
        reports.fsync()
        run_pin.assert_child_identity("reports", reports, "ATES reports directory")
        lock.assert_authoritative()

        checked = verify_report_bundle(
            root,
            approval_key_resolver=approval_key_resolver,
        )
        if checked.trust_state is not FinalizationTrustState.REGENERATED_VERIFIED:
            raise ReportError(
                checked.error or "regenerated report verification failed"
            )
        lock.assert_authoritative()

    except BaseException as exc:
        if reports is not None:
            try:
                _rollback(
                    reports,
                    staged=staged,
                    backups=backups,
                    published=published,
                    token=token,
                )
            except ReportError as rollback_exc:
                raise rollback_exc from exc
        if isinstance(exc, ReportError):
            raise
        if isinstance(exc, (OSError, AtesStoreError)):
            raise ReportError("cannot publish report bundle safely") from exc
        raise

    # The new generation is now durable and verified. Cleanup is outside the
    # rollback boundary: once any restoration backup is removed, rolling back
    # could destroy the only complete bundle. Surface cleanup errors while
    # leaving the committed public members and any remaining backups intact.
    try:
        for backup_name in backups.values():
            if _exists(reports, backup_name):
                _unlink(reports, backup_name)
        reports.fsync()
    except (OSError, AtesStoreError, ReportError) as exc:
        raise ReportError(
            "report bundle committed, but backup cleanup failed"
        ) from exc

    return ReportBundle(
        root,
        root / "reports",
        paths["report.json"],
        paths["report.md"],
        paths["report.html"],
        paths["junit.xml"],
        paths["report-manifest-0001.json"],
        checked.trust_state,
    )


def render_reports(
    run_dir: Path | str,
    *,
    approval_key_resolver=None,
):
    root = _root(run_dir)
    # Report publication has its own cross-process writer authority. Detached
    # ledgers retain their independent append lock; the post-publication
    # verifier detects any ledger change and triggers rollback while no second
    # renderer can touch this generation's names/backups.
    with _report_transaction(root) as (run_pin, reports, lock):
        lock.assert_authoritative()
        return _render_reports_locked(
            root,
            run_pin,
            reports,
            lock,
            approval_key_resolver=approval_key_resolver,
        )


def inspect_report_bundle(run_dir: Path | str) -> ReportVerificationResult:
    root = _root(run_dir); report_dir = root / "reports"; manifest = report_dir / "report-manifest-0001.json"
    if manifest.is_file() and all((report_dir / name).is_file() for name in _REPORT_NAMES):
        return ReportVerificationResult(FinalizationTrustState.UNVERIFIED_DERIVED, report_dir, manifest, "existing report bytes have not been regenerated or independently bound")
    return ReportVerificationResult(FinalizationTrustState.INVALID, report_dir, manifest if manifest.exists() else None, "report bundle is incomplete")


def verify_report_bundle(run_dir: Path | str, *, trusted_report_manifest_digest: Optional[str] = None, approval_key_resolver: Optional[KeyResolver] = None) -> ReportVerificationResult:
    try:
        root = _root(run_dir); verify_finalized_run(root); report_dir = root / "reports"; manifest_raw = _pinned_bytes(report_dir, "report-manifest-0001.json", "report manifest"); manifest = _strict_object(manifest_raw, "report manifest")
        if trusted_report_manifest_digest is not None:
            if (
                not isinstance(trusted_report_manifest_digest, str)
                or not trusted_report_manifest_digest.startswith("sha256:")
                or len(trusted_report_manifest_digest) != len("sha256:") + 64
            ):
                raise ReportError(
                    "trusted report-manifest digest must be sha256:<64 lowercase/uppercase hex>"
                )
            expected = trusted_report_manifest_digest[len("sha256:") :]
            try:
                int(expected, 16)
            except ValueError as exc:
                raise ReportError(
                    "trusted report-manifest digest must contain hexadecimal SHA-256"
                ) from exc
            actual_digest = "sha256:" + hashlib.sha256(manifest_raw).hexdigest()
            if not hmac.compare_digest(actual_digest.lower(), trusted_report_manifest_digest.lower()):
                raise ReportError("external report-manifest binding does not verify")
        if manifest.get("report_manifest_version") != REPORT_MANIFEST_VERSION: raise ReportError("unsupported report manifest version")
        renderer = manifest.get("renderer")
        if not isinstance(renderer, Mapping) or renderer.get("id") != REPORT_RENDERER_ID: raise ReportError("unsupported report renderer identity")
        source = manifest.get("source_evidence_manifest"); source_raw = _pinned_bytes(root / "manifests", "manifest-0001.json", "source evidence manifest"); source_digest = "sha256:" + hashlib.sha256(source_raw).hexdigest()
        if not isinstance(source, Mapping) or source.get("path") != "manifests/manifest-0001.json" or not isinstance(source.get("sha256"), str) or not hmac.compare_digest(source_digest, source["sha256"]): raise ReportError("report manifest source binding does not verify")
        listed = manifest.get("members")
        if isinstance(listed, (str, bytes, bytearray, Mapping)) or not isinstance(listed, Sequence): raise ReportError("report manifest members are malformed")
        by_path: dict[str, Mapping[str, object]] = {}
        for item in tuple(listed):
            if not isinstance(item, Mapping) or not isinstance(item.get("path"), str) or item["path"] in by_path: raise ReportError("report manifest contains malformed/duplicate member")
            by_path[item["path"]] = item
        actual: dict[str, bytes] = {}
        for name in _REPORT_NAMES:
            meta = by_path.get("reports/" + name)
            if meta is None: raise ReportError(f"report manifest is missing {name}")
            data = _pinned_bytes(report_dir, name, f"rendered report {name}"); digest = "sha256:" + hashlib.sha256(data).hexdigest()
            if meta.get("size_bytes") != len(data) or not isinstance(meta.get("sha256"), str) or not hmac.compare_digest(digest, meta["sha256"]): raise ReportError(f"report byte binding failed for {name}")
            actual[name] = data
        if trusted_report_manifest_digest is not None:
            # External binding authenticates these point-in-time report bytes;
            # later detached-ledger changes affect freshness, not this binding.
            return ReportVerificationResult(FinalizationTrustState.BOUND_VERIFIED, report_dir, report_dir / "report-manifest-0001.json")
        expected = _rendered(_model(root, approval_key_resolver))
        for name in _REPORT_NAMES:
            if actual[name] != expected[name]: raise ReportError(f"semantic regeneration differs for {name}")
        if manifest_raw != _json(_manifest(root, expected)): raise ReportError("report manifest differs from regenerated canonical manifest")
        return ReportVerificationResult(FinalizationTrustState.REGENERATED_VERIFIED, report_dir, report_dir / "report-manifest-0001.json")
    except (ReportError, FinalizationError, OSError, ValueError) as exc:
        try: report_dir = _root(run_dir) / "reports"
        except ReportError: report_dir = Path(run_dir) / "reports"
        return ReportVerificationResult(FinalizationTrustState.INVALID, report_dir, report_dir / "report-manifest-0001.json", str(exc))


__all__ = ["REPORT_MANIFEST_VERSION", "REPORT_RENDERER_ID", "REPORT_VERSION", "FinalizationTrustInspection", "ReportBundle", "ReportError", "ReportVerificationResult", "inspect_finalization_trust", "inspect_report_bundle", "render_reports", "verify_report_bundle"]
