"""Context-safe ATES report rendering from finalized canonical evidence.

Reports are derived views, never the truth store.  Rendering starts from a
successfully verified finalization and every output format performs escaping at
its final context boundary.  Existing report files are unverified conveniences
until either regenerated from verified evidence or checked against an
independently trusted report-manifest digest.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence

from .audit import ApprovalError, KeyResolver, validate_approvals, validate_audit_chain
from .core import EventType, RunStatus, StepAttemptStatus, VerificationStatus, to_json_compatible
from .finalization import FinalizationError, FinalizationTrustState, verify_finalized_run
from .store import AtesEventStore, AtesStoreError, _PinnedDirectory, _open_regular_file

REPORT_VERSION = "ates-report-v1"
REPORT_RENDERER_ID = "argus-ates-stdlib-renderer-v1"
REPORT_MANIFEST_VERSION = "ates-report-manifest-v1"
_REPORT_NAMES = ("report.json", "report.md", "report.html", "junit.xml")
_XML_ILLEGAL = re.compile(
    "[\x00-\x08\x0B\x0C\x0E-\x1F\uD800-\uDFFF\uFFFE\uFFFF]"
)


class ReportError(RuntimeError):
    """A report cannot be derived or verified safely."""


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


def _canonical_json(value: object, *, pretty: bool = False) -> bytes:
    try:
        if pretty:
            text = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
        else:
            text = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ReportError(f"report model is not JSON serializable: {exc}") from exc
    return text.encode("utf-8") + b"\n"


def _strict_object(raw: bytes, label: str) -> dict[str, object]:
    def pairs(items):
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate key {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda item: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant {item}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ReportError(f"{label} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise ReportError(f"{label} must contain an object")
    if _canonical_json(value, pretty=False) != raw:
        raise ReportError(f"{label} is not in canonical persisted representation")
    return value


def _run_root(run_dir: Path | str) -> Path:
    try:
        root = Path(run_dir).resolve(strict=True)
    except OSError as exc:
        raise ReportError(f"cannot resolve ATES run directory: {exc}") from exc
    if root.parent.name != "runs" or root.parent.parent.name != ".argus":
        raise ReportError("report source is not beneath a canonical .argus/runs directory")
    return root


def _project(root: Path) -> Path:
    return root.parent.parent.parent


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
            try:
                pin.close()
            except BaseException:
                pass


def inspect_finalization_trust(run_dir: Path | str) -> FinalizationTrustInspection:
    """Return a consumer-facing trust state without treating labels as authority."""
    root = _run_root(run_dir)
    if not (root / "run.json").exists():
        if (root / "evidence.jsonl").exists():
            return FinalizationTrustInspection(
                FinalizationTrustState.UNVERIFIED_DERIVED,
                root,
                "run has canonical evidence but no final binding",
            )
        return FinalizationTrustInspection(
            FinalizationTrustState.INVALID,
            root,
            "run has no canonical evidence",
        )
    try:
        verify_finalized_run(root)
    except (FinalizationError, OSError, ValueError) as exc:
        return FinalizationTrustInspection(
            FinalizationTrustState.INVALID,
            root,
            str(exc),
        )
    return FinalizationTrustInspection(FinalizationTrustState.BOUND_VERIFIED, root)


def _events_for_verified_run(root: Path):
    result = verify_finalized_run(root)
    store = None
    try:
        store = AtesEventStore(_project(root), result.outcome.run_id)
        if store.run_dir.resolve(strict=True) != root:
            raise ReportError("verified run_id resolves to another run directory")
        return result, tuple(store.events)
    except (OSError, AtesStoreError, ValueError) as exc:
        raise ReportError("cannot open verified canonical events for rendering") from exc
    finally:
        if store is not None:
            try:
                store.close()
            except BaseException:
                pass


def _payload_object(event, key: str) -> Optional[dict[str, object]]:
    value = event.payload.get(key)
    if not isinstance(value, Mapping):
        return None
    converted = to_json_compatible(value)
    return converted if isinstance(converted, dict) else None


def _requirement_identity(requirement: Mapping[str, object]) -> str:
    return "sha256:" + hashlib.sha256(
        _canonical_json(to_json_compatible(requirement), pretty=False).rstrip(b"\n")
    ).hexdigest()


def _report_model(
    root: Path,
    *,
    approval_key_resolver: Optional[KeyResolver] = None,
) -> dict[str, object]:
    result, events = _events_for_verified_run(root)
    run_started = next(
        (event for event in events if event.envelope.event_type is EventType.RUN_STARTED),
        None,
    )
    if run_started is None:
        raise ReportError("verified evidence has no RUN_STARTED event")

    run_record = _payload_object(run_started, "run") or {}
    raw_steps = run_started.payload.get("steps", ())
    if isinstance(raw_steps, Sequence) and not isinstance(raw_steps, (str, bytes, bytearray, Mapping)):
        steps = [to_json_compatible(item) for item in tuple(raw_steps)]
    else:
        steps = []

    attempts: list[dict[str, object]] = []
    assertions: list[dict[str, object]] = []
    observations: list[dict[str, object]] = []
    artifacts: list[dict[str, object]] = []
    findings: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []
    timeline: list[dict[str, object]] = []
    artifact_ids_by_attempt: dict[str, list[str]] = {}

    for event in events:
        event_type = event.envelope.event_type
        timeline.append(
            {
                "sequence": event.sequence,
                "event_id": str(event.event_id),
                "event_type": event_type.value,
                "occurred_at": event.envelope.occurred_at.isoformat(),
            }
        )
        if event_type is EventType.STEP_ATTEMPT_COMPLETED:
            record = _payload_object(event, "attempt")
            if record is not None:
                attempts.append(record)
                if record.get("status") not in {StepAttemptStatus.PASSED.value}:
                    failures.append(
                        {
                            "type": "step_attempt",
                            "sequence": event.sequence,
                            "step_id": record.get("step_id"),
                            "step_attempt_id": record.get("step_attempt_id"),
                            "status": record.get("status"),
                        }
                    )
        elif event_type is EventType.ASSERTION_EVALUATED:
            record = _payload_object(event, "assertion")
            if record is not None:
                assertions.append(record)
                if record.get("result") not in {"passed", "skipped"}:
                    failures.append(
                        {
                            "type": "assertion",
                            "sequence": event.sequence,
                            "assertion_id": record.get("assertion_id"),
                            "result": record.get("result"),
                        }
                    )
        elif event_type is EventType.OBSERVATION_CAPTURED:
            record = _payload_object(event, "observation")
            if record is not None:
                observations.append(record)
        elif event_type in {EventType.CHECKPOINT_CAPTURED, EventType.ARTIFACT_COLLECTED}:
            record = _payload_object(event, "artifact")
            if record is not None:
                context = event.payload.get("context")
                relationship_attempt = event.payload.get("step_attempt_id")
                entry = {
                    "artifact": record,
                    "context": context,
                    "step_attempt_id": relationship_attempt,
                    "sequence": event.sequence,
                }
                artifacts.append(entry)
                artifact_id = record.get("artifact_id")
                if isinstance(relationship_attempt, str) and isinstance(artifact_id, str):
                    artifact_ids_by_attempt.setdefault(relationship_attempt, []).append(artifact_id)
        elif event_type is EventType.ARTIFACT_SUPPRESSED:
            converted = to_json_compatible(event.payload)
            if isinstance(converted, dict):
                artifacts.append({"suppressed": converted, "sequence": event.sequence})
        elif event_type is EventType.FINDING_RECORDED:
            record = _payload_object(event, "finding")
            if record is not None:
                findings.append(record)
        elif event_type is EventType.ACTION_OUTCOME_UNKNOWN:
            failures.append(
                {
                    "type": "action_outcome_unknown",
                    "sequence": event.sequence,
                    "action_id": event.payload.get("action_id"),
                    "operation_id": event.payload.get("operation_id"),
                }
            )
        elif event_type is EventType.RUN_MARKED_INCOMPLETE and event.payload.get("reason") != "runtime.finalization_pending":
            failures.append(
                {
                    "type": "run_incomplete",
                    "sequence": event.sequence,
                    "reason": event.payload.get("reason"),
                }
            )

    traceability: list[dict[str, object]] = []
    for assertion in assertions:
        requirement = assertion.get("requirement")
        if not isinstance(requirement, Mapping):
            continue
        attempt_id = assertion.get("step_attempt_id")
        traceability.append(
            {
                "requirement_identity": to_json_compatible(requirement),
                "requirement_identity_digest": _requirement_identity(requirement),
                "test_case_id": (
                    run_record.get("source", {}).get("test_case_id")
                    if isinstance(run_record.get("source"), Mapping)
                    else None
                ),
                "run_id": str(result.outcome.run_id),
                "step_id": assertion.get("step_id"),
                "step_attempt_id": attempt_id,
                "assertion_id": assertion.get("assertion_id"),
                "observation_id": assertion.get("observation_id"),
                "artifact_ids": list(artifact_ids_by_attempt.get(str(attempt_id), ())),
            }
        )

    try:
        approval_result = validate_approvals(root, key_resolver=approval_key_resolver)
        approvals = [
            {
                "record": to_json_compatible(item.record),
                "verification_status": item.verification_status.value,
                "effective": item.effective,
                "verification_reason": item.reason,
            }
            for item in approval_result.records
        ]
        verified_approval_count = len(approval_result.verified_approvals)
    except ApprovalError as exc:
        approvals = [
            {
                "verification_status": VerificationStatus.INVALID.value,
                "effective": False,
                "verification_reason": str(exc),
            }
        ]
        verified_approval_count = 0

    try:
        audit_records = [to_json_compatible(item) for item in validate_audit_chain(root)]
        audit_state = "locally_chain_verified"
    except ApprovalError as exc:
        audit_records = []
        audit_state = "invalid: " + str(exc)

    manifest_raw = _pinned_bytes(
        root / "manifests",
        "manifest-0001.json",
        "source evidence manifest",
    )
    manifest_digest = "sha256:" + hashlib.sha256(manifest_raw).hexdigest()

    outcome = to_json_compatible(result.outcome)
    model: dict[str, object] = {
        "report_version": REPORT_VERSION,
        "renderer": {
            "id": REPORT_RENDERER_ID,
            "active_artifact_links": False,
            "unsafe_url_schemes": "rendered_as_inert_text",
        },
        "evidence_trust_state": FinalizationTrustState.BOUND_VERIFIED.value,
        "report_trust_state": FinalizationTrustState.REGENERATED_VERIFIED.value,
        "source": {
            "run_id": str(result.outcome.run_id),
            "finalization_id": str(result.outcome.finalization_id),
            "evidence_revision": result.outcome.evidence_revision,
            "evidence_manifest_path": "manifests/manifest-0001.json",
            "evidence_manifest_sha256": manifest_digest,
        },
        "outcome": outcome,
        "run": run_record,
        "steps": steps,
        "attempts": attempts,
        "assertions": assertions,
        "observations": observations,
        "artifacts": artifacts,
        "findings": findings,
        "failures_and_ambiguities": failures,
        "traceability": traceability,
        "approvals": {
            "verified_effective_approval_count": verified_approval_count,
            "records": approvals,
            "note": "Only records whose detached authentication verifies are treated as approvals.",
        },
        "audit": {
            "local_chain_state": audit_state,
            "records": audit_records,
            "note": "A local hash chain detects inconsistency only while its head is trusted; it is not an external tamper-evidence boundary.",
        },
        "timeline": timeline,
    }
    return model


def _indented_json(value: object) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
    return "\n".join("    " + line for line in text.splitlines())


def _render_markdown(model: Mapping[str, object]) -> bytes:
    source = model.get("source", {})
    outcome = model.get("outcome", {})
    status = outcome.get("effective_status") if isinstance(outcome, Mapping) else None
    run_id = source.get("run_id") if isinstance(source, Mapping) else None
    lines = [
        "# Argus ATES Test Execution Report",
        "",
        f"**Run:** `{str(run_id).replace('`', '\\`')}`",
        f"**Canonical status:** `{str(status).replace('`', '\\`')}`",
        f"**Evidence trust:** `{model.get('evidence_trust_state')}`",
        f"**Report trust:** `{model.get('report_trust_state')}`",
        "",
        "> Reports are derived views. Canonical evidence and its verified finalization remain authoritative.",
        "",
    ]
    sections = (
        ("Execution / source", model.get("run")),
        ("Outcome", model.get("outcome")),
        ("Logical steps", model.get("steps")),
        ("Attempt history", model.get("attempts")),
        ("Assertions", model.get("assertions")),
        ("Observations", model.get("observations")),
        ("Artifacts (inert references only)", model.get("artifacts")),
        ("Findings", model.get("findings")),
        ("Failures / ambiguous outcomes", model.get("failures_and_ambiguities")),
        ("Requirement traceability", model.get("traceability")),
        ("Approvals", model.get("approvals")),
        ("Audit", model.get("audit")),
        ("Timeline", model.get("timeline")),
        ("Integrity / source binding", model.get("source")),
    )
    for heading, value in sections:
        lines.extend((f"## {heading}", "", _indented_json(value), ""))
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def _html_pre(value: object) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
    return "<pre>" + html.escape(text, quote=True) + "</pre>"


def _render_html(model: Mapping[str, object]) -> bytes:
    source = model.get("source", {})
    outcome = model.get("outcome", {})
    run_id = source.get("run_id") if isinstance(source, Mapping) else None
    status = outcome.get("effective_status") if isinstance(outcome, Mapping) else None
    sections = (
        ("Execution / source", model.get("run")),
        ("Outcome", model.get("outcome")),
        ("Logical steps", model.get("steps")),
        ("Attempt history", model.get("attempts")),
        ("Assertions", model.get("assertions")),
        ("Observations", model.get("observations")),
        ("Artifacts (inert references only)", model.get("artifacts")),
        ("Findings", model.get("findings")),
        ("Failures / ambiguous outcomes", model.get("failures_and_ambiguities")),
        ("Requirement traceability", model.get("traceability")),
        ("Approvals", model.get("approvals")),
        ("Audit", model.get("audit")),
        ("Timeline", model.get("timeline")),
        ("Integrity / source binding", model.get("source")),
    )
    body = []
    for heading, value in sections:
        body.append("<section><h2>" + html.escape(heading, quote=True) + "</h2>" + _html_pre(value) + "</section>")
    document = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'none'; media-src 'none'; object-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'none'; style-src 'unsafe-inline'">
<meta name="referrer" content="no-referrer"><title>Argus ATES report</title>
<style>body{font-family:system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f5f5;padding:1rem;border-radius:.4rem}code{font-family:ui-monospace,monospace}.trust{font-weight:700}</style></head><body>"""
    document += "<h1>Argus ATES Test Execution Report</h1>"
    document += "<p><strong>Run:</strong> <code>" + html.escape(str(run_id), quote=True) + "</code></p>"
    document += "<p><strong>Canonical status:</strong> <code>" + html.escape(str(status), quote=True) + "</code></p>"
    document += "<p class=\"trust\">Evidence trust: " + html.escape(str(model.get("evidence_trust_state")), quote=True) + " &middot; Report trust: " + html.escape(str(model.get("report_trust_state")), quote=True) + "</p>"
    document += "<p>Reports are derived views. Artifact paths and URL-like evidence are rendered as inert text; this document creates no evidence-controlled links or embeds.</p>"
    document += "".join(body) + "</body></html>\n"
    return document.encode("utf-8")


def _xml_text(value: object) -> str:
    return _XML_ILLEGAL.sub("\uFFFD", str(value))


def _render_junit(model: Mapping[str, object]) -> bytes:
    source = model.get("source", {})
    outcome = model.get("outcome", {})
    run_id = source.get("run_id") if isinstance(source, Mapping) else "unknown"
    status = outcome.get("effective_status") if isinstance(outcome, Mapping) else "error"
    attempts = model.get("attempts", ())
    if not isinstance(attempts, Sequence) or isinstance(attempts, (str, bytes, bytearray, Mapping)):
        attempts = ()
    tests = max(1, len(attempts))
    failures = sum(1 for item in attempts if isinstance(item, Mapping) and item.get("status") == "failed")
    errors = sum(1 for item in attempts if isinstance(item, Mapping) and item.get("status") in {"error", "outcome_unknown"})
    skipped = sum(1 for item in attempts if isinstance(item, Mapping) and item.get("status") == "cancelled")
    suite = ET.Element(
        "testsuite",
        {
            "name": _xml_text(f"Argus ATES {run_id}"),
            "tests": str(tests),
            "failures": str(failures),
            "errors": str(errors),
            "skipped": str(skipped),
        },
    )
    props = ET.SubElement(suite, "properties")
    ET.SubElement(props, "property", {"name": "ates.evidence_trust", "value": _xml_text(model.get("evidence_trust_state"))})
    ET.SubElement(props, "property", {"name": "ates.report_trust", "value": _xml_text(model.get("report_trust_state"))})
    ET.SubElement(props, "property", {"name": "ates.run_status", "value": _xml_text(status)})

    if attempts:
        for item in attempts:
            if not isinstance(item, Mapping):
                continue
            case = ET.SubElement(
                suite,
                "testcase",
                {
                    "classname": "argus.ates",
                    "name": _xml_text(item.get("step_id", "unknown-step")),
                },
            )
            attempt_status = item.get("status")
            if attempt_status == "failed":
                node = ET.SubElement(case, "failure", {"message": "deterministic step failure"})
                node.text = _xml_text(json.dumps(to_json_compatible(item), ensure_ascii=False, sort_keys=True))
            elif attempt_status in {"error", "outcome_unknown"}:
                node = ET.SubElement(case, "error", {"message": "step outcome is unreliable"})
                node.text = _xml_text(json.dumps(to_json_compatible(item), ensure_ascii=False, sort_keys=True))
            elif attempt_status == "cancelled":
                ET.SubElement(case, "skipped", {"message": "step cancelled"})
    else:
        case = ET.SubElement(suite, "testcase", {"classname": "argus.ates", "name": _xml_text(run_id)})
        if status == RunStatus.FAILED.value:
            ET.SubElement(case, "failure", {"message": "run failed"})
        elif status == RunStatus.ERROR.value:
            ET.SubElement(case, "error", {"message": "run errored"})
        elif status == RunStatus.CANCELLED.value:
            ET.SubElement(case, "skipped", {"message": "run cancelled"})

    raw = ET.tostring(suite, encoding="utf-8", xml_declaration=True, short_empty_elements=True)
    return raw + b"\n"


def _render_bytes(model: Mapping[str, object]) -> dict[str, bytes]:
    return {
        "report.json": _canonical_json(model, pretty=True),
        "report.md": _render_markdown(model),
        "report.html": _render_html(model),
        "junit.xml": _render_junit(model),
    }


def _replace_member(directory: _PinnedDirectory, name: str, data: bytes) -> Path:
    temp_name = f".{name}.argus-{uuid.uuid4().hex}.part"
    handle = None
    try:
        handle, created = _open_regular_file(directory, temp_name)
        if not created:
            raise ReportError("report temporary filename unexpectedly exists")
        written = handle.write(data)
        if written != len(data):
            raise ReportError(f"short report write for {name}")
        handle.flush()
        os.fsync(handle.fileno())
        directory.assert_file_identity(temp_name, handle.fileno(), f"report temporary member {name}")
        handle.close()
        handle = None
        if os.name == "nt":
            os.replace(directory.path / temp_name, directory.path / name)
        else:
            if directory._fd is None:
                raise ReportError("pinned reports directory has no descriptor")
            os.replace(
                temp_name,
                name,
                src_dir_fd=directory._fd,
                dst_dir_fd=directory._fd,
            )
        directory.fsync()
        actual = _pinned_bytes(directory.path, name, f"rendered report {name}")
        if actual != data:
            raise ReportError(f"rendered report {name} changed during publication")
        return directory.path / name
    except ReportError:
        raise
    except (OSError, AtesStoreError) as exc:
        raise ReportError(f"cannot publish report member {name} safely") from exc
    finally:
        if handle is not None:
            try:
                handle.close()
            except BaseException:
                pass
        try:
            if os.name == "nt":
                (directory.path / temp_name).unlink(missing_ok=True)
            elif directory._fd is not None:
                try:
                    os.unlink(temp_name, dir_fd=directory._fd)
                except FileNotFoundError:
                    pass
        except BaseException:
            pass


def _report_manifest(root: Path, members: Mapping[str, bytes]) -> dict[str, object]:
    source_manifest = _pinned_bytes(
        root / "manifests",
        "manifest-0001.json",
        "source evidence manifest",
    )
    return {
        "report_manifest_version": REPORT_MANIFEST_VERSION,
        "renderer": {"id": REPORT_RENDERER_ID},
        "source_evidence_manifest": {
            "path": "manifests/manifest-0001.json",
            "sha256": "sha256:" + hashlib.sha256(source_manifest).hexdigest(),
        },
        "members": [
            {
                "path": "reports/" + name,
                "size_bytes": len(data),
                "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
            }
            for name, data in sorted(members.items())
        ],
        "trust_note": "This local manifest is not an independent trust boundary unless its digest is supplied/verified externally.",
    }


def render_reports(
    run_dir: Path | str,
    *,
    approval_key_resolver: Optional[KeyResolver] = None,
) -> ReportBundle:
    """Regenerate all standard v0.1 reports from BOUND_VERIFIED evidence."""
    root = _run_root(run_dir)
    model = _report_model(root, approval_key_resolver=approval_key_resolver)
    members = _render_bytes(model)
    run_pin = reports_pin = None
    try:
        run_pin = _PinnedDirectory(root)
        reports_pin = run_pin.ensure_child("reports", "ATES reports directory")
        run_pin.assert_child_identity("reports", reports_pin, "ATES reports directory")
        paths = {name: _replace_member(reports_pin, name, data) for name, data in members.items()}
        manifest = _report_manifest(root, members)
        manifest_path = _replace_member(
            reports_pin,
            "report-manifest-0001.json",
            _canonical_json(manifest, pretty=False),
        )
        run_pin.assert_child_identity("reports", reports_pin, "ATES reports directory")
    except (OSError, AtesStoreError) as exc:
        raise ReportError("cannot establish authoritative reports directory") from exc
    finally:
        if reports_pin is not None:
            try:
                reports_pin.close()
            except BaseException:
                pass
        if run_pin is not None:
            try:
                run_pin.close()
            except BaseException:
                pass

    verified = verify_report_bundle(root, approval_key_resolver=approval_key_resolver)
    if verified.trust_state is not FinalizationTrustState.REGENERATED_VERIFIED:
        raise ReportError(verified.error or "regenerated report bundle did not verify")
    return ReportBundle(
        root,
        root / "reports",
        paths["report.json"],
        paths["report.md"],
        paths["report.html"],
        paths["junit.xml"],
        manifest_path,
        FinalizationTrustState.REGENERATED_VERIFIED,
    )


def inspect_report_bundle(run_dir: Path | str) -> ReportVerificationResult:
    """Inspect existing outputs without granting semantic trust to their bytes."""
    root = _run_root(run_dir)
    report_dir = root / "reports"
    manifest = report_dir / "report-manifest-0001.json"
    if all((report_dir / name).is_file() for name in _REPORT_NAMES) and manifest.is_file():
        return ReportVerificationResult(
            FinalizationTrustState.UNVERIFIED_DERIVED,
            report_dir,
            manifest,
            "existing reports have not been regenerated or independently bound in this inspection",
        )
    return ReportVerificationResult(
        FinalizationTrustState.INVALID,
        report_dir,
        manifest if manifest.exists() else None,
        "report bundle is incomplete",
    )


def verify_report_bundle(
    run_dir: Path | str,
    *,
    trusted_report_manifest_digest: Optional[str] = None,
    approval_key_resolver: Optional[KeyResolver] = None,
) -> ReportVerificationResult:
    """Regenerate semantic bytes and optionally verify an external manifest digest.

    Without an independently supplied report-manifest digest, exact regeneration
    yields ``REGENERATED_VERIFIED``.  When the supplied external digest also
    matches the report manifest, the exact existing report bytes are
    ``BOUND_VERIFIED``.  Verification failures return ``INVALID`` rather than
    silently falling back to an unverified label.
    """
    try:
        root = _run_root(run_dir)
        verify_finalized_run(root)
        report_dir = root / "reports"
        manifest_path = report_dir / "report-manifest-0001.json"
        manifest_raw = _pinned_bytes(report_dir, manifest_path.name, "report manifest")
        manifest = _strict_object(manifest_raw, "report manifest")
        if manifest.get("report_manifest_version") != REPORT_MANIFEST_VERSION:
            raise ReportError("unsupported report manifest version")
        renderer = manifest.get("renderer")
        if not isinstance(renderer, Mapping) or renderer.get("id") != REPORT_RENDERER_ID:
            raise ReportError("report manifest renderer identity is unsupported")

        source = manifest.get("source_evidence_manifest")
        if not isinstance(source, Mapping) or source.get("path") != "manifests/manifest-0001.json":
            raise ReportError("report manifest source binding is malformed")
        source_raw = _pinned_bytes(root / "manifests", "manifest-0001.json", "source evidence manifest")
        source_digest = "sha256:" + hashlib.sha256(source_raw).hexdigest()
        if not isinstance(source.get("sha256"), str) or not hmac.compare_digest(source_digest, source["sha256"]):
            raise ReportError("report manifest points at another evidence snapshot")

        listed = manifest.get("members")
        if isinstance(listed, (str, bytes, bytearray, Mapping)) or not isinstance(listed, Sequence):
            raise ReportError("report manifest members are malformed")
        by_path: dict[str, Mapping[str, object]] = {}
        for member in tuple(listed):
            if not isinstance(member, Mapping) or not isinstance(member.get("path"), str):
                raise ReportError("report manifest member is malformed")
            path = member["path"]
            if path in by_path:
                raise ReportError("report manifest has duplicate member paths")
            by_path[path] = member

        actual_members: dict[str, bytes] = {}
        for name in _REPORT_NAMES:
            path = "reports/" + name
            meta = by_path.get(path)
            if meta is None:
                raise ReportError(f"report manifest is missing {path}")
            data = _pinned_bytes(report_dir, name, f"rendered report {name}")
            expected_size = meta.get("size_bytes")
            expected_digest = meta.get("sha256")
            if isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size != len(data):
                raise ReportError(f"report size binding failed for {name}")
            actual_digest = "sha256:" + hashlib.sha256(data).hexdigest()
            if not isinstance(expected_digest, str) or not hmac.compare_digest(actual_digest, expected_digest):
                raise ReportError(f"report digest binding failed for {name}")
            actual_members[name] = data

        model = _report_model(root, approval_key_resolver=approval_key_resolver)
        expected_members = _render_bytes(model)
        for name in _REPORT_NAMES:
            if actual_members[name] != expected_members[name]:
                raise ReportError(f"report regeneration differs for {name}")
        expected_manifest = _canonical_json(_report_manifest(root, expected_members), pretty=False)
        if manifest_raw != expected_manifest:
            raise ReportError("report manifest differs from regenerated canonical manifest")

        if trusted_report_manifest_digest is not None:
            if not isinstance(trusted_report_manifest_digest, str) or not trusted_report_manifest_digest.startswith("sha256:"):
                raise ReportError("trusted report-manifest digest must be a sha256: value")
            actual = "sha256:" + hashlib.sha256(manifest_raw).hexdigest()
            if not hmac.compare_digest(actual, trusted_report_manifest_digest):
                raise ReportError("external report-manifest binding does not verify")
            state = FinalizationTrustState.BOUND_VERIFIED
        else:
            state = FinalizationTrustState.REGENERATED_VERIFIED
        return ReportVerificationResult(state, report_dir, manifest_path)
    except (ReportError, FinalizationError, OSError, ValueError) as exc:
        try:
            root = _run_root(run_dir)
            report_dir = root / "reports"
        except ReportError:
            report_dir = Path(run_dir) / "reports"
        return ReportVerificationResult(
            FinalizationTrustState.INVALID,
            report_dir,
            report_dir / "report-manifest-0001.json",
            str(exc),
        )


__all__ = [
    "REPORT_MANIFEST_VERSION",
    "REPORT_RENDERER_ID",
    "REPORT_VERSION",
    "FinalizationTrustInspection",
    "ReportBundle",
    "ReportError",
    "ReportVerificationResult",
    "inspect_finalization_trust",
    "inspect_report_bundle",
    "render_reports",
    "verify_report_bundle",
]
