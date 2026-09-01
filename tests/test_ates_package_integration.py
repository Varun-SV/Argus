"""ATES package integration regression coverage."""
from __future__ import annotations

import inspect
import json
from dataclasses import replace

import pytest

from argus.ates import (
    FinalizationTrustState,
    PackageCompletionError,
    RunStatus,
    append_audit_event,
    complete_run_package,
    ensure_detached_ledgers,
    validate_audit_chain,
    verify_finalized_run,
    verify_report_bundle,
)
from argus.engine.roam import roam
from argus.engine.runner import run_test
from argus.engine.spec import parse_spec
from argus.tokens import Budget
from tests.ates_test_support import (
    _finalize_and_recover,
    _finalize_without_package,
    _run_json_dirs,
)
from tests.conftest import FakeAdapter, FakeProvider
from tests.test_ates_finalization import _open_run


def test_finalizing_shims_preserve_public_callable_signatures():
    for public_callable in (run_test, roam):
        original = getattr(public_callable, "__wrapped__", None)
        assert original is not None
        assert inspect.signature(public_callable) == inspect.signature(original)
        assert tuple(inspect.signature(public_callable).parameters) != ("args", "kwargs")


def test_run_test_publishes_bound_revision_one_and_authoritative_status(tmp_path):
    spec = parse_spec(
        """\
name: finalization integration
target: {adapter: desktop-gui, launch: fake.exe}
steps:
  - "finish"
"""
    )
    provider = FakeProvider([json.dumps({"action": "done", "success": True})])
    result = run_test(spec, provider, FakeAdapter(), project_dir=tmp_path)
    assert result.status == "pass"
    assert result.ates_effective_status == "passed"
    bindings = _run_json_dirs(tmp_path)
    assert len(bindings) == 1
    verified = verify_finalized_run(bindings[0].parent)
    assert verified.outcome.effective_status.value == "passed"


def test_roam_publishes_bound_revision_one_and_authoritative_status(tmp_path):
    session_dir = tmp_path / ".argus" / "roam-review"
    session_dir.mkdir(parents=True)
    session = roam(
        target="fake.exe",
        provider=FakeProvider([]),
        adapter=FakeAdapter(),
        budget=Budget(max_tokens=1000),
        session_dir=session_dir,
        project_dir=tmp_path,
        stop_flag=lambda: True,
        generate_regressions=False,
    )
    assert session.execution_status == "cancelled"
    assert session.ates_effective_status == "cancelled"
    bindings = _run_json_dirs(tmp_path)
    assert len(bindings) == 1
    verified = verify_finalized_run(bindings[0].parent)
    assert verified.outcome.effective_status.value == "cancelled"


def test_run_test_preserves_positional_project_dir(tmp_path):
    spec = parse_spec(
        """\
name: positional runner project root
target: {adapter: desktop-gui, launch: fake.exe}
steps:
  - "finish"
"""
    )
    provider = FakeProvider([json.dumps({"action": "done", "success": True})])
    result = run_test(
        spec,
        provider,
        FakeAdapter(),
        None,
        None,
        None,
        None,
        None,
        tmp_path,
    )
    assert result.status == "pass"
    assert result.ates_effective_status == "passed"
    assert list((tmp_path / ".argus" / "runs").glob("RUN-*/run.json"))


def test_roam_preserves_positional_project_dir(tmp_path):
    session_dir = tmp_path / ".argus" / "roam-positional"
    session_dir.mkdir(parents=True)
    session = roam(
        "fake.exe",
        FakeProvider([]),
        FakeAdapter(),
        Budget(max_tokens=1000),
        session_dir,
        None,
        lambda: True,
        False,
        None,
        None,
        tmp_path,
    )
    assert session.execution_status == "cancelled"
    assert session.ates_effective_status == "cancelled"
    assert list((tmp_path / ".argus" / "runs").glob("RUN-*/run.json"))


def test_finalization_audit_dedupe_collision_fails_package_completion(tmp_path):
    finalization = _finalize_without_package(_open_run(tmp_path))
    root = finalization.run_dir
    ensure_detached_ledgers(root)
    dedupe_key = f"finalization:{finalization.outcome.finalization_id}"

    poisoned = append_audit_event(
        root,
        "unrelated.event",
        actor="unrelated-actor",
        dedupe_key=dedupe_key,
        occurred_at=finalization.outcome.finalized_at,
        details={"purpose": "occupy another operation's dedupe key"},
    )

    with pytest.raises(PackageCompletionError, match="audit dedupe conflict"):
        complete_run_package(finalization)

    records = validate_audit_chain(root)
    same_key = [record for record in records if record.get("dedupe_key") == dedupe_key]
    assert same_key == [poisoned]
    assert same_key[0]["event_type"] == "unrelated.event"
    assert not any(
        record.get("event_type") == "finalization.bound"
        and record.get("dedupe_key") == dedupe_key
        for record in records
    )
    assert not (root / "reports" / "report.json").exists()


def test_package_completion_returns_authoritative_verified_finalization(tmp_path):
    finalization = _finalize_without_package(_open_run(tmp_path))
    forged = replace(
        finalization,
        outcome=replace(
            finalization.outcome,
            effective_status=RunStatus.ERROR,
        ),
    )

    completed = complete_run_package(forged)
    authoritative = verify_finalized_run(finalization.run_dir)

    assert completed.finalization == authoritative
    assert completed.finalization.outcome.effective_status is RunStatus.PASSED


def test_closed_run_recovery_materializes_complete_pr22_package(tmp_path):
    result = _finalize_and_recover(_open_run(tmp_path), tmp_path)
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
    assert report["report_trust_state"] == "unverified_derived"
    assert verify_report_bundle(root).trust_state is FinalizationTrustState.REGENERATED_VERIFIED
    assert report["outcome"]["effective_status"] == "passed"
    assert report["renderer"]["active_artifact_links"] is False

    audit = validate_audit_chain(root)
    assert any(item["event_type"] == "finalization.bound" for item in audit)
