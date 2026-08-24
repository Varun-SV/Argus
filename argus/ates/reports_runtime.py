"""Portable, context-safe ATES v0.1 report implementation."""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence

from .audit import ApprovalError, KeyResolver, validate_approvals, validate_audit_chain
from .core import EventType, RunStatus, VerificationStatus, to_json_compatible
from .finalization import FinalizationError, FinalizationTrustState, verify_finalized_run
from .store import AtesEventStore, AtesStoreError, _PinnedDirectory, _open_regular_file

REPORT_VERSION = "ates-report-v1"
REPORT_RENDERER_ID = "argus-ates-stdlib-renderer-v1"
REPORT_MANIFEST_VERSION = "ates-report-manifest-v1"
_REPORT_NAMES = ("report.json", "report.md", "report.html", "junit.xml")


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
    from . import finalization_impl as finalization
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
    artifacts_by_attempt: dict[str, list[str]] = {}

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
                artifacts.append({"record": record, "sequence": event.sequence, "step_attempt_id": attempt_id})
                artifact_id = record.get("artifact_id")
                if isinstance(attempt_id, str) and isinstance(artifact_id, str):
                    artifacts_by_attempt.setdefault(attempt_id, []).append(artifact_id)
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
            "artifact_ids": list(artifacts_by_attempt.get(str(attempt_id), ())),
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
    return {
        "report_version": REPORT_VERSION,
        "renderer": {"id": REPORT_RENDERER_ID, "active_artifact_links": False},
        "evidence_trust_state": FinalizationTrustState.BOUND_VERIFIED.value,
        "report_trust_state": FinalizationTrustState.REGENERATED_VERIFIED.value,
        "source": {"run_id": str(result.outcome.run_id), "finalization_id": str(result.outcome.finalization_id),
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
    source, outcome = model.get("source", {}), model.get("outcome", {})
    run_id = source.get("run_id") if isinstance(source, Mapping) else "unknown"
    status = outcome.get("effective_status") if isinstance(outcome, Mapping) else RunStatus.ERROR.value
    raw = model.get("attempts", ())
    attempts = tuple(raw) if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray, Mapping)) else ()
    failures = sum(1 for item in attempts if isinstance(item, Mapping) and item.get("status") == "failed")
    errors = sum(1 for item in attempts if isinstance(item, Mapping) and item.get("status") in {"error", "outcome_unknown"})
    skipped = sum(1 for item in attempts if isinstance(item, Mapping) and item.get("status") == "cancelled")
    suite = ET.Element("testsuite", {"name": _xml_text("Argus ATES " + str(run_id)), "tests": str(max(1, len(attempts))), "failures": str(failures), "errors": str(errors), "skipped": str(skipped)})
    props = ET.SubElement(suite, "properties")
    for name, value in (("ates.evidence_trust", model.get("evidence_trust_state")), ("ates.report_trust", model.get("report_trust_state")), ("ates.run_status", status)):
        ET.SubElement(props, "property", {"name": name, "value": _xml_text(value)})
    if attempts:
        for item in attempts:
            if not isinstance(item, Mapping): continue
            case = ET.SubElement(suite, "testcase", {"classname": "argus.ates", "name": _xml_text(item.get("step_id", "unknown-step"))})
            item_status = item.get("status")
            if item_status == "failed": ET.SubElement(case, "failure", {"message": "deterministic step failure"})
            elif item_status in {"error", "outcome_unknown"}: ET.SubElement(case, "error", {"message": "step outcome is unreliable"})
            elif item_status == "cancelled": ET.SubElement(case, "skipped", {"message": "step cancelled"})
    else:
        case = ET.SubElement(suite, "testcase", {"classname": "argus.ates", "name": _xml_text(run_id)})
        if status == RunStatus.FAILED.value: ET.SubElement(case, "failure", {"message": "run failed"})
        elif status == RunStatus.ERROR.value: ET.SubElement(case, "error", {"message": "run errored"})
        elif status == RunStatus.CANCELLED.value: ET.SubElement(case, "skipped", {"message": "run cancelled"})
    return ET.tostring(suite, encoding="utf-8", xml_declaration=True, short_empty_elements=True) + b"\n"


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
    return {"report_manifest_version": REPORT_MANIFEST_VERSION, "renderer": {"id": REPORT_RENDERER_ID},
            "source_evidence_manifest": {"path": "manifests/manifest-0001.json", "sha256": "sha256:" + hashlib.sha256(source).hexdigest()},
            "members": [{"path": "reports/" + name, "size_bytes": len(data), "sha256": "sha256:" + hashlib.sha256(data).hexdigest()} for name, data in sorted(members.items())],
            "trust_note": "Local report hashes are not an independent trust boundary unless the report-manifest digest is verified externally."}


def render_reports(run_dir: Path | str, *, approval_key_resolver: Optional[KeyResolver] = None) -> ReportBundle:
    root = _root(run_dir); model = _model(root, approval_key_resolver); members = _rendered(model); run_pin = reports = None
    try:
        run_pin = _PinnedDirectory(root); reports = run_pin.ensure_child("reports", "ATES reports directory"); run_pin.assert_child_identity("reports", reports, "ATES reports directory")
        paths = {name: _write(reports, name, data) for name, data in members.items()}
        manifest_path = _write(reports, "report-manifest-0001.json", _json(_manifest(root, members))); run_pin.assert_child_identity("reports", reports, "ATES reports directory")
    except (OSError, AtesStoreError) as exc: raise ReportError("cannot establish reports directory") from exc
    finally:
        if reports is not None:
            try: reports.close()
            except BaseException: pass
        if run_pin is not None:
            try: run_pin.close()
            except BaseException: pass
    checked = verify_report_bundle(root, approval_key_resolver=approval_key_resolver)
    if checked.trust_state is not FinalizationTrustState.REGENERATED_VERIFIED: raise ReportError(checked.error or "regenerated report verification failed")
    return ReportBundle(root, root / "reports", paths["report.json"], paths["report.md"], paths["report.html"], paths["junit.xml"], manifest_path, checked.trust_state)


def inspect_report_bundle(run_dir: Path | str) -> ReportVerificationResult:
    root = _root(run_dir); report_dir = root / "reports"; manifest = report_dir / "report-manifest-0001.json"
    if manifest.is_file() and all((report_dir / name).is_file() for name in _REPORT_NAMES):
        return ReportVerificationResult(FinalizationTrustState.UNVERIFIED_DERIVED, report_dir, manifest, "existing report bytes have not been regenerated or independently bound")
    return ReportVerificationResult(FinalizationTrustState.INVALID, report_dir, manifest if manifest.exists() else None, "report bundle is incomplete")


def verify_report_bundle(run_dir: Path | str, *, trusted_report_manifest_digest: Optional[str] = None, approval_key_resolver: Optional[KeyResolver] = None) -> ReportVerificationResult:
    try:
        root = _root(run_dir); verify_finalized_run(root); report_dir = root / "reports"; manifest_raw = _pinned_bytes(report_dir, "report-manifest-0001.json", "report manifest"); manifest = _strict_object(manifest_raw, "report manifest")
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
        expected = _rendered(_model(root, approval_key_resolver))
        for name in _REPORT_NAMES:
            if actual[name] != expected[name]: raise ReportError(f"semantic regeneration differs for {name}")
        if manifest_raw != _json(_manifest(root, expected)): raise ReportError("report manifest differs from regenerated canonical manifest")
        state = FinalizationTrustState.REGENERATED_VERIFIED
        if trusted_report_manifest_digest is not None:
            if not isinstance(trusted_report_manifest_digest, str) or not trusted_report_manifest_digest.startswith("sha256:"): raise ReportError("trusted report-manifest digest must be sha256:<hex>")
            digest = "sha256:" + hashlib.sha256(manifest_raw).hexdigest()
            if not hmac.compare_digest(digest, trusted_report_manifest_digest): raise ReportError("external report-manifest binding does not verify")
            state = FinalizationTrustState.BOUND_VERIFIED
        return ReportVerificationResult(state, report_dir, report_dir / "report-manifest-0001.json")
    except (ReportError, FinalizationError, OSError, ValueError) as exc:
        try: report_dir = _root(run_dir) / "reports"
        except ReportError: report_dir = Path(run_dir) / "reports"
        return ReportVerificationResult(FinalizationTrustState.INVALID, report_dir, report_dir / "report-manifest-0001.json", str(exc))


__all__ = ["REPORT_MANIFEST_VERSION", "REPORT_RENDERER_ID", "REPORT_VERSION", "FinalizationTrustInspection", "ReportBundle", "ReportError", "ReportVerificationResult", "inspect_finalization_trust", "inspect_report_bundle", "render_reports", "verify_report_bundle"]
