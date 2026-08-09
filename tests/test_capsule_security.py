from __future__ import annotations

from pathlib import Path

import pytest

from argus.capsule.base import CapsuleError, CapsuleHandle, CapsuleRequest, CapsuleSettings
from argus.capsule.guest_agent import GuestAgentState, _consume_control_token
from argus.capsule.hyperv import HyperVProvider
from argus.config import load_config
from argus.execution import CapsuleExecutionEnvironment


def test_guest_control_token_is_scrubbed_before_cli_child_launch(monkeypatch):
    secret = "guest-control-secret-should-never-reach-target"
    monkeypatch.setenv("ARGUS_CAPSULE_GUEST_TOKEN", secret)

    token = _consume_control_token(token_env="ARGUS_CAPSULE_GUEST_TOKEN")

    assert token == secret
    assert "ARGUS_CAPSULE_GUEST_TOKEN" not in __import__("os").environ

    state = GuestAgentState()
    try:
        state.start(
            "cli",
            "python -c \"import os; print(os.environ.get('ARGUS_CAPSULE_GUEST_TOKEN', 'MISSING'))\"",
            "safe",
        )
        obs = state.observe(include_screenshot=False)
        assert secret not in (obs.stdout or "")
        assert "MISSING" in (obs.stdout or "")
    finally:
        state.close()


def test_guest_token_file_is_consumed_before_server_can_launch_targets(tmp_path, monkeypatch):
    token_path = tmp_path / "guest-token.once"
    token_path.write_text("file-secret\n", encoding="utf-8")
    monkeypatch.setenv("ARGUS_CAPSULE_GUEST_TOKEN", "stale-env-secret")

    token = _consume_control_token(
        token_file=str(token_path),
        token_env="ARGUS_CAPSULE_GUEST_TOKEN",
    )

    assert token == "file-secret"
    assert not token_path.exists()
    assert "ARGUS_CAPSULE_GUEST_TOKEN" not in __import__("os").environ


def test_project_config_cannot_select_arbitrary_host_secret_source(tmp_path, monkeypatch):
    argus_dir = tmp_path / ".argus"
    argus_dir.mkdir()
    (argus_dir / "config.yaml").write_text(
        """
provider: ollama
execution:
  environment: capsule
  capsule:
    guest_token_env: AWS_SECRET_ACCESS_KEY
    guest_address: 203.0.113.77
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-never-be-read-for-capsule")
    monkeypatch.setenv("ARGUS_CAPSULE_GUEST_TOKEN", "dedicated-capsule-token")

    with pytest.raises(ValueError, match="cannot select arbitrary host environment variables"):
        load_config(tmp_path)


def test_unrelated_guest_address_is_rejected_before_guest_client_creation(tmp_path):
    image = tmp_path / "golden.vhdx"
    image.write_bytes(b"golden")
    calls = []
    clients = []

    def runner(script: str, timeout: float) -> str:
        calls.append(script)
        if "Get-VMSwitch" in script:
            return "Internal"
        if "Get-VMNetworkAdapter" in script:
            return "10.0.0.9"
        return ""

    def client_factory(*args, **kwargs):
        clients.append((args, kwargs))
        raise AssertionError("guest client must not be constructed before address attestation")

    settings = CapsuleSettings(
        image=str(image),
        switch_name="Argus Internal",
        vm_root=str(tmp_path / "sessions"),
        guest_token="dedicated-capsule-token",
        guest_address="203.0.113.77",
        boot_timeout_seconds=2,
    )
    environment = CapsuleExecutionEnvironment(
        "cli",
        settings,
        provider=HyperVProvider(runner=runner),
        client_factory=client_factory,
        session_id="address-attestation",
    )

    with pytest.raises(CapsuleError, match="not reported by Hyper-V"):
        environment.prepare()

    assert clients == []
    assert any("Get-VMNetworkAdapter" in call for call in calls)
    assert any("Remove-VM" in call for call in calls)
    assert environment._prepared is False


def test_matching_configured_guest_address_is_provider_attested(tmp_path):
    image = tmp_path / "golden.vhdx"
    image.write_bytes(b"golden")
    calls = []

    def runner(script: str, timeout: float) -> str:
        calls.append(script)
        if "Get-VMSwitch" in script:
            return "Internal"
        if "Get-VMNetworkAdapter" in script:
            return "10.0.0.8\n10.0.0.9"
        return ""

    provider = HyperVProvider(runner=runner)
    settings = CapsuleSettings(
        image=str(image),
        switch_name="Argus Internal",
        vm_root=str(tmp_path / "sessions"),
        guest_token="secret",
        guest_address="10.0.0.9",
    )

    handle = provider.create(CapsuleRequest("attested", "cli", settings))
    assert handle.address == "10.0.0.9"
    provider.destroy(handle)


def test_quoted_false_in_yaml_cannot_enable_external_switch(tmp_path):
    argus_dir = tmp_path / ".argus"
    argus_dir.mkdir()
    (argus_dir / "config.yaml").write_text(
        """
provider: ollama
execution:
  environment: capsule
  capsule:
    allow_external_switch: "false"
""",
        encoding="utf-8",
    )

    cfg = load_config(tmp_path)

    assert cfg.execution.capsule.allow_external_switch is False


def test_capsule_settings_normalize_quoted_false_without_truthiness_bypass():
    settings = CapsuleSettings.from_mapping({"allow_external_switch": "false"})
    assert settings.allow_external_switch is False


@pytest.mark.parametrize("value", ["definitely", 1, [], {}])
def test_capsule_settings_reject_ambiguous_external_switch_values(value):
    with pytest.raises(CapsuleError, match="must be a boolean"):
        CapsuleSettings.from_mapping({"allow_external_switch": value})


def test_hyperv_rejects_external_switch_even_if_legacy_opt_in_is_true(tmp_path):
    image = tmp_path / "golden.vhdx"
    image.write_bytes(b"golden")

    def runner(script: str, timeout: float) -> str:
        if "Get-VMSwitch" in script:
            return "External"
        return ""

    provider = HyperVProvider(runner=runner)
    settings = CapsuleSettings(
        image=str(image),
        switch_name="Corp LAN",
        vm_root=str(tmp_path / "sessions"),
        guest_token="secret",
        allow_external_switch=True,
    )

    with pytest.raises(CapsuleError, match="not supported in PR3"):
        provider.create(CapsuleRequest("external", "desktop-gui", settings))


def test_hyperv_rejects_external_switch_without_opt_in_too(tmp_path):
    image = tmp_path / "golden.vhdx"
    image.write_bytes(b"golden")

    def runner(script: str, timeout: float) -> str:
        if "Get-VMSwitch" in script:
            return "External"
        return ""

    provider = HyperVProvider(runner=runner)
    settings = CapsuleSettings(
        image=str(image),
        switch_name="Corp LAN",
        vm_root=str(tmp_path / "sessions"),
        guest_token="secret",
    )

    with pytest.raises(CapsuleError, match="bearer protocol is not transport-encrypted"):
        provider.create(CapsuleRequest("external", "desktop-gui", settings))


def test_destroy_preserves_session_storage_when_vm_deregistration_fails(tmp_path):
    root = tmp_path / "session"
    root.mkdir()
    (root / "session.vhdx").write_bytes(b"child")

    def runner(script: str, timeout: float) -> str:
        raise CapsuleError("Remove-VM failed")

    provider = HyperVProvider(runner=runner)
    handle = CapsuleHandle(
        session_id="recoverable",
        provider="hyperv",
        vm_name="Argus-recoverable",
        root_dir=str(root),
        address="10.0.0.2",
        guest_port=8765,
    )

    with pytest.raises(CapsuleError, match="storage preserved"):
        provider.destroy(handle)

    assert root.exists()
    assert (root / "session.vhdx").exists()


def test_partial_create_preserves_storage_if_vm_cleanup_fails(tmp_path):
    image = tmp_path / "golden.vhdx"
    image.write_bytes(b"golden")
    root = tmp_path / "sessions" / "partial-security"

    def runner(script: str, timeout: float) -> str:
        if "Get-VMSwitch" in script:
            return "Internal"
        if "Set-VMProcessor" in script:
            raise CapsuleError("processor configuration failed")
        if "Remove-VM" in script:
            raise CapsuleError("VM deregistration failed")
        return ""

    provider = HyperVProvider(runner=runner)
    settings = CapsuleSettings(
        image=str(image),
        switch_name="Argus Internal",
        vm_root=str(tmp_path / "sessions"),
        guest_token="secret",
    )

    with pytest.raises(CapsuleError, match="storage preserved"):
        provider.create(CapsuleRequest("partial-security", "desktop-gui", settings))

    assert root.exists()
