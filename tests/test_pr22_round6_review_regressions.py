from __future__ import annotations

import json
import multiprocessing
import xml.etree.ElementTree as ET

import pytest

from argus.ates import (
    ApprovalCredential,
    ApprovalError,
    ArtifactContext,
    AssertionId,
    AssertionRecord,
    AtesArtifactRepository,
    AtesEventStore,
    EventType,
    EvidenceValue,
    RequirementIdentity,
    RunId,
    StepAttemptId,
    StepId,
    StepRecord,
    append_approval,
    append_audit_event,
    finalize_revision_one,
    recover_revision_one,
    to_json_compatible,
    validate_approvals,
    validate_audit_chain,
)
from argus.ates import audit_round2
from argus.ates import reports_runtime
from tests.test_ates_finalization import _open_run, _run_record_json


_STARTED = "2026-08-24T05:00:00+00:00"
_ENDED = "2026-08-24T05:00:01+00:00"


def _attempt(step_id, attempt_id, status, *, ordinal=1, ended):
    return {
        "attempt": {
            "step_attempt_id": str(attempt_id),
            "step_id": str(step_id),
            "attempt": ordinal,
            "status": status,
            "started_at": _STARTED,
            "ended_at": _ENDED if ended else None,
            "retry_reason": None if ordinal == 1 else EvidenceValue.safe("retry"),
        }
    }


def _finalize_closed(store: AtesEventStore, project_dir):
    run_id = store.run_id
    try:
        finalize_revision_one(store)
    finally:
        store.close()
    return recover_revision_one(project_dir, run_id)


def _run_with_two_assertions_and_artifacts(tmp_path):
    run_id = RunId.new()
    step_id = StepId.new()
    attempt_id = StepAttemptId.new()
    step = StepRecord(
        step_id=step_id,
        instruction=EvidenceValue.safe("verify two independent requirements"),
        kind="assert",
    )
    store = AtesEventStore(tmp_path, run_id)
    store.append(
        EventType.RUN_STARTED,
        {"run": _run_record_json(run_id), "steps": [to_json_compatible(step)]},
    )
    store.append(
        EventType.ENVIRONMENT_PREPARED,
        {"environment_type": "direct", "isolated": False},
    )
    store.append(
        EventType.TARGET_LAUNCHED,
        {"target": to_json_compatible(EvidenceValue.redacted("privacy.target_value"))},
    )
    store.append(
        EventType.STEP_ATTEMPT_STARTED,
        _attempt(step_id, attempt_id, "running", ended=False),
    )

    for revision in ("rev-a", "rev-b"):
        requirement = RequirementIdentity(
            requirement_id="REQ-shared",
            source_system="round6",
            source_revision=revision,
        )
        assertion = AssertionRecord(
            assertion_id=AssertionId.new(),
            step_id=step_id,
            step_attempt_id=attempt_id,
            kind="equals",
            expected=EvidenceValue.safe("ok"),
            actual=EvidenceValue.safe("ok"),
            result="passed",
            method="deterministic",
            required=True,
            requirement=requirement,
        )
        store.append(
            EventType.ASSERTION_EVALUATED,
            {"assertion": to_json_compatible(assertion)},
        )

    artifacts = AtesArtifactRepository(store)
    for payload in (b"artifact-for-something-a", b"artifact-for-something-b"):
        captured = artifacts.capture_bytes(
            payload,
            context=ArtifactContext.CHECKPOINT_SCREENSHOT,
            kind="screenshot",
            media_type="image/png",
        )
        assert captured.record is not None
        store.append(
            EventType.CHECKPOINT_CAPTURED,
            {
                "artifact": to_json_compatible(captured.record),
                "context": ArtifactContext.CHECKPOINT_SCREENSHOT.value,
                "step_attempt_id": str(attempt_id),
            },
        )

    store.append(
        EventType.STEP_ATTEMPT_COMPLETED,
        _attempt(step_id, attempt_id, "passed", ended=True),
    )
    store.append(EventType.TARGET_CLOSED, {})
    store.append(EventType.ENVIRONMENT_RELEASED, {})
    store.append(
        EventType.RUN_MARKED_INCOMPLETE,
        {"reason": "runtime.finalization_pending", "execution_result": "pass"},
    )
    return store


def test_traceability_does_not_fabricate_attempt_level_artifact_links(tmp_path):
    result = _finalize_closed(_run_with_two_assertions_and_artifacts(tmp_path), tmp_path)
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
    raw = reports_runtime._junit(
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


def _audit_worker(run_dir: str, index: int, start_event, queue) -> None:
    try:
        from argus.ates import append_audit_event as child_append

        start_event.wait()
        record = child_append(
            run_dir,
            "round6.concurrent",
            actor=f"worker-{index}",
            details={"index": index},
            dedupe_key=f"round6:{index}",
        )
        queue.put(("ok", record["audit_id"]))
    except BaseException as exc:  # pragma: no cover - child diagnostics
        queue.put(("error", f"{type(exc).__name__}: {exc}"))


def test_concurrent_audit_transactions_keep_one_valid_hash_chain(tmp_path):
    result = _finalize_closed(_open_run(tmp_path), tmp_path)
    root = result.run_dir
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Event()
    queue = ctx.Queue()
    workers = [
        ctx.Process(target=_audit_worker, args=(str(root), index, start, queue))
        for index in range(4)
    ]
    for worker in workers:
        worker.start()
    start.set()
    messages = [queue.get(timeout=15) for _ in workers]
    for worker in workers:
        worker.join(timeout=15)
        assert worker.exitcode == 0
    assert all(kind == "ok" for kind, _value in messages), messages

    records = validate_audit_chain(root)
    concurrent = [
        record for record in records if record.get("event_type") == "round6.concurrent"
    ]
    assert len(concurrent) == 4
    assert len({record["dedupe_key"] for record in concurrent}) == 4


def test_failed_approval_audit_append_leaves_no_effective_approval_and_retry_repairs(
    tmp_path, monkeypatch
):
    result = _finalize_closed(_open_run(tmp_path), tmp_path)
    root = result.run_dir
    key = b"0123456789abcdef0123456789abcdef"
    credential = ApprovalCredential(
        key_id="round6-reviewer-key",
        key=key,
        actor="reviewer@example.invalid",
        roles=("test_reviewer",),
    )
    resolver = lambda key_id: credential if key_id == credential.key_id else None

    original = audit_round2._append_line_held
    failed = {"done": False}

    def fail_required_audit(pin, lock, name, line):
        if name == "audit.jsonl" and not failed["done"]:
            failed["done"] = True
            raise ApprovalError("injected audit append failure")
        return original(pin, lock, name, line)

    monkeypatch.setattr(audit_round2, "_append_line_held", fail_required_audit)
    with pytest.raises(ApprovalError, match="injected audit append failure"):
        append_approval(
            root,
            actor=credential.actor,
            role="test_reviewer",
            key_id=credential.key_id,
            authentication_key=key,
        )

    pending_lines = (root / "approvals.jsonl").read_text("utf-8").splitlines()
    assert len(pending_lines) == 1
    pending_id = json.loads(pending_lines[0])["approval_id"]
    pending_state = validate_approvals(root, key_resolver=resolver)
    assert pending_state.verified_approvals == ()
    assert len(pending_state.records) == 1
    assert pending_state.records[0].effective is False
    assert "pending" in (pending_state.records[0].reason or "")

    monkeypatch.setattr(audit_round2, "_append_line_held", original)
    repaired = append_approval(
        root,
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
    )
    assert repaired["approval_id"] == pending_id
    assert len((root / "approvals.jsonl").read_text("utf-8").splitlines()) == 1

    repaired_state = validate_approvals(root, key_resolver=resolver)
    assert [
        item.record["approval_id"] for item in repaired_state.verified_approvals
    ] == [pending_id]
    audit = validate_audit_chain(root)
    matching = [
        item
        for item in audit
        if item.get("event_type") == "approval.changed"
        and isinstance(item.get("details"), dict)
        and item["details"].get("approval_id") == pending_id
    ]
    assert len(matching) == 1
