from __future__ import annotations

import json
from pathlib import Path

import pytest

import argus.ates.reports_runtime as reports_runtime
from argus.ates import (
    EventType,
    EvidenceValue,
    FinalizationError,
    FinalizationTrustState,
    FindingId,
    FindingRecord,
    ReportError,
    ReportVerificationResult,
    RunStatus,
    finalize_revision_one,
    render_reports,
    to_json_compatible,
)
from tests.test_ates_finalization import _open_run


_REPORT_FILES = (
    "report.json",
    "report.md",
    "report.html",
    "junit.xml",
    "report-manifest-0001.json",
)


def _append_pending(store) -> None:
    store.append(
        EventType.RUN_MARKED_INCOMPLETE,
        {"reason": "runtime.finalization_pending", "execution_result": "pass"},
    )


def _finalized_root(tmp_path: Path) -> Path:
    store = _open_run(tmp_path)
    try:
        result = finalize_revision_one(store)
    finally:
        store.close()
    return result.run_dir


def _bundle_bytes(root: Path) -> dict[str, bytes]:
    report_dir = root / "reports"
    return {name: (report_dir / name).read_bytes() for name in _REPORT_FILES}


def _assert_no_transaction_residue(root: Path) -> None:
    names = [item.name for item in (root / "reports").iterdir()]
    assert not any(".stage-" in name for name in names)
    assert not any(".backup-" in name for name in names)
    assert not any(".failed-" in name for name in names)


def test_report_staging_failure_preserves_previous_bundle(tmp_path, monkeypatch):
    root = _finalized_root(tmp_path)
    render_reports(root)
    before = _bundle_bytes(root)

    real_write = reports_runtime._write

    def fail_late_stage(directory, name, data):
        if name.startswith(".report.html.stage-"):
            raise ReportError("forced late staged-report failure")
        return real_write(directory, name, data)

    monkeypatch.setattr(reports_runtime, "_write", fail_late_stage)

    with pytest.raises(ReportError, match="forced late staged-report failure"):
        render_reports(root)

    assert _bundle_bytes(root) == before
    _assert_no_transaction_residue(root)


def test_postpublication_verification_failure_rolls_back_previous_bundle(
    tmp_path, monkeypatch
):
    root = _finalized_root(tmp_path)
    render_reports(root)
    before = _bundle_bytes(root)

    # Force the second generation to differ from the old one so byte equality
    # proves rollback, not merely that both renders happened to be identical.
    real_model = reports_runtime._model

    def changed_model(run_root, resolver):
        model = dict(real_model(run_root, resolver))
        model["round11_transaction_marker"] = "new-generation"
        return model

    monkeypatch.setattr(reports_runtime, "_model", changed_model)

    def fail_verification(run_dir, **kwargs):
        report_dir = Path(run_dir) / "reports"
        return ReportVerificationResult(
            FinalizationTrustState.INVALID,
            report_dir,
            report_dir / "report-manifest-0001.json",
            "forced post-publication verification failure",
        )

    monkeypatch.setattr(reports_runtime, "verify_report_bundle", fail_verification)

    with pytest.raises(ReportError, match="forced post-publication verification failure"):
        render_reports(root)

    assert _bundle_bytes(root) == before
    _assert_no_transaction_residue(root)


def test_malformed_sequence_tombstone_cannot_finalize_passed(tmp_path):
    store = _open_run(tmp_path, provisional=False)
    store.append(EventType.SEQUENCE_TOMBSTONE, {})
    _append_pending(store)
    try:
        with pytest.raises(FinalizationError, match="SEQUENCE_TOMBSTONE.*reason"):
            finalize_revision_one(store)
    finally:
        store.close()


def test_reasoned_sequence_tombstone_remains_explicit_and_finalizable(tmp_path):
    store = _open_run(tmp_path, provisional=False)
    store.append(
        EventType.SEQUENCE_TOMBSTONE,
        {"reason": "producer intentionally omitted non-core diagnostic detail"},
    )
    _append_pending(store)
    try:
        result = finalize_revision_one(store)
        assert result.outcome.effective_status is RunStatus.PASSED
    finally:
        store.close()


def test_malformed_finding_is_rejected_before_finalization(tmp_path):
    store = _open_run(tmp_path, provisional=False)
    store.append(EventType.FINDING_RECORDED, {})
    _append_pending(store)
    try:
        with pytest.raises(FinalizationError, match="FINDING_RECORDED finding"):
            finalize_revision_one(store)
    finally:
        store.close()


def test_valid_finding_survives_into_verified_report(tmp_path):
    store = _open_run(tmp_path, provisional=False)
    finding = FindingRecord(
        finding_id=FindingId.new(),
        title=EvidenceValue.safe("visible finding"),
        description=EvidenceValue.safe("canonical finding description"),
        evidence_refs=(),
        classification_source="model",
        classification="low",
    )
    store.append(
        EventType.FINDING_RECORDED,
        {"finding": to_json_compatible(finding)},
    )
    _append_pending(store)
    try:
        result = finalize_revision_one(store)
    finally:
        store.close()

    bundle = render_reports(result.run_dir)
    model = json.loads(bundle.json_path.read_text("utf-8"))
    assert [item["finding_id"] for item in model["findings"]] == [
        str(finding.finding_id)
    ]
