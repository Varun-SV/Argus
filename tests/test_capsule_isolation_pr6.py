from __future__ import annotations

import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from argus.capsule.base import CapsuleError, CapsuleHandle, CapsuleProvider, CapsuleRequest, CapsuleSettings
from argus.capsule.guest import CapsuleGuestError
from argus.capsule.hyperv_isolated import IsolatedHyperVProvider
from argus.capsule.secure_client import SecureGuestAgentClient
from argus.capsule.secure_guest_agent import SecureGuestAgentServer, _consume_tls_private_key
from argus.execution import SecureCapsuleExecutionEnvironment, create_execution_environment


def test_secure_client_rejects_plain_http_by_default():
    with pytest.raises(CapsuleGuestError, match="plain HTTP Capsule control is disabled"):
        SecureGuestAgentClient("http://127.0.0.1:8765", "bootstrap")


def test_secure_client_requires_pinned_ca_for_https(tmp_path):
    with pytest.raises(CapsuleGuestError, match="guest_ca_cert"):
        SecureGuestAgentClient(
            "https://10.0.0.8:8765",
            "bootstrap",
            ca_cert_path=str(tmp_path / "missing.pem"),
        )


def test_secure_client_allows_explicit_legacy_http():
    client = SecureGuestAgentClient(
        "http://127.0.0.1:8765",
        "bootstrap",
        allow_insecure_http=True,
    )
    assert client.transport_secure is False


def test_tls_private_key_is_consumed_from_session_disk(tmp_path):
    key = tmp_path / "guest-key.pem"
    key.write_text("private-key-material", encoding="utf-8")
    _consume_tls_private_key(str(key))
    assert not key.exists()


def test_tls_private_key_symlink_is_rejected(tmp_path):
    actual = tmp_path / "actual-key.pem"
    actual.write_text("private-key-material", encoding="utf-8")
    link = tmp_path / "guest-key.pem"
    try:
        link.symlink_to(actual)
    except OSError:
        pytest.skip("symlink creation is unavailable on this runner")
    with pytest.raises(Exception, match="cannot be a symlink"):
        _consume_tls_private_key(str(link))
    assert actual.exists()


def _start_secure_loopback_server(token: str = "bootstrap-secret"):
    server = SecureGuestAgentServer(("127.0.0.1", 0), token)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_address[1]}"
    return server, thread, endpoint


def test_rotated_bearer_replaces_bootstrap_and_binds_file_session():
    server, thread, endpoint = _start_secure_loopback_server()
    bootstrap = SecureGuestAgentClient(
        endpoint,
        "bootstrap-secret",
        allow_insecure_http=True,
        timeout_seconds=2,
    )
    rotated = "s" * 48
    try:
        assert bootstrap.health()["auth_session_id"] == ""
        bootstrap.rotate_session_token("capsule123", rotated)
        assert bootstrap.token == rotated
        assert bootstrap.health()["auth_session_id"] == "capsule123"

        old = SecureGuestAgentClient(
            endpoint,
            "bootstrap-secret",
            allow_insecure_http=True,
            timeout_seconds=2,
        )
        with pytest.raises(CapsuleGuestError, match="401"):
            old.health()

        with pytest.raises(CapsuleGuestError, match="does not match"):
            bootstrap.begin_files("other-session")
        assert bootstrap.begin_files("capsule123")["workspace"]

        with pytest.raises(CapsuleGuestError, match="already been rotated"):
            bootstrap.rotate_session_token("capsule123", "t" * 48)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_rotation_recovers_when_success_response_is_lost():
    server, thread, endpoint = _start_secure_loopback_server()
    lost = {"done": False}

    def lossy_opener(request, timeout):
        response = urllib.request.urlopen(request, timeout=timeout)
        if request.full_url.endswith("/v1/auth/rotate") and not lost["done"]:
            lost["done"] = True
            # The guest has already authenticated and committed the new token.
            # Simulate losing the response before GuestAgentClient can read it.
            response.close()
            raise urllib.error.URLError("rotation response lost")
        return response

    client = SecureGuestAgentClient(
        endpoint,
        "bootstrap-secret",
        allow_insecure_http=True,
        timeout_seconds=2,
        opener=lossy_opener,
    )
    proposed = "r" * 48
    try:
        client.rotate_session_token("recover123", proposed)
        assert lost["done"] is True
        assert client.token == proposed
        assert client.health()["auth_session_id"] == "recover123"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_capsule_factory_uses_secure_environment_by_default():
    environment = create_execution_environment(
        "cli",
        environment_type="capsule",
        capsule_config={"guest_token": "bootstrap"},
    )
    assert isinstance(environment, SecureCapsuleExecutionEnvironment)
    assert environment.settings.guest_transport == "https"
    assert environment.settings.network_mode == "host_only"
    assert environment.settings.rotate_session_token is True
    assert environment.settings.allow_insecure_http is False


def _isolated_settings(tmp_path: Path, **overrides) -> CapsuleSettings:
    image = tmp_path / "golden.vhdx"
    image.write_bytes(b"golden")
    ca = tmp_path / "guest-ca.pem"
    ca.write_text("test trust anchor", encoding="utf-8")
    values = dict(
        image=str(image),
        switch_name="Argus Internal",
        vm_root=str(tmp_path / "sessions"),
        guest_token="bootstrap-secret",
        guest_ca_cert=str(ca),
        boot_timeout_seconds=2,
    )
    values.update(overrides)
    return CapsuleSettings(**values)


def _isolated_runner(calls: list[str], *, fail_acl: bool = False, host_ip: str = "192.168.100.1"):
    def runner(script: str, timeout: float) -> str:
        calls.append(script)
        if "SwitchType.ToString()" in script:
            return "Internal"
        if "Get-NetIPAddress" in script:
            return host_ip
        if "Add-VMNetworkAdapterExtendedAcl" in script:
            if fail_acl:
                raise CapsuleError("ACL install failed")
            return ""
        if "Get-VMNetworkAdapter" in script and ".IPAddresses" in script:
            return "192.168.100.20"
        return ""
    return runner


def test_hyperv_isolation_is_installed_before_vm_boot(tmp_path):
    calls: list[str] = []
    provider = IsolatedHyperVProvider(runner=_isolated_runner(calls))
    settings = _isolated_settings(
        tmp_path,
        network_mode="allowlist",
        egress_allowlist=("10.20.30.0/24",),
    )

    handle = provider.create(CapsuleRequest("isolated123", "cli", settings))
    assert handle.transport == "https"
    assert handle.address == "192.168.100.20"

    acl_index = next(i for i, script in enumerate(calls) if "Add-VMNetworkAdapterExtendedAcl" in script)
    start_index = next(i for i, script in enumerate(calls) if "Start-VM -Name" in script)
    gsi_index = next(i for i, script in enumerate(calls) if "Guest Service Interface" in script)
    assert gsi_index < acl_index < start_index

    acl = calls[acl_index]
    assert "192.168.100.1" in acl
    assert "8765" in acl
    assert "10.20.30.0/24" in acl
    assert "-Direction Inbound" in acl
    assert "-Direction Outbound" in acl
    assert "-Action Deny" in acl
    assert "-LocalPort 68" in acl
    assert "-RemotePort 67" in acl
    assert "-Stateful $true" in acl

    provider.destroy(handle)


def test_host_only_policy_has_no_declared_egress_allowlist(tmp_path):
    calls: list[str] = []
    provider = IsolatedHyperVProvider(runner=_isolated_runner(calls))
    settings = _isolated_settings(tmp_path)
    handle = provider.create(CapsuleRequest("hostonly123", "cli", settings))

    acl = next(script for script in calls if "Add-VMNetworkAdapterExtendedAcl" in script)
    assert "10.20.30.0/24" not in acl
    assert acl.count("-Action Deny") == 2
    provider.destroy(handle)


@pytest.mark.parametrize(
    "overrides,match",
    [
        ({"guest_transport": "http"}, "plain HTTP Capsule control is disabled"),
        ({"network_mode": "host_only", "egress_allowlist": ("10.0.0.0/8",)}, "requires network_mode=allowlist"),
        ({"network_mode": "allowlist", "egress_allowlist": ("not-a-cidr",)}, "invalid Capsule egress CIDR"),
        ({"network_mode": "allowlist", "egress_allowlist": ("2001:db8::/32",)}, "IPv4 CIDRs only"),
        ({"network_mode": "allowlist", "egress_allowlist": ("224.0.0.0/4",)}, "multicast egress"),
    ],
)
def test_isolation_policy_rejects_unsafe_configuration_before_allocation(tmp_path, overrides, match):
    calls: list[str] = []
    provider = IsolatedHyperVProvider(runner=_isolated_runner(calls))
    settings = _isolated_settings(tmp_path, **overrides)
    with pytest.raises(CapsuleError, match=match):
        provider.create(CapsuleRequest("reject123", "cli", settings))
    assert not any("New-VHD" in script for script in calls)


def test_network_isolation_failure_rolls_back_vm_and_storage(tmp_path):
    calls: list[str] = []
    provider = IsolatedHyperVProvider(runner=_isolated_runner(calls, fail_acl=True))
    settings = _isolated_settings(tmp_path)
    root = Path(settings.vm_root) / "rollback123"

    with pytest.raises(CapsuleError, match="ACL install failed"):
        provider.create(CapsuleRequest("rollback123", "cli", settings))

    assert any("Remove-VM" in script for script in calls)
    assert not root.exists()
    assert not any("Start-VM -Name" in script for script in calls)


def test_missing_management_switch_address_fails_closed_and_rolls_back(tmp_path):
    calls: list[str] = []
    provider = IsolatedHyperVProvider(runner=_isolated_runner(calls, host_ip=""))
    settings = _isolated_settings(tmp_path)

    with pytest.raises(CapsuleError, match="no usable management-OS IPv4"):
        provider.create(CapsuleRequest("nohostip123", "cli", settings))

    assert any("Remove-VM" in script for script in calls)
    assert not any("Start-VM -Name" in script for script in calls)


class _FakeSecureProvider(CapsuleProvider):
    provider_name = "hyperv"

    def __init__(self, root: Path):
        self.root = root
        self.destroyed = []

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

    def destroy(self, handle: CapsuleHandle) -> None:
        self.destroyed.append(handle.session_id)


class _FakeSecureClient:
    def __init__(self, endpoint, token, **kwargs):
        self.endpoint = endpoint
        self.token = token
        self.kwargs = kwargs
        self.rotations = []
        self.closed = 0

    def wait_until_ready(self, timeout_seconds):
        return None

    def rotate_session_token(self, session_id, token):
        self.rotations.append((session_id, token))
        self.token = token

    def close_session(self):
        self.closed += 1


def test_secure_environment_rotates_bootstrap_before_exposing_adapter(tmp_path):
    provider = _FakeSecureProvider(tmp_path / "capsule")
    clients = []

    def factory(*args, **kwargs):
        client = _FakeSecureClient(*args, **kwargs)
        clients.append(client)
        return client

    settings = CapsuleSettings(
        image="unused",
        switch_name="unused",
        guest_token="bootstrap",
        guest_transport="https",
        guest_ca_cert=str(tmp_path / "guest-ca.pem"),
    )
    environment = SecureCapsuleExecutionEnvironment(
        "cli",
        settings,
        provider=provider,
        client_factory=factory,
        session_id="secureenv123",
    )

    environment.prepare()
    assert environment._prepared is True
    assert len(clients) == 1
    assert clients[0].endpoint == "https://10.0.0.8:8765"
    assert clients[0].rotations
    session_id, token = clients[0].rotations[0]
    assert session_id == "secureenv123"
    assert token != "bootstrap"
    assert len(token) >= 32
    assert clients[0].kwargs["ca_cert_path"] == settings.guest_ca_cert
    environment.close()
    assert provider.destroyed == ["secureenv123"]
