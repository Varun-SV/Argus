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
    def __init__(self):
        self.calls = []
        self.refs = []

    def put(self, value, *, context, field_name, protected_ref):
        self.calls.append((value, context, field_name))
        self.refs.append(protected_ref)


def test_policy_issued_references_avoid_short_value_false_positives_and_copying():
    policy = EvidencePrivacyPolicy.standard()

    text_ref = policy.issue_secret_ref()
    short_text = policy.capture(
        "e",
        context=EvidenceContext.ACTION_PARAMETER,
        secret_refs=(text_ref,),
    )
    assert short_text.disposition is EvidenceDisposition.REDACTED
    assert short_text.secret_refs == (text_ref,)

    numeric_ref = policy.issue_secret_ref()
    short_number = policy.capture(
        1,
        context=EvidenceContext.ACTION_PARAMETER,
        secret_refs=(numeric_ref,),
    )
    assert short_number.disposition is EvidenceDisposition.REDACTED
    assert short_number.secret_refs == (numeric_ref,)

    # Even syntactically valid aliases are rejected unless this policy instance
    # issued them independently of the payload.
    for forged in (
        "secret://ates/abc00000000000000000000000000000",
        "secret://ates/01200000000000000000000000000000",
    ):
        with pytest.raises(PrivacyPolicyError, match="policy-issued opaque references"):
            policy.capture(
                "ABC",
                context=EvidenceContext.ACTION_PARAMETER,
                secret_refs=(forged,),
            )

    sink = _FixedSink()
    protected = EvidencePrivacyPolicy(
        EvidencePrivacyConfig(
            protected_contexts=frozenset({EvidenceContext.FINDING_TITLE})
        ),
        protected_sink=sink,
    )
    projected = protected.finding_title("ABC")
    assert projected.disposition is EvidenceDisposition.PROTECTED_REF
    assert projected.protected_ref == sink.refs[0]
    assert projected.protected_ref.startswith("protected://ates/")
    assert "abc" not in projected.protected_ref


class _FailingTargetSink:
    def put(self, value, *, context, field_name, protected_ref):
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

    adapter.failures = 2
    adapter.close()
    assert adapter.app.alive is False


class _RecordingProtectedSink:
    def __init__(self):
        self.calls = []
        self.refs = []

    def put(self, value, *, context, field_name, protected_ref):
        self.calls.append((value, context, field_name))
        self.refs.append(protected_ref)


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
    assert finding["title"]["protected_ref"] == sink.refs[0]
    assert finding["description"]["protected_ref"] == sink.refs[1]

    persisted = b"".join(event.canonical_line() for event in events)
    for secret in (
        b"protected roam finding",
        b"expected protected detail",
        b"actual protected detail",
        b"protected explanation",
    ):
        assert secret not in persisted
