import pytest

from argus.ates import (
    EvidenceContext,
    EvidencePrivacyConfig,
    EvidencePrivacyPolicy,
    PrivacyPolicyError,
)


class _EchoingProtectedSink:
    def put(self, value, *, context, field_name):
        return f"protected://vault/{value}"


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


def test_protected_reference_cannot_echo_raw_protected_text():
    policy = EvidencePrivacyPolicy(
        EvidencePrivacyConfig(
            protected_contexts=frozenset({EvidenceContext.FINDING_TITLE})
        ),
        protected_sink=_EchoingProtectedSink(),
    )

    with pytest.raises(PrivacyPolicyError, match="non-opaque reference") as exc_info:
        policy.finding_title("private-customer-value")

    assert "private-customer-value" not in str(exc_info.value)


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
