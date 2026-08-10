from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from argus.capsule.base import (
    CapsuleError,
    CapsuleHandle,
    CapsuleProvider,
    CapsuleRequest,
    CapsuleSettings,
    FailureCapsule,
)
from argus.capsule.secure_client import SecureGuestAgentClient
from argus.capsule.secure_guest_agent import SecureGuestAgentServer
from argus.execution import SecureCapsuleExecutionEnvironment


def test_guest_recovery_arm_restores_one_time_restart_material(tmp_path):
    token_file = tmp_path / "guest-token.once"
    key_file = tmp_path / "guest-key.pem"
    server = SecureGuestAgentServer(
        ("127.0.0.1", 0),
        "bootstrap-secret",
        recovery_token_file=str(token_file),
        recovery_tls_key_path=str(key_file),
    )
    server.recovery_tls_key_material = b"private-key-material"
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.01},
        daemon=True,
    )
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_address[1]}"
    client = SecureGuestAgentClient(
        endpoint,
        "bootstrap-secret",
        allow_insecure_http=True,
        timeout_seconds=2,
    )
    try:
        client.rotate_session_token("retained123", "s" * 48)
        recovery = "r" * 48
        client.arm_recovery("retained123", recovery)

        assert token_file.read_text(encoding="utf-8").strip() == recovery
        assert key_file.read_bytes() == b"private-key-material"
        assert server.recovery_armed is True
        assert server.recovery_tls_key_material == b""

        with pytest.raises(Exception, match="already armed"):
            client.arm_recovery("retained123", "t" * 48)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class RecoveryProvider(CapsuleProvider):
    provider_name = "hyperv"

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
        return FailureCapsule(
            failure_id=handle.session_id,
            session_id=handle.session_id,
            provider=handle.provider,
            vm_name=handle.vm_name,
            root_dir=handle.root_dir,
            reason=reason,
            retained_at="2026-08-10T00:00:00+00:00",
            vm_state="Off",
        )

    def destroy(self, handle: CapsuleHandle) -> None:
        self.destroyed.append(handle.session_id)


class RecoveryClient:
    def __init__(self, endpoint, token, *, fail_arm: bool = False, **kwargs):
        self.endpoint = endpoint
        self.token = token
        self.fail_arm = fail_arm
        self.rotations = []
        self.events = []
        self.armed = []

    def wait_until_ready(self, timeout_seconds):
        self.events.append("ready")

    def rotate_session_token(self, session_id, token):
        self.rotations.append((session_id, token))
        self.token = token
        self.events.append("rotate")

    def close_session(self):
        self.events.append("close-target")

    def arm_recovery(self, session_id, token):
        self.events.append("arm-recovery")
        if self.fail_arm:
            raise CapsuleError("recovery arm failed")
        self.armed.append((session_id, token))


def _recovery_environment(tmp_path: Path, *, fail_arm: bool = False):
    provider = RecoveryProvider(tmp_path / "capsule")
    clients = []

    def factory(*args, **kwargs):
        client = RecoveryClient(*args, fail_arm=fail_arm, **kwargs)
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
        session_id="recoverable123",
    )
    environment.prepare()
    return environment, provider, clients[0]


def test_retained_secure_capsule_arms_recovery_before_provider_poweroff(tmp_path):
    environment, provider, client = _recovery_environment(tmp_path)
    environment.record_failure("assertion failed")

    environment.close()

    assert client.events[-2:] == ["close-target", "arm-recovery"]
    assert len(client.armed) == 1
    assert len(provider.retained) == 1
    assert provider.destroyed == []

    session_id, recovery_token = client.armed[0]
    assert session_id == "recoverable123"
    assert recovery_token != "bootstrap"
    assert len(recovery_token) >= 32

    recovery_path = Path(environment.failure_capsule()["recovery_credentials_path"])
    recovery = json.loads(recovery_path.read_text(encoding="utf-8"))
    assert recovery["session_id"] == "recoverable123"
    assert recovery["token"] == recovery_token

    # Caller-visible/report metadata points to the host-only recovery file but
    # never serializes the bearer itself.
    failure = environment.failure_capsule()
    assert failure["recovery_credentials_path"] == str(recovery_path)
    assert "token" not in failure

    manifest = json.loads((provider.root / "failure-capsule.json").read_text(encoding="utf-8"))
    assert manifest["recovery_credentials_path"] == str(recovery_path)
    assert recovery_token not in json.dumps(manifest)


def test_recovery_arm_failure_preserves_live_capsule_instead_of_powering_off(tmp_path):
    environment, provider, client = _recovery_environment(tmp_path, fail_arm=True)
    environment.record_failure("assertion failed")

    with pytest.raises(CapsuleError, match="recovery credentials could not be provisioned"):
        environment.close()

    assert client.events[-2:] == ["close-target", "arm-recovery"]
    assert provider.retained == []
    assert provider.destroyed == []
    assert environment._handle is not None
    error = environment.failure_capsule_error()
    assert error["status"] == "recovery_provision_failed"
    assert "unrestartable" in error["recovery"]
    assert not (provider.root / "recovery-control.json").exists()
