"""Regressions for ATES authority-bearing identifiers and retry transitions."""
from __future__ import annotations

import json

import pytest

import argus.ates.audit as audit_module
import argus.ates.reports as report_module
from argus.ates import (
    ApprovalCredential,
    ApprovalError,
    EventType,
    EvidenceValue,
    FinalizationError,
    FinalizationTrustState,
    RunStatus,
    VerificationStatus,
    append_approval,
    finalize_revision_one,
    inspect_finalization_trust,
    render_reports,
    to_json_compatible,
    validate_approvals,
    validate_audit_chain,
)
from tests.ates_test_support import (
    _approval_credential,
    _canonical_lines,
    _finalized_package,
)
from tests.test_ates_trust_boundary_regressions import (
    _finish_retry_store,
    _open_retry_store,
    _retry_attempt_payload,
)


def _append_retry_predecessor(store, step_id, first_id, second_id, status):
    first_started = "2026-09-02T06:00:00+00:00"
    first_ended = "2026-09-02T06:00:01+00:00"
    retry_reason = EvidenceValue.safe("ordinary-retry")
    store.append(
        EventType.STEP_ATTEMPT_STARTED,
        _retry_attempt_payload(
            step_id,
            first_id,
            "running",
            ordinal=1,
            started_at=first_started,
            ended_at=None,
        ),
    )
    store.append(
        EventType.STEP_ATTEMPT_COMPLETED,
        _retry_attempt_payload(
            step_id,
            first_id,
            status,
            ordinal=1,
            started_at=first_started,
            ended_at=first_ended,
        ),
    )
    store.append(
        EventType.STEP_RETRY_SCHEDULED,
        {
            "step_id": str(step_id),
            "previous_step_attempt_id": str(first_id),
            "next_step_attempt_id": str(second_id),
            "next_attempt": 2,
            "reason": to_json_compatible(retry_reason),
        },
    )
    return retry_reason


def _append_passing_retry(store, step_id, second_id, retry_reason):
    second_started = "2026-09-02T06:00:02+00:00"
    second_ended = "2026-09-02T06:00:03+00:00"
    store.append(
        EventType.STEP_ATTEMPT_STARTED,
        _retry_attempt_payload(
            step_id,
            second_id,
            "running",
            ordinal=2,
            started_at=second_started,
            ended_at=None,
            retry_reason=retry_reason,
        ),
    )
    store.append(
        EventType.STEP_ATTEMPT_COMPLETED,
        _retry_attempt_payload(
            step_id,
            second_id,
            "passed",
            ordinal=2,
            started_at=second_started,
            ended_at=second_ended,
            retry_reason=retry_reason,
        ),
    )


@pytest.mark.parametrize("predecessor", ["cancelled", "passed"])
def test_nonretryable_terminal_attempt_cannot_be_erased_by_passing_retry(
    tmp_path, predecessor
):
    store, step_id, first_id, second_id = _open_retry_store(tmp_path)
    retry_reason = _append_retry_predecessor(
        store, step_id, first_id, second_id, predecessor
    )
    _append_passing_retry(store, step_id, second_id, retry_reason)
    _finish_retry_store(store)
    try:
        with pytest.raises(FinalizationError, match="not retryable by an ordinary retry"):
            finalize_revision_one(store)
    finally:
        store.close()


@pytest.mark.parametrize("predecessor", ["cancelled", "passed"])
def test_incomplete_prefix_rejects_retry_after_nonretryable_terminal_attempt(
    tmp_path, predecessor
):
    store, step_id, first_id, second_id = _open_retry_store(tmp_path)
    _append_retry_predecessor(store, step_id, first_id, second_id, predecessor)
    _finish_retry_store(store, execution_result="error")
    root = store.run_dir
    store.close()

    inspected = inspect_finalization_trust(root)
    assert inspected.trust_state is FinalizationTrustState.INVALID
    with pytest.raises(report_module.ReportError):
        render_reports(root)


def test_failed_attempt_remains_retryable(tmp_path):
    store, step_id, first_id, second_id = _open_retry_store(tmp_path)
    retry_reason = _append_retry_predecessor(
        store, step_id, first_id, second_id, "failed"
    )
    _append_passing_retry(store, step_id, second_id, retry_reason)
    _finish_retry_store(store)
    try:
        result = finalize_revision_one(store)
    finally:
        store.close()

    assert result.outcome.effective_status is RunStatus.PASSED


@pytest.mark.parametrize(
    "field,secret",
    [
        ("role", "Bearer TOP-SECRET-ROLE"),
        ("key_id", "Bearer TOP-SECRET-KEY"),
    ],
)
def test_approval_authority_identifiers_fail_before_persistence(tmp_path, field, secret):
    root = _finalized_package(tmp_path).run_dir
    approvals_path = root / "approvals.jsonl"
    audit_path = root / "audit.jsonl"
    approvals_before = approvals_path.read_bytes()
    audit_before = audit_path.read_bytes()
    key, credential, _resolver = _approval_credential()
    kwargs = {
        "actor": credential.actor,
        "role": "test_reviewer",
        "key_id": credential.key_id,
        "authentication_key": key,
    }
    kwargs[field] = secret

    with pytest.raises(ApprovalError, match="machine-safe"):
        append_approval(root, **kwargs)

    assert approvals_path.read_bytes() == approvals_before
    assert audit_path.read_bytes() == audit_before


@pytest.mark.parametrize(
    "field,secret",
    [
        ("role", "Bearer TOP-SECRET-ROLE"),
        ("key_id", "Bearer TOP-SECRET-KEY"),
    ],
)
def test_approval_credential_rejects_unsafe_authority_identifiers(field, secret):
    kwargs = {
        "key_id": "reviewer-key",
        "key": b"0123456789abcdef0123456789abcdef",
        "actor": "reviewer@example.invalid",
        "roles": ("test_reviewer",),
    }
    if field == "role":
        kwargs["roles"] = (secret,)
    else:
        kwargs["key_id"] = secret

    with pytest.raises(ValueError, match="machine-safe"):
        ApprovalCredential(**kwargs)


@pytest.mark.parametrize(
    "field,secret",
    [
        ("role", "Bearer TOP-SECRET-ROLE"),
        ("key_id", "Bearer TOP-SECRET-KEY"),
    ],
)
def test_imported_approval_authority_identifiers_cannot_reach_reports(
    tmp_path, field, secret
):
    root = _finalized_package(tmp_path).run_dir
    key, credential, resolver = _approval_credential()
    append_approval(
        root,
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
    )
    approval_path = root / "approvals.jsonl"
    records = [
        json.loads(line) for line in approval_path.read_text("utf-8").splitlines()
    ]
    assert len(records) == 1
    if field == "role":
        records[0]["role"] = secret
    else:
        records[0]["authentication"]["key_id"] = secret
    records[0]["authentication"]["signature"] = audit_module._sign_record(
        records[0], key
    )
    approval_path.write_bytes(_canonical_lines(records))

    validated = validate_approvals(root, key_resolver=resolver)
    assert len(validated.records) == 1
    assert validated.records[0].verification_status is VerificationStatus.INVALID
    assert not validated.records[0].effective

    bundle = render_reports(root, approval_key_resolver=resolver)
    for path in (
        bundle.json_path,
        bundle.markdown_path,
        bundle.html_path,
        bundle.junit_path,
    ):
        assert secret not in path.read_text("utf-8")


def test_malformed_signed_candidate_is_not_reused_or_audit_repaired(tmp_path):
    root = _finalized_package(tmp_path).run_dir
    key, credential, _resolver = _approval_credential()
    approval = append_approval(
        root,
        actor=credential.actor,
        role="test_reviewer",
        key_id=credential.key_id,
        authentication_key=key,
    )
    approval_id = approval["approval_id"]
    approval_path = root / "approvals.jsonl"
    audit_path = root / "audit.jsonl"

    approvals = [
        json.loads(line) for line in approval_path.read_text("utf-8").splitlines()
    ]
    assert len(approvals) == 1
    approvals[0]["debug_note"] = "plaintext-extension"
    approvals[0]["authentication"]["signature"] = audit_module._sign_record(
        approvals[0], key
    )
    approval_path.write_bytes(_canonical_lines(approvals))

    audits = [json.loads(line) for line in audit_path.read_text("utf-8").splitlines()]
    audits = [
        record
        for record in audits
        if not (
            record.get("event_type") == "approval.changed"
            and isinstance(record.get("details"), dict)
            and record["details"].get("approval_id") == approval_id
        )
    ]
    audit_path.write_bytes(_canonical_lines(audits))
    validate_audit_chain(root)

    approvals_before = approval_path.read_bytes()
    audit_before = audit_path.read_bytes()
    with pytest.raises(ApprovalError):
        append_approval(
            root,
            actor=credential.actor,
            role="test_reviewer",
            key_id=credential.key_id,
            authentication_key=key,
        )

    assert approval_path.read_bytes() == approvals_before
    assert audit_path.read_bytes() == audit_before
    assert not any(
        record.get("event_type") == "approval.changed"
        and isinstance(record.get("details"), dict)
        and record["details"].get("approval_id") == approval_id
        for record in validate_audit_chain(root)
    )
