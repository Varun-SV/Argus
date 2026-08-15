import json

import pytest

from argus.ates import (
    AtesEventStore,
    EvidenceContext,
    EvidenceDisposition,
    EvidencePrivacyConfig,
    EvidencePrivacyPolicy,
    EventType,
    PrivacyPolicyError,
    RunId,
    to_json_compatible,
)
from argus.engine.ates_runtime import AtesRuntimeRecorder
from argus.engine.roam import roam
from argus.engine.runner import run_test
from argus.engine.spec import parse_spec
from argus.tokens import Budget
from tests.conftest import FakeAdapter, FakeProvider


def _action(**values):
    return json.dumps(values)


def _events(project_dir, run_id):
    with AtesEventStore(project_dir, RunId(run_id)) as store:
        return tuple(store.events)


class _FixedSink:
    def __init__(self, reference):
        self.reference = reference
        self.calls = []

    def put(self, value, *, context, field_name):
        self.calls.append((value, context, field_name))
        return self.reference


def test_reference_opacity_catches_case_normalized_short_leaks_without_short_value_false_positives():
    policy = EvidencePrivacyPolicy.standard()

    with pytest.raises(PrivacyPolicyError, match="opaque references"):
        policy.capture(
            "ABC",
            context=EvidenceContext.ACTION_PARAMETER,
            secret_refs=("secret://vault/abc",),
        )
    with pytest.raises(PrivacyPolicyError, match="opaque references"):
        policy.capture(
            "ABC",
            context=EvidenceContext.ACTION_PARAMETER,
            secret_refs=("secret://vault/xabcx",),
        )
    with pytest.raises(PrivacyPolicyError, match="opaque references"):
        policy.capture(
            12,
            context=EvidenceContext.ACTION_PARAMETER,
            secret_refs=("secret://vault/id12x",),
        )

    short_text = policy.capture(
        "e",
        context=EvidenceContext.ACTION_PARAMETER,
        secret_refs=("secret://vault/evidence-record-8e7f",),
    )
    assert short_text.disposition is EvidenceDisposition.REDACTED

    short_number = policy.capture(
        1,
        context=EvidenceContext.ACTION_PARAMETER,
        secret_refs=("secret://vault/record-a1b2c3",),
    )
    assert short_number.disposition is EvidenceDisposition.REDACTED

    leaking_sink = _FixedSink("protected://vault/abc")
    protected = EvidencePrivacyPolicy(
        EvidencePrivacyConfig(
            protected_contexts=frozenset({EvidenceContext.FINDING_TITLE})
        ),
        protected_sink=leaking_sink,
    )
    with pytest.raises(PrivacyPolicyError, match="non-opaque reference"):
        protected.finding_title("ABC")

    embedded_sink = _FixedSink("protected://vault/xabcx")
    embedded_policy = EvidencePrivacyPolicy(
        EvidencePrivacyConfig(
            protected_contexts=frozenset({EvidenceContext.FINDING_TITLE})
        ),
        protected_sink=embedded_sink,
    )
    with pytest.raises(PrivacyPolicyError, match="non-opaque reference"):
        embedded_policy.finding_title("ABC")

    embedded_numeric_sink = _FixedSink("protected://vault/id12x")
    embedded_numeric_policy = EvidencePrivacyPolicy(
        EvidencePrivacyConfig(
            protected_contexts=frozenset({EvidenceContext.ASSERTION_ACTUAL})
        ),
        protected_sink=embedded_numeric_sink,
    )
    with pytest.raises(PrivacyPolicyError, match="non-opaque reference"):
        embedded_numeric_policy.assertion_actual(12)

    numeric_sink = _FixedSink("protected://vault/record-a1b2c3")
    numeric_policy = EvidencePrivacyPolicy(
        EvidencePrivacyConfig(
            protected_contexts=frozenset({EvidenceContext.ASSERTION_ACTUAL})
        ),
        protected_sink=numeric_sink,
    )
    numeric = numeric_policy.assertion_actual(1)
    assert numeric.disposition is EvidenceDisposition.PROTECTED_REF
    assert numeric.protected_ref == "protected://vault/record-a1b2c3"


class _FailingTargetSink:
    def put(self, value, *, context, field_name):
        assert context is EvidenceContext.TARGET
        raise OSError("protected target store unavailable")


class _LaunchTrackingAdapter(FakeAdapter):
    def __init__(self):
        super().__init__()
        self.launch_calls = 0

    def launch(self, target):
        self.launch_calls += 1
        super().launch(target)


def test_protected_target_failure_is_classified_before_launch_and_finalizes_ates(tmp_path):
    spec = parse_spec(
        """\
name: protected target launch failure
target: {adapter: desktop-gui, launch: private-target.exe}
steps:
  - "Do nothing"
"""
    )
    policy = EvidencePrivacyPolicy(
        EvidencePrivacyConfig(
            protected_contexts=frozenset({EvidenceContext.TARGET})
        ),
        protected_sink=_FailingTargetSink(),
    )
    adapter = _LaunchTrackingAdapter()

    result = run_test(
        spec,
        FakeProvider([]),
        adapter,
        project_dir=tmp_path,
        privacy_policy=policy,
    )

    assert result.status == "error"
    assert adapter.launch_calls == 0
    assert "launch failed" in result.error
    assert result.ates_run_id.startswith("RUN-")

    events = _events(tmp_path, result.ates_run_id)
    types = [event.envelope.event_type for event in events]
    assert EventType.TARGET_LAUNCHED not in types
    assert EventType.ENVIRONMENT_RELEASED in types
    assert types[-1] is EventType.RUN_MARKED_INCOMPLETE


class _RollbackRetryAdapter(FakeAdapter):
    def __init__(self, failures):
        super().__init__()
        self.failures = failures
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        if self.close_calls <= self.failures:
            raise OSError(f"rollback close failure {self.close_calls}")
        super().close()


def _fail_target_launched_append(monkeypatch):
    original_append = AtesRuntimeRecorder._append

    def failing_append(self, event_type, payload):
        if event_type is EventType.TARGET_LAUNCHED:
            failure = OSError("target launch evidence unavailable")
            self._failed = True
            self._failure = failure
            raise failure
        return original_append(self, event_type, payload)

    monkeypatch.setattr(AtesRuntimeRecorder, "_append", failing_append)


def test_target_launch_evidence_failure_retries_failed_rollback(tmp_path, monkeypatch):
    _fail_target_launched_append(monkeypatch)
    spec = parse_spec(
        """\
name: target launch evidence rollback retry
target: {adapter: desktop-gui, launch: private-target.exe}
steps:
  - "Do nothing"
"""
    )
    adapter = _RollbackRetryAdapter(failures=1)

    result = run_test(spec, FakeProvider([]), adapter, project_dir=tmp_path)

    assert result.status == "error"
    assert adapter.close_calls == 2
    assert adapter.app.alive is False
    assert "initial rollback failed but cleanup retry succeeded" in result.error
    assert "ATES evidence failure" in result.error


def test_target_launch_evidence_failure_surfaces_unrecoverable_rollback(tmp_path, monkeypatch):
    _fail_target_launched_append(monkeypatch)
    spec = parse_spec(
        """\
name: target launch evidence rollback failure
target: {adapter: desktop-gui, launch: private-target.exe}
steps:
  - "Do nothing"
"""
    )
    adapter = _RollbackRetryAdapter(failures=2)

    result = run_test(spec, FakeProvider([]), adapter, project_dir=tmp_path)

    assert result.status == "error"
    assert adapter.close_calls == 2
    assert adapter.app.alive is True
    assert "rollback failed after cleanup retry" in result.error
    assert "target may still be running" in result.error
    assert "ATES evidence failure" in result.error

    # The underlying adapter remains available to the caller for explicit
    # recovery after Argus reports the unresolved launch cleanup state.
    adapter.failures = 2
    adapter.close()
    assert adapter.app.alive is False


class _RecordingProtectedSink:
    def __init__(self):
        self.calls = []

    def put(self, value, *, context, field_name):
        self.calls.append((value, context, field_name))
        return f"protected://review/opaque-{len(self.calls)}"


def test_public_roam_accepts_privacy_policy_and_protects_findings(tmp_path, monkeypatch):
    import argus.engine.roam_impl as roam_impl

    monkeypatch.setattr(roam_impl.time, "sleep", lambda _seconds: None)
    sink = _RecordingProtectedSink()
    policy = EvidencePrivacyPolicy(
        EvidencePrivacyConfig(
            protected_contexts=frozenset(
                {
                    EvidenceContext.FINDING_TITLE,
                    EvidenceContext.FINDING_DESCRIPTION,
                }
            )
        ),
        protected_sink=sink,
    )
    provider = FakeProvider(
        [
            _action(action="click", element_id=2),
            _action(
                action="report_bug",
                title="protected roam finding",
                severity="high",
                expected="expected protected detail",
                actual="actual protected detail",
                why="protected explanation",
            ),
        ]
    )
    budget = Budget(max_tokens=241, tracker=provider.tracker)
    session_dir = tmp_path / ".argus" / "roam" / "protected-session"

    session = roam(
        target="private-roam-target.exe",
        provider=provider,
        adapter=FakeAdapter(),
        budget=budget,
        session_dir=session_dir,
        project_dir=tmp_path,
        generate_regressions=False,
        privacy_policy=policy,
    )

    assert len(session.findings) == 1
    assert [call[1] for call in sink.calls] == [
        EvidenceContext.FINDING_TITLE,
        EvidenceContext.FINDING_DESCRIPTION,
    ]
    assert sink.calls[0][0] == "protected roam finding"
    assert sink.calls[1][0] == {
        "expected": "expected protected detail",
        "actual": "actual protected detail",
        "detail": "protected explanation",
    }

    events = _events(tmp_path, session.ates_run_id)
    finding_event = next(
        event for event in events if event.envelope.event_type is EventType.FINDING_RECORDED
    )
    finding = to_json_compatible(finding_event.payload)["finding"]
    assert finding["title"]["disposition"] == "protected_ref"
    assert finding["description"]["disposition"] == "protected_ref"
    assert finding["title"]["protected_ref"] == "protected://review/opaque-1"
    assert finding["description"]["protected_ref"] == "protected://review/opaque-2"

    persisted = b"".join(event.canonical_line() for event in events)
    for secret in (
        b"protected roam finding",
        b"expected protected detail",
        b"actual protected detail",
        b"protected explanation",
    ):
        assert secret not in persisted
