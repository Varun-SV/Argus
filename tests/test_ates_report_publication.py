"""ATES report publication regression coverage."""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

import argus.ates.reports as report_module
from argus.ates import (
    FinalizationTrustState,
    ReportError,
    ReportVerificationResult,
    append_approval,
    append_audit_event,
    render_reports,
    verify_report_bundle,
)
from tests.ates_test_support import (
    _approval_credential,
    _assert_no_transaction_residue,
    _bundle_bytes,
    _finalized_package,
    _finalized_root,
)

def test_report_staging_failure_preserves_previous_bundle(tmp_path, monkeypatch):
    root = _finalized_root(tmp_path)
    render_reports(root)
    before = _bundle_bytes(root)

    real_write = report_module._write

    def fail_late_stage(directory, name, data):
        if name.startswith(".report.html.stage-"):
            raise ReportError("forced late staged-report failure")
        return real_write(directory, name, data)

    monkeypatch.setattr(report_module, "_write", fail_late_stage)

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
    real_model = report_module._model

    def changed_model(run_root, resolver):
        model = dict(real_model(run_root, resolver))
        model["transaction_test_marker"] = "new-generation"
        return model

    monkeypatch.setattr(report_module, "_model", changed_model)

    def fail_verification(run_dir, **kwargs):
        report_dir = Path(run_dir) / "reports"
        return ReportVerificationResult(
            FinalizationTrustState.INVALID,
            report_dir,
            report_dir / "report-manifest-0001.json",
            "forced post-publication verification failure",
        )

    monkeypatch.setattr(report_module, "verify_report_bundle", fail_verification)

    with pytest.raises(ReportError, match="forced post-publication verification failure"):
        render_reports(root)

    assert _bundle_bytes(root) == before
    _assert_no_transaction_residue(root)


def test_concurrent_report_writers_are_serialized_across_complete_transactions(
    tmp_path, monkeypatch
):
    finalization = _finalized_package(tmp_path)
    root = finalization.run_dir
    key, credential, resolver = _approval_credential()
    append_approval(
        root,
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
    )

    real_model = report_module._model
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    first_calls = 0
    call_guard = threading.Lock()

    def tracked_model(run_root, key_resolver):
        nonlocal first_calls
        name = threading.current_thread().name
        if name == "render-first":
            with call_guard:
                first_calls += 1
                first_call = first_calls == 1
            if first_call:
                first_entered.set()
                assert release_first.wait(3)
        elif name == "render-second":
            second_entered.set()
        return real_model(run_root, key_resolver)

    monkeypatch.setattr(report_module, "_model", tracked_model)
    errors: list[BaseException] = []
    results = []

    def writer(resolver_arg):
        try:
            results.append(
                render_reports(root, approval_key_resolver=resolver_arg)
            )
        except BaseException as exc:  # surfaced below with the original traceback
            errors.append(exc)

    first = threading.Thread(target=writer, args=(None,), name="render-first")
    second = threading.Thread(target=writer, args=(resolver,), name="render-second")
    first.start()
    assert first_entered.wait(3)
    second.start()
    time.sleep(0.1)
    assert not second_entered.is_set()
    release_first.set()
    first.join(5)
    second.join(5)

    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert len(results) == 2
    assert second_entered.is_set()
    assert (
        verify_report_bundle(root, approval_key_resolver=resolver).trust_state
        is FinalizationTrustState.REGENERATED_VERIFIED
    )


@pytest.mark.parametrize("failure", ["unlink", "fsync"])
def test_report_cleanup_failure_preserves_verified_new_generation(
    tmp_path, monkeypatch, failure
):
    root = _finalized_package(tmp_path).run_dir
    before = _bundle_bytes(root)
    append_audit_event(
        root, "review.completed", actor="reviewer", details={"round": 16}
    )
    verified_bytes = {}
    removed_backups = []
    real_verify = report_module.verify_report_bundle
    real_unlink = report_module._unlink
    real_fsync = report_module._PinnedDirectory.fsync

    def capture_verified_generation(*args, **kwargs):
        result = real_verify(*args, **kwargs)
        if result.trust_state is FinalizationTrustState.REGENERATED_VERIFIED:
            verified_bytes.update(_bundle_bytes(root))
        return result

    def fail_cleanup_unlink(directory, name):
        if ".backup-" in name:
            if failure == "unlink" and len(removed_backups) == 1:
                raise OSError("forced second-backup cleanup failure")
            real_unlink(directory, name)
            removed_backups.append(name)
        else:
            real_unlink(directory, name)

    def fail_cleanup_fsync(directory):
        if failure == "fsync" and removed_backups and directory.path == root / "reports":
            raise OSError("forced post-cleanup fsync failure")
        return real_fsync(directory)

    with monkeypatch.context() as patch:
        patch.setattr(report_module, "verify_report_bundle", capture_verified_generation)
        patch.setattr(report_module, "_unlink", fail_cleanup_unlink)
        patch.setattr(report_module._PinnedDirectory, "fsync", fail_cleanup_fsync)
        with pytest.raises(ReportError, match="committed.*cleanup"):
            render_reports(root)

    assert removed_backups
    assert verified_bytes != before
    assert _bundle_bytes(root) == verified_bytes
    assert verify_report_bundle(root).trust_state is FinalizationTrustState.REGENERATED_VERIFIED
    assert not any(
        ".failed-" in entry.name or ".stage-" in entry.name
        for entry in (root / "reports").iterdir()
    )
    # A cleanup error also releases writer authority for a subsequent render.
    assert render_reports(root).trust_state is FinalizationTrustState.REGENERATED_VERIFIED
