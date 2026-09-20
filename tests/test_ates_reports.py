"""ATES reports regression coverage."""
from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
from copy import deepcopy

import pytest

import argus.ates.audit as audit_module
import argus.ates.reports as report_module
from argus.ates import (
    EventType,
    FinalizationTrustState,
    append_approval,
    append_audit_event,
    finalize_revision_one,
    inspect_finalization_trust,
    inspect_report_bundle,
    render_reports,
    revoke_approval,
    validate_approvals,
    verify_report_bundle,
)
from tests.ates_test_support import (
    _approval_credential,
    _assert_report_is_self_described_as_derived,
    _canonical_lines,
    _custom_run,
    _finalize_and_complete,
    _finalize_and_recover,
    _finalized_package,
    _report_model,
    _run_with_two_assertions_and_artifacts,
)
from tests.test_ates_finalization import _open_run

def test_reports_render_read_only_when_detached_ledgers_are_absent(tmp_path):
    store = _open_run(tmp_path)
    try:
        result = finalize_revision_one(store)
    finally:
        store.close()
    root = result.run_dir

    assert not (root / "approvals.jsonl").exists()
    assert not (root / "audit.jsonl").exists()

    bundle = render_reports(root)
    model = json.loads(bundle.json_path.read_text("utf-8"))
    members = {
        item["path"]: item
        for item in model["detached_ledger_snapshot"]["members"]
    }
    assert members["approvals.jsonl"] == {
        "path": "approvals.jsonl",
        "state": "absent",
        "size_bytes": 0,
        "sha256": None,
    }
    assert members["audit.jsonl"] == {
        "path": "audit.jsonl",
        "state": "absent",
        "size_bytes": 0,
        "sha256": None,
    }
    # Rendering a verified run must not initialize detached mutable ledgers.
    assert not (root / "approvals.jsonl").exists()
    assert not (root / "audit.jsonl").exists()


def test_reports_omit_structurally_invalid_approval_payloads(tmp_path):
    root = _finalized_package(tmp_path).run_dir
    key, credential, resolver = _approval_credential()
    approval = append_approval(
        root,
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
    )
    approvals_path = root / "approvals.jsonl"
    approvals = [
        json.loads(line) for line in approvals_path.read_text("utf-8").splitlines()
    ]
    malformed = deepcopy(approvals[-1])
    malformed["reason"] = "review-secret-plaintext"
    malformed["debug_note"] = "review-private-extension"
    malformed["authentication"]["signature"] = audit_module._sign_record(
        malformed, key
    )
    approvals[-1] = malformed
    approvals_path.write_bytes(_canonical_lines(approvals))

    audit_path = root / "audit.jsonl"
    audits = [json.loads(line) for line in audit_path.read_text("utf-8").splitlines()]
    matching = [
        row
        for row in audits
        if isinstance(row.get("details"), dict)
        and row["details"].get("approval_id") == approval["approval_id"]
    ]
    assert len(matching) == 1
    matching[0]["details"]["approval_record_digest"] = audit_module._approval_digest(
        malformed
    )
    audit_path.write_bytes(_canonical_lines(audits))
    assert validate_approvals(root, key_resolver=resolver).records[-1].effective is False

    bundle = render_reports(root, approval_key_resolver=resolver)
    model = json.loads(bundle.json_path.read_text("utf-8"))
    reported = model["approvals"]["records"][-1]

    assert reported["verification_status"] == "invalid"
    assert reported["effective"] is False
    assert reported["record_state"] == "omitted_invalid"
    assert reported["record_digest"].startswith("sha256:")
    assert "record" not in reported
    for path in (
        bundle.json_path,
        bundle.markdown_path,
        bundle.html_path,
        bundle.junit_path,
    ):
        rendered = path.read_text("utf-8")
        assert "review-secret-plaintext" not in rendered
        assert "review-private-extension" not in rendered
    assert (
        verify_report_bundle(root, approval_key_resolver=resolver).trust_state
        is FinalizationTrustState.REGENERATED_VERIFIED
    )


def test_externally_bound_report_remains_valid_after_detached_audit_update(tmp_path):
    finalization = _finalized_package(tmp_path)
    root = finalization.run_dir
    bundle = render_reports(root)
    manifest_bytes = bundle.manifest_path.read_bytes()
    trusted_digest = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()

    append_audit_event(
        root,
        "review.note",
        actor="round12-reviewer",
        details={"note": "post-report append-only audit activity"},
    )

    stale = verify_report_bundle(root)
    assert stale.trust_state is FinalizationTrustState.INVALID

    historical = verify_report_bundle(
        root,
        trusted_report_manifest_digest=trusted_digest,
    )
    assert historical.trust_state is FinalizationTrustState.BOUND_VERIFIED


def test_externally_bound_report_rejects_extra_manifest_members(tmp_path):
    root = _finalized_package(tmp_path).run_dir
    bundle = render_reports(root)
    manifest = json.loads(bundle.manifest_path.read_text("utf-8"))
    manifest["members"].append(
        {
            "path": "../unverified-private.txt",
            "size_bytes": 0,
            "sha256": "sha256:" + hashlib.sha256(b"").hexdigest(),
        }
    )
    manifest_raw = report_module._json(manifest)
    bundle.manifest_path.write_bytes(manifest_raw)
    trusted_digest = "sha256:" + hashlib.sha256(manifest_raw).hexdigest()

    verified = verify_report_bundle(
        root,
        trusted_report_manifest_digest=trusted_digest,
    )

    assert verified.trust_state is FinalizationTrustState.INVALID
    assert verified.error is not None
    assert "member set is not canonical" in verified.error


@pytest.mark.parametrize(
    ("execution_result", "expected_status"),
    [
        ("fail", "failed"),
        ("error", "error"),
        ("cancelled", "cancelled"),
        ("outcome_unknown", "error"),
    ],
)
def test_reports_include_status_driving_provisional_markers(
    tmp_path,
    execution_result,
    expected_status,
):
    store = _open_run(tmp_path, provisional=False)
    store.append(
        EventType.RUN_MARKED_INCOMPLETE,
        {
            "reason": "runtime.finalization_pending",
            "execution_result": execution_result,
        },
    )
    result = _finalize_and_recover(store, tmp_path)
    model = json.loads((result.run_dir / "reports" / "report.json").read_text("utf-8"))

    assert model["outcome"]["effective_status"] == expected_status
    failures = model["failures_and_ambiguities"]
    assert len(failures) == 1
    assert failures[0]["type"] == "run_incomplete"
    assert failures[0]["reason"] == "runtime.finalization_pending"
    assert failures[0]["execution_result"] == execution_result
    assert isinstance(failures[0]["sequence"], int)


def test_traceability_does_not_fabricate_attempt_level_artifact_links(tmp_path):
    result = _finalize_and_recover(_run_with_two_assertions_and_artifacts(tmp_path), tmp_path)
    report = json.loads(
        (result.run_dir / "reports" / "report.json").read_text("utf-8")
    )
    assert len(report["traceability"]) == 2
    assert len(report["artifacts"]) == 2
    assert all(item["artifact_ids"] == [] for item in report["traceability"])
    assert all(
        item["artifact_binding_state"]
        == "unbound_no_explicit_assertion_relation"
        for item in report["traceability"]
    )


@pytest.mark.parametrize(
    ("status", "attempts", "expected_failures", "expected_errors"),
    [
        ("failed", [{"step_id": "STEP-a", "status": "passed"}], 1, 0),
        (
            "passed",
            [
                {"step_id": "STEP-a", "status": "failed"},
                {"step_id": "STEP-a", "status": "passed"},
            ],
            0,
            0,
        ),
    ],
)
def test_junit_never_contradicts_canonical_outcome(
    status, attempts, expected_failures, expected_errors
):
    raw = report_module._junit(
        {
            "source": {"run_id": "RUN-round6"},
            "outcome": {"effective_status": status},
            "attempts": attempts,
            "evidence_trust_state": "bound_verified",
            "report_trust_state": "regenerated_verified",
        }
    )
    suite = ET.fromstring(raw)
    assert suite.attrib["tests"] == "1"
    assert int(suite.attrib["failures"]) == expected_failures
    assert int(suite.attrib["errors"]) == expected_errors
    cases = suite.findall("testcase")
    assert len(cases) == 1
    if status == "failed":
        assert cases[0].find("failure") is not None
    else:
        assert cases[0].find("failure") is None
        assert cases[0].find("error") is None


def test_detached_ledger_mutations_never_leave_reports_self_attesting_freshness(tmp_path):
    _finalization, package = _finalize_and_complete(tmp_path)
    root = package.finalization.run_dir
    key, credential, resolver = _approval_credential()

    initial = _assert_report_is_self_described_as_derived(root)
    initial_bytes = (root / "reports" / "report.json").read_bytes()

    approval = append_approval(
        root,
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
    )
    # Detached-ledger commits do not rewrite a report behind the reader's back;
    # the existing file remains explicitly derived and exposes its old snapshot.
    assert (root / "reports" / "report.json").read_bytes() == initial_bytes
    stale_after_approval = _report_model(root)
    assert stale_after_approval["report_trust_state"] == FinalizationTrustState.UNVERIFIED_DERIVED.value
    assert stale_after_approval["detached_ledger_snapshot"] == initial["detached_ledger_snapshot"]
    assert verify_report_bundle(root, approval_key_resolver=resolver).trust_state is FinalizationTrustState.INVALID

    render_reports(root, approval_key_resolver=resolver)
    assert verify_report_bundle(root, approval_key_resolver=resolver).trust_state is FinalizationTrustState.REGENERATED_VERIFIED
    before_revoke = (root / "reports" / "report.json").read_bytes()
    _assert_report_is_self_described_as_derived(root)

    revoke_approval(
        root,
        approval["approval_id"],
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
    )
    assert (root / "reports" / "report.json").read_bytes() == before_revoke
    assert _report_model(root)["report_trust_state"] == FinalizationTrustState.UNVERIFIED_DERIVED.value
    assert verify_report_bundle(root, approval_key_resolver=resolver).trust_state is FinalizationTrustState.INVALID

    render_reports(root, approval_key_resolver=resolver)
    assert verify_report_bundle(root, approval_key_resolver=resolver).trust_state is FinalizationTrustState.REGENERATED_VERIFIED
    before_audit = (root / "reports" / "report.json").read_bytes()

    append_audit_event(
        root,
        "round8.detached-change",
        actor="round8-test",
        details={"reason": "prove report snapshot staleness is visible"},
        dedupe_key="round8:detached-change",
    )
    assert (root / "reports" / "report.json").read_bytes() == before_audit
    assert _report_model(root)["report_trust_state"] == FinalizationTrustState.UNVERIFIED_DERIVED.value
    assert verify_report_bundle(root, approval_key_resolver=resolver).trust_state is FinalizationTrustState.INVALID


def test_reports_escape_untrusted_text_and_never_create_active_artifact_links(tmp_path):
    malicious = '<script>alert(1)</script> [click](javascript:alert(1)) <img src=x onerror=alert(1)>'
    result = _finalize_and_recover(_custom_run(tmp_path, instruction=malicious), tmp_path)
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
    result = _finalize_and_recover(
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


def test_report_and_evidence_trust_states_have_real_consumer_paths(tmp_path):
    result = _finalize_and_recover(_open_run(tmp_path), tmp_path)
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
