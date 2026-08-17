import pytest

from argus.ates import (
    EvidenceContext,
    EvidencePrivacyConfig,
    EvidencePrivacyPolicy,
)


class _RecordingProtectedSink:
    def __init__(self):
        self.refs = []

    def put(self, value, *, context, field_name, protected_ref):
        self.refs.append(protected_ref)


def test_effective_policy_identity_binds_protected_context_set():
    stdout_policy = EvidencePrivacyPolicy(
        EvidencePrivacyConfig(
            policy_id="tenant-policy-v1",
            protected_contexts=frozenset({EvidenceContext.OBSERVATION_STDOUT}),
        )
    )
    finding_policy = EvidencePrivacyPolicy(
        EvidencePrivacyConfig(
            policy_id="tenant-policy-v1",
            protected_contexts=frozenset({EvidenceContext.FINDING_TITLE}),
        )
    )

    assert stdout_policy.policy_id.startswith("tenant-policy-v1.")
    assert finding_policy.policy_id.startswith("tenant-policy-v1.")
    assert stdout_policy.policy_id != finding_policy.policy_id


def test_protected_reference_is_generated_independently_of_protected_text():
    sink = _RecordingProtectedSink()
    policy = EvidencePrivacyPolicy(
        EvidencePrivacyConfig(
            protected_contexts=frozenset({EvidenceContext.FINDING_TITLE})
        ),
        protected_sink=sink,
    )

    value = policy.finding_title("private-customer-value")

    assert value.protected_ref == sink.refs[0]
    assert value.protected_ref.startswith("protected://ates/")
    assert "private-customer-value" not in value.protected_ref


def test_direct_safe_key_fact_uses_real_argus_key_grammar():
    policy = EvidencePrivacyPolicy.standard()

    canonical = policy.action_parameter(
        "key", "keys", "ctrl+s", validated=True
    )
    invalid_word = policy.action_parameter(
        "key", "keys", "customersecret", validated=True
    )

    assert canonical.disposition.value == "safe"
    assert canonical.value == "ctrl+s"
    assert invalid_word.disposition.value == "redacted"
    assert invalid_word.value == "<redacted>"
