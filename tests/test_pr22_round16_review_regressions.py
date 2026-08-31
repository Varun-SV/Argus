from __future__ import annotations

import json

import pytest

import argus.ates.audit_impl as audit_impl
import argus.ates.audit_round2 as audit_round2
import argus.ates.reports_round4 as reports_round4
import argus.ates.reports_runtime as reports_runtime
from argus.ates import (
    ARTIFACT_POLICY_VERSION,
    ApprovalError,
    ArtifactContext,
    ArtifactId,
    AtesArtifactRepository,
    EventType,
    EvidenceValue,
    FinalizationError,
    FinalizationTrustState,
    ReportError,
    RunStatus,
    VerificationStatus,
    append_approval,
    append_audit_event,
    finalize_revision_one,
    render_reports,
    revoke_approval,
    to_json_compatible,
    validate_approvals,
    verify_report_bundle,
)
from tests.test_ates_finalization import _open_run
from tests.test_pr22_round9_review_regressions import _finalized_package
from tests.test_pr22_round11_review_regressions import _bundle_bytes
from tests.test_pr22_round14_review_regressions import _canonical_lines, _credential


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
    real_verify = reports_runtime.verify_report_bundle
    real_unlink = reports_round4._unlink
    real_fsync = reports_round4._PinnedDirectory.fsync

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
        patch.setattr(reports_runtime, "verify_report_bundle", capture_verified_generation)
        patch.setattr(reports_round4, "_unlink", fail_cleanup_unlink)
        patch.setattr(reports_round4._PinnedDirectory, "fsync", fail_cleanup_fsync)
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


@pytest.mark.parametrize(
    "stored,retried",
    [
        ({"enabled": True}, {"enabled": 1}),
        ({"enabled": 1}, {"enabled": True}),
        ({"enabled": False}, {"enabled": 0}),
        ({"items": [{"enabled": True}]}, {"items": [{"enabled": 1}]}),
    ],
)
def test_audit_dedupe_distinguishes_json_boolean_and_number(tmp_path, stored, retried):
    root = _finalized_package(tmp_path).run_dir
    append_audit_event(
        root, "policy.changed", actor="reviewer", details=stored, dedupe_key="policy-change"
    )
    before = (root / "audit.jsonl").read_bytes()
    with pytest.raises(ApprovalError, match="audit dedupe conflict"):
        append_audit_event(
            root, "policy.changed", actor="reviewer", details=retried, dedupe_key="policy-change"
        )
    assert (root / "audit.jsonl").read_bytes() == before


def test_audit_dedupe_accepts_equivalent_reordered_json_objects(tmp_path):
    root = _finalized_package(tmp_path).run_dir
    original = append_audit_event(
        root,
        "policy.changed",
        actor="reviewer",
        details={"enabled": True, "policy": {"a": 1, "b": [False, None]}},
        dedupe_key="policy-change",
    )
    before = (root / "audit.jsonl").read_bytes()
    retried = append_audit_event(
        root,
        "policy.changed",
        actor="reviewer",
        details={"policy": {"b": [False, None], "a": 1}, "enabled": True},
        dedupe_key="policy-change",
    )
    assert retried == original
    assert (root / "audit.jsonl").read_bytes() == before


def _invalidate_supersession(root, approval_id, target, key):
    approvals = [json.loads(line) for line in (root / "approvals.jsonl").read_text("utf-8").splitlines()]
    changed = next(row for row in approvals if row["approval_id"] == approval_id)
    changed["supersedes_approval_id"] = target
    changed["authentication"]["signature"] = audit_impl._sign_record(changed, key)
    (root / "approvals.jsonl").write_bytes(_canonical_lines(approvals))

    # Keep every signature, audit binding, and subsequent chain link valid so
    # the invalid historical relationship is the sole source of rejection.
    audits = [json.loads(line) for line in (root / "audit.jsonl").read_text("utf-8").splitlines()]
    previous = None
    for record in audits:
        details = record["details"]
        if record["event_type"] == "approval.changed" and details.get("approval_id") == approval_id:
            details["supersedes_approval_id"] = target
            details["approval_record_digest"] = audit_round2._approval_digest(changed)
        record["previous_record_digest"] = previous
        previous = audit_impl._audit_digest(record)
    (root / "audit.jsonl").write_bytes(_canonical_lines(audits))


@pytest.mark.parametrize("relationship", ["supersession", "generation"])
@pytest.mark.parametrize("invalid_target", ["missing", "self"])
def test_invalid_supersession_cannot_become_approval_history(
    tmp_path, relationship, invalid_target
):
    root = _finalized_package(tmp_path).run_dir
    key, credential, resolver = _credential()
    kwargs = dict(
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
    )
    first = append_approval(root, **kwargs)
    if relationship == "generation":
        parent = revoke_approval(root, first["approval_id"], **kwargs)
        dependent = append_approval(root, **kwargs)
        assert dependent["request_generation_after_approval_id"] == parent["approval_id"]
    else:
        parent = first
        dependent = append_approval(
            root,
            supersedes_approval_id=parent["approval_id"],
            reason=EvidenceValue.safe("updated review"),
            **kwargs,
        )
    before = validate_approvals(root, key_resolver=resolver)
    assert dependent["approval_id"] in before.effective_approval_ids

    target = "APR-" + "0" * 32 if invalid_target == "missing" else parent["approval_id"]
    _invalidate_supersession(root, parent["approval_id"], target, key)

    after = validate_approvals(root, key_resolver=resolver)
    invalid_ids = {parent["approval_id"], dependent["approval_id"]}
    invalid_rows = [row for row in after.records if row.record["approval_id"] in invalid_ids]
    assert len(invalid_rows) == 2
    assert all(row.verification_status is VerificationStatus.INVALID for row in invalid_rows)
    assert all(not row.effective for row in invalid_rows)
    assert not invalid_ids.intersection(after.effective_approval_ids)


def _append_collection_outcome(store, retained, ordinal):
    if retained:
        capture = AtesArtifactRepository(store).capture_bytes(
            b"collected file",
            context=ArtifactContext.COLLECTED_FILE,
            kind="collected_file",
            media_type="application/octet-stream",
        )
        assert capture.record is not None
        store.append(
            EventType.ARTIFACT_COLLECTED,
            {"artifact": to_json_compatible(capture.record), "collection_ordinal": ordinal},
        )
    else:
        store.append(
            EventType.ARTIFACT_SUPPRESSED,
            {
                "artifact_id": str(ArtifactId.new()),
                "context": ArtifactContext.COLLECTED_FILE.value,
                "kind": "collected_file",
                "capture_policy": ARTIFACT_POLICY_VERSION,
                "reason": "artifact.too_large",
                "step_attempt_id": None,
                "collection_ordinal": ordinal,
            },
        )


@pytest.mark.parametrize("outcomes", [(True, True), (True, False), (False, True), (False, False)])
def test_collection_ordinals_are_unique_across_retained_and_suppressed(tmp_path, outcomes):
    store = _open_run(tmp_path, provisional=False)
    try:
        for retained in outcomes:
            _append_collection_outcome(store, retained, 1)
        with pytest.raises(FinalizationError, match="collection_ordinal.*duplicated"):
            finalize_revision_one(store)
    finally:
        store.close()


@pytest.mark.parametrize("outcomes", [(True, True), (True, False), (False, True), (False, False)])
def test_distinct_collection_ordinals_remain_finalizable(tmp_path, outcomes):
    store = _open_run(tmp_path, provisional=False)
    try:
        for ordinal, retained in enumerate(outcomes, 1):
            _append_collection_outcome(store, retained, ordinal)
        assert finalize_revision_one(store).outcome.effective_status is RunStatus.PASSED
    finally:
        store.close()
