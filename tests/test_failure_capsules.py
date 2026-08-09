from __future__ import annotations

import json
from pathlib import Path

from argus.adapters.base import Observation
from argus.capsule.base import (
    CapsuleHandle,
    CapsuleProvider,
    CapsuleRequest,
    CapsuleSettings,
    FailureCapsule,
)
from argus.capsule.hyperv import HyperVProvider
from argus.engine.runner import run_test
from argus.engine.spec import parse_spec
from argus.execution import CapsuleExecutionEnvironment
from tests.conftest import FakeProvider


_CAPABILITIES = {
    "actions": {"wait": {}, "done": {}},
    "notes": ["failure capsule fake guest"],
}


class RetentionProvider(CapsuleProvider):
    provider_name = "fake-retention"

    def __init__(self, root: Path):
        self.root = root
        self.created = []
        self.retained = []
        self.destroyed = []

    def create(self, request: CapsuleRequest) -> CapsuleHandle:
        self.created.append(request)
        self.root.mkdir(parents=True, exist_ok=True)
        return CapsuleHandle(
            session_id=request.session_id,
            provider=self.provider_name,
            vm_name="Argus-failure-test",
            root_dir=str(self.root),
            address="10.10.0.2",
            guest_port=request.settings.guest_port,
        )

    def retain_failure(self, handle: CapsuleHandle, reason: str) -> FailureCapsule:
        self.retained.append((handle, reason))
        return FailureCapsule(
            failure_id=handle.session_id,
            session_id=handle.session_id,
            provider=self.provider_name,
            vm_name=handle.vm_name,
            root_dir=handle.root_dir,
            reason=reason,
            retained_at="2026-08-09T00:00:00+00:00",
            vm_state="Saved",
        )

    def destroy(self, handle: CapsuleHandle) -> None:
        self.destroyed.append(handle)


class RetentionClient:
    def __init__(self):
        self.ready = False
        self.launched = None
        self.closed = 0

    def wait_until_ready(self, timeout_seconds: float) -> None:
        self.ready = True

    def launch(self, adapter_type: str, target: str, input_mode: str) -> dict:
        self.launched = (adapter_type, target, input_mode)
        return {"ok": True, "capabilities": _CAPABILITIES}

    def observe(self, include_screenshot: bool = True) -> Observation:
        return Observation(
            window_title="Failure Capsule App",
            process_alive=True,
            action_capabilities=_CAPABILITIES,
        )

    def act(self, action: dict) -> str:
        return "acted"

    def close_session(self) -> None:
        self.closed += 1


def _environment(tmp_path: Path, *, retain_on_failure: bool):
    provider = RetentionProvider(tmp_path / "capsule")
    client = RetentionClient()
    settings = CapsuleSettings(
        provider="hyperv",
        image="unused.vhdx",
        switch_name="Argus Internal",
        guest_token="secret",
        retain_on_failure=retain_on_failure,
    )
    environment = CapsuleExecutionEnvironment(
        "desktop-gui",
        settings,
        provider=provider,
        client_factory=lambda *args, **kwargs: client,
        session_id="failure123",
    )
    return environment, provider, client


def test_failed_run_retains_capsule_before_guest_teardown(tmp_path):
    environment, provider, client = _environment(tmp_path, retain_on_failure=True)
    spec = parse_spec(
        """\
name: retain failed capsule
target:
  adapter: desktop-gui
  launch: fake.exe
steps:
  - assert:
      process_running: false
teardown:
  - close
"""
    )

    result = run_test(spec, FakeProvider([]), environment)

    assert result.status == "fail"
    assert len(provider.retained) == 1
    assert provider.destroyed == []
    assert client.closed == 0
    assert result.failure_capsule is not None
    assert result.failure_capsule["failure_id"] == "failure123"
    assert result.failure_capsule["vm_state"] == "Saved"
    assert "process_running" in result.failure_capsule["reason"]


def test_passing_run_still_destroys_ephemeral_capsule(tmp_path):
    environment, provider, client = _environment(tmp_path, retain_on_failure=True)
    spec = parse_spec(
        """\
name: clean passing capsule
target:
  adapter: desktop-gui
  launch: fake.exe
steps:
  - assert:
      process_running: true
"""
    )

    result = run_test(spec, FakeProvider([]), environment)

    assert result.status == "pass"
    assert provider.retained == []
    assert len(provider.destroyed) == 1
    assert client.closed == 1
    assert result.failure_capsule is None


def test_disabled_retention_keeps_existing_cleanup_behavior(tmp_path):
    environment, provider, client = _environment(tmp_path, retain_on_failure=False)
    environment.launch("fake.exe")
    environment.record_failure("should not retain")
    environment.close()

    assert provider.retained == []
    assert len(provider.destroyed) == 1
    assert client.closed == 1
    assert environment.failure_capsule() is None


def test_hyperv_retention_saves_vm_and_writes_manifest(tmp_path):
    root = tmp_path / "session"
    root.mkdir()
    calls = []

    def runner(script: str, timeout: float) -> str:
        calls.append(script)
        if "Get-VM" in script and "Save-VM" in script:
            return "Saved"
        return ""

    provider = HyperVProvider(runner=runner)
    handle = CapsuleHandle(
        session_id="retain456",
        provider="hyperv",
        vm_name="Argus-retain456",
        root_dir=str(root),
        address="10.0.0.8",
        guest_port=8765,
    )

    retained = provider.retain_failure(handle, "assertion failed")

    assert retained.failure_id == "retain456"
    assert retained.vm_state == "Saved"
    assert any("Save-VM" in call for call in calls)
    manifest = json.loads((root / "failure-capsule.json").read_text(encoding="utf-8"))
    assert manifest["vm_name"] == "Argus-retain456"
    assert manifest["reason"] == "assertion failed"


def test_retain_on_failure_uses_strict_boolean_parsing():
    assert CapsuleSettings.from_mapping({"retain_on_failure": "false"}).retain_on_failure is False
    assert CapsuleSettings.from_mapping({"retain_on_failure": "true"}).retain_on_failure is True
