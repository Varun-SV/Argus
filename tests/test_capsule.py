from __future__ import annotations

from pathlib import Path

import pytest

from argus.adapters.base import AdapterError, Observation
from argus.capsule.base import (
    CapsuleError,
    CapsuleHandle,
    CapsuleProvider,
    CapsuleRequest,
    CapsuleSettings,
)
from argus.capsule.guest import CapsuleGuestError
from argus.capsule.hyperv import HyperVProvider, _ps_quote
from argus.config import load_config
from argus.engine.runner import run_test
from argus.engine.spec import parse_spec
from argus.execution import CapsuleExecutionEnvironment
from tests.conftest import FakeProvider


_CAPABILITIES = {
    "actions": {
        "type": {"element_id": "optional"},
        "key": {},
        "wait": {},
        "done": {},
    },
    "notes": ["fake Capsule guest"],
}


class FakeCapsuleProvider(CapsuleProvider):
    provider_name = "fake-hypervisor"

    def __init__(self, root: Path):
        self.root = root
        self.created = []
        self.destroyed = []

    def create(self, request: CapsuleRequest) -> CapsuleHandle:
        self.created.append(request)
        return CapsuleHandle(
            session_id=request.session_id,
            provider=self.provider_name,
            vm_name="Argus-test",
            root_dir=str(self.root),
            address="10.10.0.2",
            guest_port=request.settings.guest_port,
        )

    def destroy(self, handle: CapsuleHandle) -> None:
        self.destroyed.append(handle)


class FakeGuestClient:
    def __init__(self, endpoint="", token="", timeout_seconds=15.0):
        self.endpoint = endpoint
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.ready = False
        self.launched = None
        self.closed = 0
        self.actions = []
        self.text = ""

    def wait_until_ready(self, timeout_seconds: float) -> None:
        self.ready = True

    def launch(self, adapter_type: str, target: str, input_mode: str) -> dict:
        self.launched = (adapter_type, target, input_mode)
        return {"ok": True, "capabilities": _CAPABILITIES}

    def observe(self, include_screenshot: bool = True) -> Observation:
        return Observation(
            window_title="Capsule Fake App",
            process_alive=True,
            screenshot_png=b"PNG" if include_screenshot else None,
            action_capabilities=_CAPABILITIES,
        )

    def act(self, action: dict) -> str:
        self.actions.append(action)
        if action.get("action") == "type":
            self.text = str(action.get("text", ""))
            return f"typed {self.text!r}"
        return "acted"

    def close_session(self) -> None:
        self.closed += 1


def _environment(tmp_path, client=None):
    provider = FakeCapsuleProvider(tmp_path / "capsule")
    client = client or FakeGuestClient()
    settings = CapsuleSettings(
        provider="hyperv",
        image="unused-in-fake-provider.vhdx",
        switch_name="Argus Internal",
        guest_token="secret",
        guest_input_mode="physical",
    )
    environment = CapsuleExecutionEnvironment(
        "desktop-gui",
        settings,
        provider=provider,
        client_factory=lambda *args, **kwargs: client,
        session_id="session123",
    )
    return environment, provider, client


def test_capsule_environment_routes_actions_inside_guest_and_destroys_vm(tmp_path):
    environment, provider, client = _environment(tmp_path)

    environment.launch("fake.exe")

    assert environment._prepared is True
    assert client.ready is True
    assert client.launched == ("desktop-gui", "fake.exe", "physical")
    assert provider.created[0].session_id == "session123"
    assert environment.info().environment_type == "capsule"
    assert environment.info().isolated is True
    assert environment.info().location == "fake-hypervisor"

    note = environment.act({"action": "type", "text": "hello", "element_id": 1})
    assert note == "typed 'hello'"
    assert client.text == "hello"

    environment.close()
    assert client.closed == 1
    assert len(provider.destroyed) == 1
    assert environment._prepared is False


def test_capsule_host_policy_blocks_escape_before_guest_dispatch(tmp_path):
    environment, _, client = _environment(tmp_path)
    environment.launch("fake.exe")

    with pytest.raises(AdapterError, match="system-level key combination"):
        environment.act({"action": "key", "keys": "alt+f4"})

    assert client.actions == []
    environment.close()


def test_capsule_launch_failure_rolls_back_vm(tmp_path):
    class FailingClient(FakeGuestClient):
        def launch(self, adapter_type: str, target: str, input_mode: str) -> dict:
            raise CapsuleGuestError("guest target refused to launch")

    environment, provider, _ = _environment(tmp_path, FailingClient())

    with pytest.raises(CapsuleGuestError, match="refused to launch"):
        environment.launch("broken.exe")

    assert len(provider.destroyed) == 1
    assert environment._prepared is False
    assert environment._handle is None


def test_capsule_agent_readiness_failure_rolls_back_vm(tmp_path):
    class UnreadyClient(FakeGuestClient):
        def wait_until_ready(self, timeout_seconds: float) -> None:
            raise CapsuleGuestError("agent never became ready")

    environment, provider, _ = _environment(tmp_path, UnreadyClient())

    with pytest.raises(CapsuleGuestError, match="never became ready"):
        environment.prepare()

    assert len(provider.destroyed) == 1
    assert environment._prepared is False


def test_run_result_records_capsule_isolation_metadata(tmp_path):
    environment, _, _ = _environment(tmp_path)
    spec = parse_spec(
        """\
name: capsule audit
target:
  adapter: desktop-gui
  launch: fake.exe
steps:
  - assert:
      process_running: true
teardown:
  - close
"""
    )
    result = run_test(spec, FakeProvider([]), environment)

    assert result.status == "pass"
    assert result.environment_type == "capsule"
    assert result.isolated is True
    assert result.location == "fake-hypervisor"
    data = result.to_dict()
    assert data["environment_type"] == "capsule"
    assert data["isolated"] is True


def test_config_can_select_capsule_without_allocating_vm(tmp_path, monkeypatch):
    argus_dir = tmp_path / ".argus"
    argus_dir.mkdir()
    (argus_dir / "config.yaml").write_text(
        """
provider: ollama
execution:
  environment: capsule
  capsule:
    provider: hyperv
    image: C:\\Argus\\images\\win11.vhdx
    switch_name: Argus Internal
    memory_mb: 6144
    cpu_count: 3
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("ARGUS_CAPSULE_GUEST_TOKEN", "super-secret")

    cfg = load_config(tmp_path)
    environment = cfg.make_execution_environment("cli")

    assert isinstance(environment, CapsuleExecutionEnvironment)
    assert environment.settings.image.endswith("win11.vhdx")
    assert environment.settings.memory_mb == 6144
    assert environment.settings.cpu_count == 3
    assert environment.settings.guest_token == "super-secret"
    assert environment._prepared is False


def test_powershell_quote_escapes_single_quotes():
    assert _ps_quote("C:\\VMs\\Varun's image.vhdx") == "'C:\\VMs\\Varun''s image.vhdx'"


def test_hyperv_provider_uses_differencing_disk_and_internal_switch(tmp_path):
    image = tmp_path / "golden.vhdx"
    image.write_bytes(b"golden")
    calls = []

    def runner(script: str, timeout: float) -> str:
        calls.append(script)
        if "Get-VMSwitch" in script:
            return "Internal"
        if "Get-VMNetworkAdapter" in script:
            return "10.0.0.8"
        return ""

    provider = HyperVProvider(runner=runner)
    settings = CapsuleSettings(
        image=str(image),
        switch_name="Argus Internal",
        vm_root=str(tmp_path / "sessions"),
        guest_token="secret",
        guest_address="10.0.0.8",
    )
    handle = provider.create(CapsuleRequest("abc123", "desktop-gui", settings))

    assert handle.address == "10.0.0.8"
    assert any("New-VHD" in call and "-Differencing" in call and "-ParentPath" in call for call in calls)
    assert any("New-VM" in call and "-Generation 2" in call for call in calls)
    assert any("AutomaticCheckpointsEnabled $false" in call for call in calls)
    assert any("Get-VMNetworkAdapter" in call for call in calls)
    assert Path(handle.root_dir).exists()

    provider.destroy(handle)
    assert not Path(handle.root_dir).exists()
    assert any("Remove-VM" in call for call in calls)


def test_hyperv_provider_discovers_guest_ipv4(tmp_path):
    image = tmp_path / "golden.vhdx"
    image.write_bytes(b"golden")

    def runner(script: str, timeout: float) -> str:
        if "Get-VMSwitch" in script:
            return "Internal"
        if "Get-VMNetworkAdapter" in script:
            return "10.0.0.9"
        return ""

    provider = HyperVProvider(runner=runner)
    settings = CapsuleSettings(
        image=str(image),
        switch_name="Argus Internal",
        vm_root=str(tmp_path / "sessions"),
        guest_token="secret",
        boot_timeout_seconds=2,
    )
    handle = provider.create(CapsuleRequest("iptest", "desktop-gui", settings))

    assert handle.address == "10.0.0.9"
    provider.destroy(handle)


def test_hyperv_provider_rejects_external_switch_by_default(tmp_path):
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

    with pytest.raises(CapsuleError, match="External"):
        provider.create(CapsuleRequest("external", "desktop-gui", settings))


def test_hyperv_partial_setup_failure_removes_vm_and_session_storage(tmp_path):
    image = tmp_path / "golden.vhdx"
    image.write_bytes(b"golden")
    calls = []

    def runner(script: str, timeout: float) -> str:
        calls.append(script)
        if "Get-VMSwitch" in script:
            return "Internal"
        if "Set-VMProcessor" in script:
            raise CapsuleError("processor configuration failed")
        return ""

    provider = HyperVProvider(runner=runner)
    settings = CapsuleSettings(
        image=str(image),
        switch_name="Argus Internal",
        vm_root=str(tmp_path / "sessions"),
        guest_token="secret",
    )

    with pytest.raises(CapsuleError, match="processor configuration failed"):
        provider.create(CapsuleRequest("partial", "desktop-gui", settings))

    assert not (tmp_path / "sessions" / "partial").exists()
    assert any("Remove-VM" in call for call in calls)
