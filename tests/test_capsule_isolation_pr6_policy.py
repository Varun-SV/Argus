import pytest

from argus.capsule.base import CapsuleError, CapsuleRequest, CapsuleSettings
from argus.capsule.hyperv_isolated import IsolatedHyperVProvider
from argus.execution import ExecutionEnvironmentError, SecureCapsuleExecutionEnvironment


def test_secure_environment_rejects_disabled_session_rotation():
    with pytest.raises(ExecutionEnvironmentError, match="cannot be disabled"):
        SecureCapsuleExecutionEnvironment(
            "cli",
            CapsuleSettings(rotate_session_token=False),
        )


def test_secure_hyperv_rejects_disabled_guest_file_copy_isolation():
    provider = IsolatedHyperVProvider(runner=lambda script, timeout: "")
    settings = CapsuleSettings(
        guest_transport="http",
        allow_insecure_http=True,
        disable_guest_file_copy=False,
    )
    with pytest.raises(CapsuleError, match="disable_guest_file_copy cannot be false"):
        provider._validate_isolation_settings(
            CapsuleRequest("policy123", "cli", settings)
        )
