"""Regression coverage for incomplete and ambiguous ATES report states."""
from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET

import pytest

import argus.ates.reports as report_module
from argus.ates import (
    ActionId,
    ActionOperationId,
    ActionRecord,
    EventType,
    FinalizationTrustState,
    RunStatus,
    finalize_revision_one,
    inspect_finalization_trust,
    render_reports,
    to_json_compatible,
    verify_report_bundle,
)
from tests.ates_test_support import _complete_action_store, _open_action_store
from tests.test_ates_finalization import _open_run


def test_dangling_run_binding_is_invalid_not_incomplete(tmp_path):
    store = _open_run(tmp_path)
    root = store.run_dir
    store.close()

    binding = root / "run.json"
    try:
        os.symlink("missing-run-binding.json", binding)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks are unavailable on this platform: {exc}")

    assert os.path.lexists(binding)
    assert not binding.exists()
    inspected = inspect_finalization_trust(root)
    assert inspected.trust_state is FinalizationTrustState.INVALID


def test_incomplete_run_renders_unverified_report_with_unknown_terminal_status(tmp_path):
    store = _open_run(tmp_path)
    root = store.run_dir
    store.close()

    assert not os.path.lexists(root / "run.json")
    bundle = render_reports(root)
    model = json.loads(bundle.json_path.read_text("utf-8"))

    assert bundle.trust_state is FinalizationTrustState.UNVERIFIED_DERIVED
    assert model["evidence_trust_state"] == FinalizationTrustState.UNVERIFIED_DERIVED.value
    assert model["outcome"]["lifecycle_state"] == "incomplete_recoverable"
    assert model["outcome"]["effective_status"] is None
    assert model["outcome"]["finalized_at"] is None
    assert model["source"]["finalization_id"] is None
    assert model["source"]["evidence_manifest_path"] is None
    assert model["source"]["evidence_sha256"].startswith("sha256:")

    verified = verify_report_bundle(root)
    assert verified.trust_state is FinalizationTrustState.UNVERIFIED_DERIVED

    suite = ET.parse(bundle.junit_path).getroot()
    properties = {
        item.attrib["name"]: item.attrib["value"]
        for item in suite.find("properties") or ()
    }
    assert properties["ates.run_status"] == ""
    assert properties["ates.run_terminal_status_known"] == "false"
    error = suite.find("testcase/error")
    assert error is not None
    assert "terminal status is unavailable" in error.attrib["message"]


@pytest.mark.parametrize(
    ("lifecycle", "expected_state"),
    [
        ((EventType.ACTION_PROPOSED,), "proposed"),
        (
            (EventType.ACTION_PROPOSED, EventType.ACTION_POLICY_VALIDATED),
            "policy_validated",
        ),
        (
            (
                EventType.ACTION_PROPOSED,
                EventType.ACTION_POLICY_VALIDATED,
                EventType.ACTION_DISPATCH_COMMITTED,
            ),
            "dispatch_committed",
        ),
    ],
)
def test_reports_surface_unterminated_action_lifecycles(
    tmp_path, lifecycle, expected_state
):
    store, step_id, attempt_id = _open_action_store(tmp_path)
    action = ActionRecord(
        action_id=ActionId.new(),
        step_id=step_id,
        step_attempt_id=attempt_id,
        action_type="click",
        parameters={},
        operation_id=ActionOperationId.new(),
    )
    payload = {"action": to_json_compatible(action)}
    for event_type in lifecycle:
        store.append(event_type, payload)
    _complete_action_store(store, step_id, attempt_id)

    try:
        result = finalize_revision_one(store)
    finally:
        store.close()

    assert result.outcome.effective_status is RunStatus.ERROR
    model = json.loads(render_reports(result.run_dir).json_path.read_text("utf-8"))
    failures = [
        item
        for item in model["failures_and_ambiguities"]
        if item.get("type") == "action_lifecycle_incomplete"
        and item.get("action_id") == str(action.action_id)
    ]
    assert len(failures) == 1
    assert failures[0]["state"] == expected_state
    assert isinstance(failures[0]["sequence"], int)


def test_incomplete_report_rejects_plaintext_payload_extension(tmp_path):
    store = _open_run(tmp_path)
    root = store.run_dir
    store.close()

    evidence = root / "evidence.jsonl"
    rows = [json.loads(line) for line in evidence.read_text("utf-8").splitlines()]
    rows[0]["payload"]["debug_note"] = "API_TOKEN=plaintext-secret"
    evidence.write_bytes(
        b"".join(
            json.dumps(
                row,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
            for row in rows
        )
    )

    inspected = inspect_finalization_trust(root)
    assert inspected.trust_state is FinalizationTrustState.INVALID
    assert "schema/privacy boundary" in (inspected.error or "")
    with pytest.raises(report_module.ReportError):
        render_reports(root)
