import json

import pytest

from argus.adapters.base import Observation
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
from argus.engine.runner import run_test
from argus.engine.spec import parse_spec
from tests.conftest import FakeAdapter, FakeApp, FakeProvider


def _events(project_dir, run_id):
    with AtesEventStore(project_dir, RunId(run_id)) as store:
        return tuple(store.events)


def _canonical_bytes(events):
    return b"".join(event.canonical_line() for event in events)


def test_default_policy_redacts_authored_values_and_suppresses_target_generated_text():
    policy = EvidencePrivacyPolicy.standard()

    authored = policy.capture(
        "customer-password-123",
        context=EvidenceContext.STEP_INSTRUCTION,
    )
    target = policy.observation_value("stdout", "token=target-secret")

    assert authored.disposition is EvidenceDisposition.REDACTED
    assert authored.value == "<redacted>"
    assert target.disposition is EvidenceDisposition.SUPPRESSED
    assert target.value is None
    assert "customer-password-123" not in json.dumps(to_json_compatible(authored))
    assert "target-secret" not in json.dumps(to_json_compatible(target))


def test_secret_references_are_opaque_and_strict():
    policy = EvidencePrivacyPolicy.standard()
    value = policy.capture(
        "private-value",
        context=EvidenceContext.ACTION_PARAMETER,
        secret_refs=("secret://vault/customer-token",),
    )
    assert value.secret_refs == ("secret://vault/customer-token",)
    assert value.value == "<redacted>"

    with pytest.raises(PrivacyPolicyError):
        policy.capture(
            "private-value",
            context=EvidenceContext.ACTION_PARAMETER,
            secret_refs=("customer-token-is-secret",),
        )


class _ProtectedSink:
    def __init__(self):
        self.calls = []

    def put(self, value, *, context, field_name):
        self.calls.append((value, context, field_name))
        return f"protected://test/{len(self.calls)}"


def test_protected_context_never_copies_plaintext_to_ordinary_evidence():
    sink = _ProtectedSink()
    policy = EvidencePrivacyPolicy(
        EvidencePrivacyConfig(
            protected_contexts=frozenset({EvidenceContext.OBSERVATION_STDOUT})
        ),
        protected_sink=sink,
    )

    value = policy.observation_value("stdout", "very-private-output")

    assert value.disposition is EvidenceDisposition.PROTECTED_REF
    assert value.value is None
    assert value.protected_ref == "protected://test/1"
    assert sink.calls == [
        ("very-private-output", EvidenceContext.OBSERVATION_STDOUT, "stdout")
    ]
    assert "very-private-output" not in json.dumps(to_json_compatible(value))


def test_protected_context_fails_closed_without_valid_sink():
    policy = EvidencePrivacyPolicy(
        EvidencePrivacyConfig(
            protected_contexts=frozenset({EvidenceContext.FINDING_TITLE})
        )
    )
    with pytest.raises(PrivacyPolicyError) as exc_info:
        policy.finding_title("do-not-repeat-this-secret")
    assert "do-not-repeat-this-secret" not in str(exc_info.value)

    class BadSink:
        def put(self, value, *, context, field_name):
            return value

    policy = EvidencePrivacyPolicy(
        EvidencePrivacyConfig(
            protected_contexts=frozenset({EvidenceContext.FINDING_TITLE})
        ),
        protected_sink=BadSink(),
    )
    with pytest.raises(PrivacyPolicyError) as exc_info:
        policy.finding_title("another-private-value")
    assert "another-private-value" not in str(exc_info.value)


def test_only_validated_action_structure_can_become_safe_fact():
    policy = EvidencePrivacyPolicy.standard()

    proposed_id = policy.action_parameter(
        "type", "element_id", 7, validated=False
    )
    validated_id = policy.action_parameter(
        "type", "element_id", 7, validated=True
    )
    validated_text = policy.action_parameter(
        "type", "text", "customer-secret", validated=True
    )
    validated_command = policy.action_parameter(
        "run", "command", "echo customer-secret", validated=True
    )
    validated_key = policy.action_parameter(
        "key", "keys", "ctrl+s", validated=True
    )

    assert proposed_id.disposition is EvidenceDisposition.REDACTED
    assert validated_id.disposition is EvidenceDisposition.SAFE
    assert validated_id.value == 7
    assert validated_text.disposition is EvidenceDisposition.REDACTED
    assert validated_command.disposition is EvidenceDisposition.REDACTED
    assert validated_key.disposition is EvidenceDisposition.SAFE
    assert validated_key.value == "ctrl+s"


def test_runtime_projects_secrets_without_mutating_executable_action(tmp_path):
    secret = "runtime-customer-secret"
    app = FakeApp()
    adapter = FakeAdapter(app)
    spec = parse_spec(
        f"""\
name: privacy execution separation
target: {{adapter: desktop-gui, launch: private-target.exe}}
steps:
  - "Type {secret}"
"""
    )
    provider = FakeProvider(
        [
            json.dumps(
                {
                    "action": "type",
                    "text": secret,
                    "element_id": 1,
                    "why": "contains-private-rationale",
                }
            ),
            json.dumps({"action": "done", "success": True}),
        ]
    )

    result = run_test(spec, provider, adapter, project_dir=tmp_path)
    assert result.status == "pass"
    # The evidence projection must not redact or otherwise rewrite the action
    # that actually crosses the adapter dispatch boundary.
    assert app.text_content == secret

    events = _events(tmp_path, result.ates_run_id)
    persisted = _canonical_bytes(events)
    assert secret.encode() not in persisted
    assert b"contains-private-rationale" not in persisted
    assert b"private-target.exe" not in persisted

    proposed = next(
        to_json_compatible(event.payload)["action"]
        for event in events
        if event.envelope.event_type is EventType.ACTION_PROPOSED
    )
    validated = next(
        to_json_compatible(event.payload)["action"]
        for event in events
        if event.envelope.event_type is EventType.ACTION_POLICY_VALIDATED
    )
    committed = next(
        to_json_compatible(event.payload)["action"]
        for event in events
        if event.envelope.event_type is EventType.ACTION_DISPATCH_COMMITTED
    )

    assert proposed["parameters"]["text"]["disposition"] == "redacted"
    assert proposed["parameters"]["element_id"]["disposition"] == "redacted"
    assert validated["parameters"]["text"]["disposition"] == "redacted"
    assert validated["parameters"]["element_id"] == {
        "disposition": "safe",
        "value": 1,
        "reason": None,
        "secret_refs": [],
        "protected_ref": None,
    }
    assert committed["parameters"] == validated["parameters"]

    started = to_json_compatible(events[0].payload)
    assert started["run"]["evidence_profile"] == "ates-privacy-v1"


def test_runtime_observation_freeform_fields_are_suppressed(tmp_path):
    recorder = AtesRuntimeRecorder.for_roam(
        tmp_path,
        FakeProvider([]),
        FakeAdapter(),
        target="private.exe",
    )
    recorder.begin_roam()
    recorder.record_observation(
        Observation(
            window_title="private-window-title",
            stdout="private-stdout",
            stderr="private-stderr",
            error="private-error",
            url="https://example.invalid/?token=private-url-secret",
            dialogs=["private-dialog"],
            screenshot_png=b"screenshot-private-bytes",
        ),
        "fake",
    )
    run_id = str(recorder.run_id)
    recorder.close()

    events = _events(tmp_path, run_id)
    observation = next(
        to_json_compatible(event.payload)["observation"]
        for event in events
        if event.envelope.event_type is EventType.OBSERVATION_CAPTURED
    )
    facts = observation["facts"]
    for name in ("window_title", "ui_tree", "dialogs", "error", "stdout", "stderr", "url"):
        assert facts[name]["disposition"] == "suppressed"
    assert facts["screenshot_present"]["value"] is True

    persisted = _canonical_bytes(events)
    for secret in (
        b"private-window-title",
        b"private-stdout",
        b"private-stderr",
        b"private-error",
        b"private-url-secret",
        b"private-dialog",
        b"screenshot-private-bytes",
    ):
        assert secret not in persisted


def test_runtime_protected_sink_gets_text_but_never_screenshot_bytes(tmp_path):
    sink = _ProtectedSink()
    policy = EvidencePrivacyPolicy(
        EvidencePrivacyConfig(
            policy_id="ates-privacy-protected-test",
            protected_contexts=frozenset(
                {
                    EvidenceContext.OBSERVATION_STDOUT,
                    EvidenceContext.FINDING_TITLE,
                }
            ),
        ),
        protected_sink=sink,
    )
    recorder = AtesRuntimeRecorder.for_roam(
        tmp_path,
        FakeProvider([]),
        FakeAdapter(),
        target="private.exe",
        privacy_policy=policy,
    )
    recorder.begin_roam()
    recorder.record_observation(
        Observation(
            window_title="ordinary-title",
            stdout="protected-output",
            screenshot_png=b"must-never-enter-text-privacy-sink",
        ),
        "fake",
    )
    recorder.record_finding(
        source="model",
        classification="high",
        title="protected-finding-title",
        description="suppressed-finding-description",
    )
    run_id = str(recorder.run_id)
    recorder.close()

    assert [call[0] for call in sink.calls] == [
        "protected-output",
        "protected-finding-title",
    ]
    assert all(
        call[0] != b"must-never-enter-text-privacy-sink"
        for call in sink.calls
    )

    persisted = _canonical_bytes(_events(tmp_path, run_id))
    assert b"protected-output" not in persisted
    assert b"protected-finding-title" not in persisted
    assert b"suppressed-finding-description" not in persisted
    assert b"must-never-enter-text-privacy-sink" not in persisted
    assert b"protected://test/1" in persisted
    assert b"protected://test/2" in persisted


def test_privacy_policy_identity_changes_run_configuration_commitment(tmp_path):
    spec = parse_spec(
        """\
name: privacy policy provenance
target: {adapter: desktop-gui, launch: fake.exe}
steps:
  - "Do something"
"""
    )
    first = AtesRuntimeRecorder.for_scripted(
        tmp_path,
        spec,
        FakeProvider([]),
        FakeAdapter(),
        privacy_policy=EvidencePrivacyPolicy(
            EvidencePrivacyConfig(policy_id="ates-privacy-alpha")
        ),
    )
    second = AtesRuntimeRecorder.for_scripted(
        tmp_path,
        spec,
        FakeProvider([]),
        FakeAdapter(),
        privacy_policy=EvidencePrivacyPolicy(
            EvidencePrivacyConfig(policy_id="ates-privacy-beta")
        ),
    )
    try:
        assert first.run_record.evidence_profile == "ates-privacy-alpha"
        assert second.run_record.evidence_profile == "ates-privacy-beta"
        assert (
            first.run_record.configuration_commitment.value
            != second.run_record.configuration_commitment.value
        )
    finally:
        first.close()
        second.close()
