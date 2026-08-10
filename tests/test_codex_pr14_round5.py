from __future__ import annotations

import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

from argus.adapters.cli_adapter import CLIAdapter
from argus.adapters.linux_gui import LinuxGUIAdapter
from argus.capsule.base import (
    CapsuleHandle,
    CapsuleProvider,
    CapsuleProviderCapabilities,
    CapsuleRequest,
    CapsuleSettings,
)
from argus.capsule.guest import GuestAdapterProxy, GuestAgentClient
from argus.execution import SecureCapsuleExecutionEnvironment
from argus.execution.base import ExecutionEnvironmentError


class _RecordingGuestClient(GuestAgentClient):
    def __init__(self, guest_path: str):
        super().__init__("http://127.0.0.1:1", "bootstrap")
        self.guest_path = guest_path
        self.session_starts: list[dict] = []

    def _request(self, method: str, path: str, payload=None):
        if path == "/v1/files/stage/commit":
            return {"guest_path": self.guest_path}
        if path == "/v1/session/start":
            self.session_starts.append(dict(payload or {}))
            return {"capabilities": {"actions": {"wait": {}, "done": {}}}}
        return {}


def test_committed_staged_cli_target_is_promoted_to_literal_launch(tmp_path):
    source = tmp_path / "tool.bin"
    source.write_bytes(b"staged executable")
    guest_path = "/tmp/argus-capsule-workspaces/session/app/tool with space"
    client = _RecordingGuestClient(guest_path)

    with source.open("rb") as handle:
        staged = client.stage_open_file(handle, "tool.bin", "app/tool")

    assert staged["guest_path"] == guest_path
    proxy = GuestAdapterProxy(client, "cli", "safe")
    proxy.launch(shlex.quote(guest_path))

    payload = client.session_starts[-1]
    assert payload["target"] == guest_path
    assert payload["literal_target"] is True


def test_arbitrary_cli_command_is_not_promoted_to_literal_launch():
    client = _RecordingGuestClient("/tmp/committed-tool")
    proxy = GuestAdapterProxy(client, "cli", "safe")

    proxy.launch("python -V")

    payload = client.session_starts[-1]
    assert payload["target"] == "python -V"
    assert payload["literal_target"] is False


def test_cli_literal_launch_uses_one_exact_argv_without_shell(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout="ok\n", stderr="", returncode=0)

    monkeypatch.setattr("argus.adapters.cli_adapter.subprocess.run", fake_run)
    adapter = CLIAdapter(shell=True)
    target = "/tmp/Argus Workspace/tool with space"

    adapter.launch_literal(target)

    args, kwargs = calls[-1]
    assert args == [target]
    assert kwargs["shell"] is False


def test_linux_gui_literal_launch_uses_one_exact_argv_without_shell(monkeypatch):
    calls = []

    class FakeProcess:
        pid = 4242

    def fake_popen(command, **kwargs):
        calls.append((command, kwargs))
        return FakeProcess()

    monkeypatch.setattr("argus.adapters.linux_gui.subprocess.Popen", fake_popen)
    monkeypatch.setattr("argus.adapters.linux_gui.time.sleep", lambda _seconds: None)

    adapter = LinuxGUIAdapter(display=":99", auto_xvfb=False)
    target = "/tmp/Argus Workspace/gui tool"
    adapter.launch_literal(target)

    command, kwargs = calls[-1]
    assert command == [target]
    assert kwargs["shell"] is False
    assert adapter._pid == 4242


class _NoGuestOSProvider(CapsuleProvider):
    provider_name = "no-guest-os"
    provider_capabilities = CapsuleProviderCapabilities(
        provider="no-guest-os",
        host_platforms=("windows", "linux"),
        guest_os=(),
        secure_transport=True,
        network_isolation=True,
        explicit_transfers=True,
        failure_retention=True,
        egress_allowlist=True,
    )

    def __init__(self):
        self.created = False

    def create(self, request: CapsuleRequest) -> CapsuleHandle:
        self.created = True
        raise AssertionError("provider.create must not be reached")

    def destroy(self, handle: CapsuleHandle) -> None:
        return None


@pytest.mark.parametrize("configured_guest_os", ["auto", "linux"])
def test_secure_environment_rejects_provider_with_no_guest_os(configured_guest_os):
    provider = _NoGuestOSProvider()
    settings = CapsuleSettings(
        provider=provider.provider_name,
        guest_os=configured_guest_os,
        guest_token="bootstrap",
        guest_transport="http",
        allow_insecure_http=True,
    )

    with pytest.raises(ExecutionEnvironmentError, match="supported guest OS"):
        SecureCapsuleExecutionEnvironment("cli", settings, provider=provider)

    assert provider.created is False
