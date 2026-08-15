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
