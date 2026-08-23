from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET

from argus.ates import (
    ApprovalAction,
    ApprovalCredential,
    AssertionId,
    AssertionRecord,
    AtesEventStore,
    EventType,
    EvidenceValue,
    FinalizationTrustState,
    RequirementIdentity,
    RunId,
    StepAttemptId,
    StepId,
    StepRecord,
    append_approval,
    finalize_revision_one,
    inspect_finalization_trust,
    inspect_report_bundle,
    recover_revision_one,
    revoke_approval,
    to_json_compatible,
    validate_approvals,
    validate_audit_chain,
    verify_report_bundle,
)
from tests.test_ates_finalization import _open_run, _run_record_json


_STARTED = "2026-08-23T10:00:00+00:00"
_ENDED = "2026-08-23T10:00:01+00:00"


def _attempt(step_id, attempt_id, status, *, ended):
    return {
        "attempt": {
            "step_attempt_id": str(attempt_id),
            "step_id": str(step_id),
            "attempt": 1,
            "status": status,
            "started_at": _STARTED,
            "ended_at": _ENDED if ended else None,
            "retry_reason": None,
        }
    }


def _finalize_closed(store: AtesEventStore, tmp_path):
    run_id = store.run_id
    try:
        finalize_revision_one(store)
    finally:
        store.close()
    return recover_revision_one(tmp_path, run_id)


def _custom_run(tmp_path, *, instruction: str, requirements=()):
    run_id = RunId.new()
    step_id = StepId.new()
    attempt_id = StepAttemptId.new()
    step = StepRecord(
        step_id=step_id,
        instruction=EvidenceValue.safe(instruction),
        kind="assert" if requirements else "act",
    )
    store = AtesEventStore(tmp_path, run_id)
    store.append(
        EventType.RUN_STARTED,
        {"run": _run_record_json(run_id), "steps": [to_json_compatible(step)]},
    )
    store.append(EventType.ENVIRONMENT_PREPARED, {"environment_type": "direct", "isolated": False})
    store.append(
        EventType.TARGET_LAUNCHED,
        {"target": to_json_compatible(EvidenceValue.redacted("privacy.target_value"))},
    )
    store.append(EventType.STEP_ATTEMPT_STARTED, _attempt(step_id, attempt_id, "running", ended=False))
    for revision in requirements:
        requirement = RequirementIdentity(
            requirement_id="REQ-shared-display-id",
            source_system="test-suite",
            source_revision=revision,
        )
        assertion = AssertionRecord(
            assertion_id=AssertionId.new(),
            step_id=step_id,
            step_attempt_id=attempt_id,
            kind="equals",
            expected=EvidenceValue.safe("expected"),
            actual=EvidenceValue.safe("expected"),
            result="passed",
            method="deterministic",
            required=True,
            requirement=requirement,
        )
        store.append(EventType.ASSERTION_EVALUATED, {"assertion": to_json_compatible(assertion)})
    store.append(EventType.STEP_ATTEMPT_COMPLETED, _attempt(step_id, attempt_id, "passed", ended=True))
    store.append(EventType.TARGET_CLOSED, {})
    store.append(EventType.ENVIRONMENT_RELEASED, {})
    store.append(
        EventType.RUN_MARKED_INCOMPLETE,
        {"reason": "runtime.finalization_pending", "execution_result": "pass"},
    )
    return store


def test_closed_run_recovery_materializes_complete_pr22_package(tmp_path):
    result = _finalize_closed(_open_run(tmp_path), tmp_path)
    root = result.run_dir

    assert (root / "evidence.jsonl").is_file()
    assert (root / "run.json").is_file()
    assert (root / "manifests" / "manifest-0001.json").is_file()
    assert (root / "manifests" / "package-manifest-0001.json").is_file()
    assert (root / "approvals.jsonl").is_file()
    assert (root / "audit.jsonl").is_file()
    for name in ("report.json", "report.md", "report.html", "junit.xml"):
        assert (root / "reports" / name).is_file()
    assert (root / "reports" / "report-manifest-0001.json").is_file()

    report = json.loads((root / "reports" / "report.json").read_text("utf-8"))
    assert report["evidence_trust_state"] == "bound_verified"
    assert report["report_trust_state"] == "regenerated_verified"
    assert report["outcome"]["effective_status"] == "passed"
    assert report["renderer"]["active_artifact_links"] is False

    audit = validate_audit_chain(root)
    assert any(item["event_type"] == "finalization.bound" for item in audit)


def test_reports_escape_untrusted_text_and_never_create_active_artifact_links(tmp_path):
    malicious = '<script>alert(1)</script> [click](javascript:alert(1)) <img src=x onerror=alert(1)>'
    result = _finalize_closed(_custom_run(tmp_path, instruction=malicious), tmp_path)
    root = result.run_dir

    html_report = (root / "reports" / "report.html").read_text("utf-8")
    assert "<script>alert(1)</script>" not in html_report
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_report
    assert 'href="javascript:' not in html_report.lower()
    assert '<img src="x"' not in html_report.lower()
    assert "default-src 'none'" in html_report

    markdown = (root / "reports" / "report.md").read_text("utf-8")
    malicious_lines = [line for line in markdown.splitlines() if "<script>" in line or "javascript:" in line]
    assert malicious_lines
    assert all(line.startswith("    ") for line in malicious_lines)

    ET.parse(root / "reports" / "junit.xml")


def test_requirement_display_id_revisions_remain_distinct_in_traceability(tmp_path):
    result = _finalize_closed(
        _custom_run(
            tmp_path,
            instruction="verify versioned requirements",
            requirements=("rev-1", "rev-2"),
        ),
        tmp_path,
    )
    report = json.loads((result.run_dir / "reports" / "report.json").read_text("utf-8"))
    trace = report["traceability"]
    assert len(trace) == 2
    assert {item["requirement_identity"]["requirement_id"] for item in trace} == {"REQ-shared-display-id"}
    assert {item["requirement_identity"]["source_revision"] for item in trace} == {"rev-1", "rev-2"}
    assert len({item["requirement_identity_digest"] for item in trace}) == 2
    assert all(item["run_id"] == str(result.outcome.run_id) for item in trace)
    assert all(item["step_id"] and item["step_attempt_id"] and item["assertion_id"] for item in trace)


def test_authenticated_approval_supersession_and_revocation_are_append_only(tmp_path):
    result = _finalize_closed(_open_run(tmp_path), tmp_path)
    root = result.run_dir
    key = b"0123456789abcdef0123456789abcdef"
    credential = ApprovalCredential(
        key_id="reviewer-key-1",
        key=key,
        actor="reviewer@example.invalid",
        roles=("test_reviewer",),
    )
    resolver = lambda key_id: credential if key_id == credential.key_id else None

    first = append_approval(
        root,
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
    )
    ledger = root / "approvals.jsonl"
    first_bytes = ledger.read_bytes()
    state = validate_approvals(root, key_resolver=resolver)
    assert [item.record["approval_id"] for item in state.verified_approvals] == [first["approval_id"]]

    second = append_approval(
        root,
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
        supersedes_approval_id=first["approval_id"],
    )
    assert ledger.read_bytes().startswith(first_bytes)
    state = validate_approvals(root, key_resolver=resolver)
    assert [item.record["approval_id"] for item in state.verified_approvals] == [second["approval_id"]]

    before_revoke = ledger.read_bytes()
    revoke_approval(
        root,
        second["approval_id"],
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
    )
    assert ledger.read_bytes().startswith(before_revoke)
    state = validate_approvals(root, key_resolver=resolver)
    assert state.verified_approvals == ()
    assert len(state.records) == 3

    without_trust = validate_approvals(root)
    assert without_trust.verified_approvals == ()
    assert all(item.verification_status.value == "unverified" for item in without_trust.records)

    wrong_credential = ApprovalCredential(
        key_id="reviewer-key-1",
        key=b"fedcba9876543210fedcba9876543210",
        actor="reviewer@example.invalid",
        roles=("test_reviewer",),
    )
    invalid = validate_approvals(root, key_resolver=lambda _key_id: wrong_credential)
    assert invalid.verified_approvals == ()
    assert all(item.verification_status.value == "invalid" for item in invalid.records)

    audit = validate_audit_chain(root)
    assert sum(item["event_type"] == "approval.changed" for item in audit) == 3


def test_report_and_evidence_trust_states_have_real_consumer_paths(tmp_path):
    result = _finalize_closed(_open_run(tmp_path), tmp_path)
    root = result.run_dir

    assert inspect_finalization_trust(root).trust_state is FinalizationTrustState.BOUND_VERIFIED
    assert inspect_report_bundle(root).trust_state is FinalizationTrustState.UNVERIFIED_DERIVED
    assert verify_report_bundle(root).trust_state is FinalizationTrustState.REGENERATED_VERIFIED

    manifest = (root / "reports" / "report-manifest-0001.json").read_bytes()
    trusted = "sha256:" + hashlib.sha256(manifest).hexdigest()
    assert verify_report_bundle(
        root,
        trusted_report_manifest_digest=trusted,
    ).trust_state is FinalizationTrustState.BOUND_VERIFIED

    report_md = root / "reports" / "report.md"
    report_md.write_bytes(report_md.read_bytes() + b"tamper\n")
    assert verify_report_bundle(root).trust_state is FinalizationTrustState.INVALID


def test_approval_validation_does_not_create_missing_ledger(tmp_path):
    result = _finalize_closed(_open_run(tmp_path), tmp_path)
    approvals = result.run_dir / "approvals.jsonl"
    approvals.unlink()
    assert not approvals.exists()
    validated = validate_approvals(result.run_dir)
    assert validated.records == ()
    assert not approvals.exists()
