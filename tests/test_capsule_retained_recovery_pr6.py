from __future__ import annotations

from pathlib import Path

from argus.capsule.base import (
    CapsuleHandle,
    CapsuleProvider,
    CapsuleProviderCapabilities,
    CapsuleRequest,
    CapsuleSettings,
    FailureCapsule,
)
from argus.capsule.secure_client import SecureGuestAgentClient
from argus.capsule.secure_guest_agent import SecureGuestAgentServer
from argus.execution import SecureCapsuleExecutionEnvironment


def test_secure_control_surface_has_no_recovery_credential_arming():
    client = SecureGuestAgentClient(
        "http://127.0.0.1:8765",
        "bootstrap-secret",
        allow_insecure_http=True,
    )
    server = SecureGuestAgentServer(("127.0.0.1", 0), "bootstrap-secret")
    try:
        assert not hasattr(client, "arm_recovery")
        assert not hasattr(server, "arm_recovery")
        assert not hasattr(server, "recovery_tls_key_material")
        assert not hasattr(server, "recovery_token_file")
    finally:
        server.server_close()


class ForensicProvider(CapsuleProvider):
    provider_name = "hyperv"
    provider_capabilities = CapsuleProviderCapabilities(
        provider="hyperv",
        # This is a pure lifecycle test double exercised on both CI hosts; it
        # does not invoke Hyper-V. Production Hyper-V still advertises Windows only.
        host_platforms=("windows", "linux"),
        guest_os=("windows",),
        secure_transport=True,
        network_isolation=True,
        explicit_transfers=True,
        failure_retention=True,
        egress_allowlist=True,
    )

    def __init__(self, root: Path):
        self.root = root
        self.retained: list[tuple[CapsuleHandle, str]] = []
        self.destroyed: list[str] = []

    def create(self, request: CapsuleRequest) -> CapsuleHandle:
        self.root.mkdir(parents=True, exist_ok=True)
        return CapsuleHandle(
            session_id=request.session_id,
            provider="hyperv",
            vm_name=f"Argus-{request.session_id}",
            root_dir=str(self.root),
            address="10.0.0.8",
            guest_port=request.settings.guest_port,
            transport=request.settings.guest_transport,
        )

    def retain_failure(self, handle: CapsuleHandle, reason: str) -> FailureCapsule:
        self.retained.append((handle, reason))
        failure = FailureCapsule(
            failure_id=handle.session_id,
            session_id=handle.session_id,
            provider=handle.provider,
            vm_name=handle.vm_name,
            root_dir=handle.root_dir,
            reason=reason,
            retained_at="2026-08-10T00:00:00+00:00",
            vm_state="Off",
        )
        (self.root / "failure-capsule.json").write_text(
            '{"forensic_only": true}\n', encoding="utf-8"
        )
        return failure

    def destroy(self, handle: CapsuleHandle) -> None:
        self.destroyed.append(handle.session_id)


class ForensicClient:
    def __init__(self, endpoint, token, **kwargs):
        self.endpoint = endpoint
        self.token = token
        self.rotations = []
        self.close_calls = 0

    def wait_until_ready(self, timeout_seconds):
        return None

    def rotate_session_token(self, session_id, token):
        self.rotations.append((session_id, token))
        self.token = token

    def close_session(self):
        self.close_calls += 1


def _forensic_environment(tmp_path: Path):
    provider = ForensicProvider(tmp_path / "capsule")
    clients = []

    def factory(*args, **kwargs):
        client = ForensicClient(*args, **kwargs)
        clients.append(client)
        return client

    settings = CapsuleSettings(
        image="unused",
        switch_name="unused",
        guest_token="bootstrap",
        guest_transport="https",
        guest_ca_cert=str(tmp_path / "guest-ca.pem"),
        retain_on_failure=True,
    )
    environment = SecureCapsuleExecutionEnvironment(
        "cli",
        settings,
        provider=provider,
        client_factory=factory,
        session_id="forensic123",
    )
    environment.prepare()
    return environment, provider, clients[0]


def test_secure_failure_retention_powers_off_without_persisting_restart_authority(tmp_path):
    environment, provider, client = _forensic_environment(tmp_path)
    environment.record_failure("assertion failed")

    environment.close()

    assert len(provider.retained) == 1
    assert provider.destroyed == []
    # PR4 retention powers the VM off before normal guest teardown. No secret is
    # written back into the potentially target-controlled guest first.
    assert client.close_calls == 0

    failure = environment.failure_capsule()
    assert failure is not None
    assert failure["vm_state"] == "Off"
    assert "recovery_credentials_path" not in failure
    assert not (provider.root / "recovery-control.json").exists()


def test_failure_capsule_contract_contains_no_restart_secret_or_recovery_path(tmp_path):
    environment, _provider, _client = _forensic_environment(tmp_path)
    environment.record_failure("assertion failed")
    environment.close()

    failure = environment.failure_capsule()
    serialized = str(failure).lower()
    assert "token" not in serialized
    assert "credential" not in serialized
    assert "recovery" not in serialized
