import pytest

from argus.adapters.base import AdapterError
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
from argus.engine.ates_runtime import AtesAdapterProxy, AtesRuntimeRecorder
from argus.engine.runner import run_test
from argus.engine.spec import parse_spec
from tests.conftest import FakeAdapter, FakeApp, FakeProvider


class _OpaqueSink:
    def __init__(self):
        self.calls = []
        self._refs = iter((
            "protected://test/refalpha",
            "protected://test/refbeta",
            "protected://test/refgamma",
            "protected://test/refdelta",
        ))

    def put(self, value, *, context, field_name):
        self.calls.append((value, context, field_name))
        return next(self._refs)


def _events(project_dir, run_id):
    with AtesEventStore(project_dir, RunId(run_id)) as store:
        return tuple(store.events)


def _canonical_bytes(events):
    return b"".join(event.canonical_line() for event in events)


def test_protected_action_parameter_override_wins_after_validation():
    sink = _OpaqueSink()
    policy = EvidencePrivacyPolicy(
        EvidencePrivacyConfig(
            protected_contexts=frozenset({EvidenceContext.ACTION_PARAMETER})
        ),
        protected_sink=sink,
    )

    projected = policy.action_parameter(
        "click",
        "element_id",
        7,
        validated=True,
    )

    assert projected.disposition is EvidenceDisposition.PROTECTED_REF
    assert projected.value is None
    assert projected.protected_ref == "protected://test/refalpha"
    assert sink.calls == [(7, EvidenceContext.ACTION_PARAMETER, "element_id")]


def test_content_producing_key_values_stay_redacted_after_validation():
    policy = EvidencePrivacyPolicy.standard()

    for chord in ("a", "1", "shift+a", "space", "period"):
        projected = policy.action_parameter(
            "key",
            "keys",
            chord,
            validated=True,
        )
        assert projected.disposition is EvidenceDisposition.REDACTED
        assert projected.value == "<redacted>"

    shortcut = policy.action_parameter(
        "key",
        "keys",
        "ctrl+s",
        validated=True,
    )
    navigation = policy.action_parameter(
        "key",
        "keys",
        "enter",
        validated=True,
    )
    assert shortcut.disposition is EvidenceDisposition.SAFE
    assert shortcut.value == "ctrl+s"
    assert navigation.disposition is EvidenceDisposition.SAFE
    assert navigation.value == "enter"


@pytest.mark.parametrize(
    ("value", "reference"),
    (
        ("123", "protected://vault/123"),
        (1234, "protected://vault/1234"),
        (True, "protected://vault/true"),
        ({"cvv": "masked"}, "protected://vault/cvv"),
    ),
)
def test_protected_reference_rejects_every_raw_scalar_and_mapping_key(value, reference):
    class EchoSink:
        def put(self, value, *, context, field_name):
            return reference

    policy = EvidencePrivacyPolicy(
        EvidencePrivacyConfig(
            protected_contexts=frozenset({EvidenceContext.OPERATOR_ANNOTATION})
        ),
        protected_sink=EchoSink(),
    )

    with pytest.raises(PrivacyPolicyError, match="non-opaque reference"):
        policy.capture(value, context=EvidenceContext.OPERATOR_ANNOTATION)


@pytest.mark.parametrize(
    ("value", "reference"),
    (
        ("123", "secret://vault/123"),
        (1234, "secret://vault/1234"),
        (False, "secret://vault/false"),
        ({"pin": "masked"}, "secret://vault/pin"),
    ),
)
def test_secret_refs_reject_every_raw_scalar_and_mapping_key(value, reference):
    policy = EvidencePrivacyPolicy.standard()

    with pytest.raises(PrivacyPolicyError, match="opaque references"):
        policy.capture(
            value,
            context=EvidenceContext.ACTION_PARAMETER,
            secret_refs=(reference,),
        )


def test_run_test_protects_real_assertion_actual_value(tmp_path):
    sink = _OpaqueSink()
    policy = EvidencePrivacyPolicy(
        EvidencePrivacyConfig(
            policy_id="protected-assertion-test",
            protected_contexts=frozenset({EvidenceContext.ASSERTION_ACTUAL}),
        ),
        protected_sink=sink,
    )
    app = FakeApp()
    app.title = "private-window-actual"
    spec = parse_spec(
        """\
name: protected assertion actual
target:
  adapter: desktop-gui
  launch: fake.exe
steps:
  - assert:
      window_title_contains: "private-window"
"""
    )

    result = run_test(
        spec,
        FakeProvider([]),
        FakeAdapter(app),
        project_dir=tmp_path,
        privacy_policy=policy,
    )

    assert result.status == "pass"
    expected_actual = 'window title is "private-window-actual"'
    assert sink.calls == [
        (expected_actual, EvidenceContext.ASSERTION_ACTUAL, None)
    ]

    events = _events(tmp_path, result.ates_run_id)
    assertion = next(
        to_json_compatible(event.payload)["assertion"]
        for event in events
        if event.envelope.event_type is EventType.ASSERTION_EVALUATED
    )
    assert assertion["actual"]["disposition"] == "protected_ref"
    assert assertion["actual"]["protected_ref"] == "protected://test/refalpha"
    assert expected_actual.encode() not in _canonical_bytes(events)


def test_dispatch_failure_projects_exception_text_to_protected_sink(tmp_path):
    secret_error = "dispatch-failure-private-detail"
    sink = _OpaqueSink()
    policy = EvidencePrivacyPolicy(
        EvidencePrivacyConfig(
            policy_id="protected-error-test",
            protected_contexts=frozenset({EvidenceContext.ERROR_TEXT}),
        ),
        protected_sink=sink,
    )

    class FailingDispatchAdapter(FakeAdapter):
        def dispatch_prepared_action(self, action):
            raise AdapterError(secret_error)

    adapter = FailingDispatchAdapter()
    recorder = AtesRuntimeRecorder.for_roam(
        tmp_path,
        FakeProvider([]),
        adapter,
        target="fake.exe",
        privacy_policy=policy,
    )
    recorder.begin_roam()
    proxy = AtesAdapterProxy(adapter, recorder)
    proxy.launch("fake.exe")

    with pytest.raises(AdapterError, match=secret_error):
        proxy.act({"action": "wait", "seconds": 1})

    run_id = str(recorder.run_id)
    proxy.close()
    recorder.close()

    assert sink.calls == [(secret_error, EvidenceContext.ERROR_TEXT, None)]
    events = _events(tmp_path, run_id)
    outcome = next(
        to_json_compatible(event.payload)
        for event in events
        if event.envelope.event_type is EventType.ACTION_OUTCOME_UNKNOWN
    )
    assert outcome["error"]["disposition"] == "protected_ref"
    assert outcome["error"]["protected_ref"] == "protected://test/refalpha"
    assert secret_error.encode() not in _canonical_bytes(events)
