from __future__ import annotations

from pathlib import Path

from argus.adapters.base import Observation
from argus.capsule.base import (
    CapsuleError,
    CapsuleHandle,
    CapsuleProvider,
    CapsuleRequest,
    CapsuleSettings,
    FailureCapsule,
)
from argus.engine.runner import run_test
from argus.engine.spec import parse_spec
from argus.execution import CapsuleExecutionEnvironment
from tests.conftest import FakeProvider


_CAPABILITIES = {
    "actions": {"wait": {}, "done": {}},
    "notes": ["execution-error result regression"],
}


class _RetentionProvider(CapsuleProvider):
    provider_name = "fake-retention"

    def __init__(self, root: Path, *, fail_retention: bool = False):
        self.root = root
        self.fail_retention = fail_retention
        self.retained = []
        self.destroyed = []

    def create(self, request: CapsuleRequest) -> CapsuleHandle:
        self.root.mkdir(parents=True, exist_ok=True)
        return CapsuleHandle(
            session_id=request.session_id,
            provider=self.provider_name,
            vm_name="Argus-error-result",
            root_dir=str(self.root),
            address="10.10.0.2",
            guest_port=request.settings.guest_port,
        )

    def retain_failure(self, handle: CapsuleHandle, reason: str) -> FailureCapsule:
        self.retained.append((handle, reason))
        if self.fail_retention:
            raise CapsuleError("retention manifest failed")
        return FailureCapsule(
            failure_id=handle.session_id,
            session_id=handle.session_id,
            provider=self.provider_name,
            vm_name=handle.vm_name,
            root_dir=handle.root_dir,
            reason=reason,
            retained_at="2026-08-10T00:00:00+00:00",
            vm_state="Off",
        )

    def destroy(self, handle: CapsuleHandle) -> None:
        self.destroyed.append(handle)


class _FailingObserveClient:
    def __init__(self):
        self.closed = 0

    def wait_until_ready(self, timeout_seconds: float) -> None:
        pass

    def launch(self, adapter_type: str, target: str, input_mode: str) -> dict:
        return {"ok": True, "capabilities": _CAPABILITIES}

    def observe(self, include_screenshot: bool = True) -> Observation:
        raise CapsuleError("guest observation failed")

    def act(self, action: dict) -> str:
        return "acted"

    def close_session(self) -> None:
        self.closed += 1


def _run_observation_error(tmp_path: Path, *, fail_retention: bool = False):
    provider = _RetentionProvider(tmp_path / "capsule", fail_retention=fail_retention)
    client = _FailingObserveClient()
    settings = CapsuleSettings(
        provider="hyperv",
        image="unused.vhdx",
        switch_name="Argus Internal",
        guest_token="secret",
        retain_on_failure=True,
    )
    environment = CapsuleExecutionEnvironment(
        "desktop-gui",
        settings,
        provider=provider,
        client_factory=lambda *args, **kwargs: client,
        session_id="error123",
    )
    spec = parse_spec(
        """\
name: retained execution error result
target:
  adapter: desktop-gui
  launch: fake.exe
steps:
  - assert:
      process_running: true
"""
    )
    result = run_test(spec, FakeProvider([]), environment)
    return result, provider, client


def test_execution_exception_returns_retained_metadata_to_caller(tmp_path):
    result, provider, client = _run_observation_error(tmp_path)

    assert result.status == "error"
    assert "CapsuleError: guest observation failed" in (result.error or "")
    assert len(provider.retained) == 1
    assert provider.destroyed == []
    assert client.closed == 0
    assert result.failure_capsule is not None
    assert result.failure_capsule["failure_id"] == "error123"
    assert result.failure_capsule["vm_name"] == "Argus-error-result"
    assert "run execution error" in result.failure_capsule["reason"]
    assert result.failure_capsule_error is None

    result_path = result.save(tmp_path)
    report = (result_path.parent / "report.md").read_text(encoding="utf-8")
    assert "guest observation failed" in report
    assert "Argus-error-result" in report


def test_execution_exception_returns_recovery_metadata_when_retention_fails(tmp_path):
    result, provider, client = _run_observation_error(tmp_path, fail_retention=True)

    assert result.status == "error"
    assert "CapsuleError: guest observation failed" in (result.error or "")
    assert len(provider.retained) == 1
    assert provider.destroyed == []
    assert client.closed == 0
    assert result.failure_capsule is None
    assert result.failure_capsule_error is not None
    assert result.failure_capsule_error["status"] == "retention_failed"
    assert result.failure_capsule_error["vm_name"] == "Argus-error-result"
    assert result.failure_capsule_error["root_dir"] == str(provider.root)
    assert "retention manifest failed" in result.failure_capsule_error["error"]
