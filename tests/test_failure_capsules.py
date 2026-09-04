from __future__ import annotations

import json
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


class FailingRetentionProvider(RetentionProvider):
    def retain_failure(self, handle: CapsuleHandle, reason: str) -> FailureCapsule:
        self.retained.append((handle, reason))
        raise CapsuleError("manifest persistence failed")


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


class FailingLaunchClient(RetentionClient):
    def launch(self, adapter_type: str, target: str, input_mode: str) -> dict:
        self.launched = (adapter_type, target, input_mode)
        raise CapsuleError("guest application launch failed")


class FailingObserveClient(RetentionClient):
    def observe(self, include_screenshot: bool = True) -> Observation:
        raise CapsuleError("guest observation failed")


class ExhaustedBudget:
    def exhausted(self):
        return "time budget exhausted"


class ExhaustsOnTeardownBudget:
    def __init__(self):
        self.calls = 0

    def exhausted(self):
        self.calls += 1
        if self.calls == 1:
            return None
        return "time budget exhausted"


def _environment(
    tmp_path: Path,
    *,
    retain_on_failure: bool,
    provider=None,
    client=None,
):
    provider = provider or RetentionProvider(tmp_path / "capsule")
    client = client or RetentionClient()
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
    assert result.failure_capsule_error is None


def test_target_launch_error_retains_prepared_capsule(tmp_path):
    client = FailingLaunchClient()
    environment, provider, client = _environment(
        tmp_path,
        retain_on_failure=True,
        client=client,
    )
    spec = parse_spec(
        """\
name: retain target launch failure
target:
  adapter: desktop-gui
  launch: missing.exe
steps:
  - assert:
      process_running: true
"""
    )

    result = run_test(spec, FakeProvider([]), environment)

    assert result.status == "error"
    assert "guest application launch failed" in (result.error or "")
    assert client.ready is True
    assert client.launched == ("desktop-gui", "missing.exe", "physical")
    assert client.closed == 0
    assert len(provider.retained) == 1
    assert "target launch failed" in provider.retained[0][1]
    assert provider.destroyed == []
    assert result.failure_capsule is not None
    assert result.failure_capsule["failure_id"] == "failure123"
    assert result.failure_capsule_error is None


def test_target_launch_retention_failure_reports_recovery_without_destroy(tmp_path):
    provider = FailingRetentionProvider(tmp_path / "capsule")
    client = FailingLaunchClient()
    environment, provider, client = _environment(
        tmp_path,
        retain_on_failure=True,
        provider=provider,
        client=client,
    )
    spec = parse_spec(
        """\
name: target launch retention failure
target:
  adapter: desktop-gui
  launch: missing.exe
steps:
  - assert:
      process_running: true
"""
    )

    result = run_test(spec, FakeProvider([]), environment)

    assert result.status == "error"
    assert "guest application launch failed" in (result.error or "")
    assert len(provider.retained) == 1
    assert provider.destroyed == []
    assert client.closed == 0
    assert result.failure_capsule is None
    assert result.failure_capsule_error is not None
    assert result.failure_capsule_error["status"] == "retention_failed"
    assert result.failure_capsule_error["vm_name"] == "Argus-failure-test"
    assert result.failure_capsule_error["root_dir"] == str(provider.root)
    assert "manifest persistence failed" in result.failure_capsule_error["error"]


def test_unexpected_execution_exception_retains_before_final_close(tmp_path):
    # Regression for review P1/P2: no StepResult exists when observe() raises,
    # so the runner must arm retention before final close and return the retained
    # metadata through RunResult rather than throwing past the reporting layer.
    client = FailingObserveClient()
    environment, provider, client = _environment(
        tmp_path,
        retain_on_failure=True,
        client=client,
    )
    spec = parse_spec(
        """\
name: retain unexpected observation failure
target:
  adapter: desktop-gui
  launch: fake.exe
steps:
  - assert:
      process_running: true
"""
    )

    result = run_test(spec, FakeProvider([]), environment)

    assert result.status == "error"
    assert "CapsuleError: guest observation failed" in (result.error or "")
    assert len(provider.retained) == 1
    assert "run execution error" in provider.retained[0][1]
    assert "guest observation failed" in provider.retained[0][1]
    assert provider.destroyed == []
    assert client.closed == 0
    assert result.failure_capsule is not None
    assert result.failure_capsule["failure_id"] == "failure123"
    assert result.failure_capsule_error is None


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
    assert result.failure_capsule_error is None


def test_disabled_retention_keeps_existing_cleanup_behavior(tmp_path):
    environment, provider, client = _environment(tmp_path, retain_on_failure=False)
    environment.launch("fake.exe")
    environment.record_failure("should not retain")
    environment.close()

    assert provider.retained == []
    assert len(provider.destroyed) == 1
    assert client.closed == 1
    assert environment.failure_capsule() is None


def test_retention_failure_is_reported_with_recovery_coordinates(tmp_path):
    provider = FailingRetentionProvider(tmp_path / "capsule")
    environment, provider, client = _environment(
        tmp_path,
        retain_on_failure=True,
        provider=provider,
    )
    spec = parse_spec(
        """\
name: retention failure is visible
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

    # The deterministic assertion failed, but retention itself did not become
    # canonical evidence and the target was intentionally left untouched for
    # recovery. ATES therefore reports an infrastructure/lifecycle error rather
    # than presenting the evaluation as a reliable deterministic failure.
    assert result.status == "error"
    assert len(provider.retained) == 1
    assert result.failure_capsule is None
    assert result.failure_capsule_error is not None
    assert result.failure_capsule_error["status"] == "retention_failed"
    assert result.failure_capsule_error["vm_name"] == "Argus-failure-test"
    assert result.failure_capsule_error["root_dir"] == str(provider.root)
    assert "manifest persistence failed" in result.failure_capsule_error["error"]
    assert provider.destroyed == []
    assert client.closed == 0

    result_path = result.save(tmp_path)
    report = (result_path.parent / "report.md").read_text(encoding="utf-8")
    assert "Failure Capsule retention error" in report
    assert "Argus-failure-test" in report
    assert str(provider.root) in report
    assert "preserved" in report


def test_budget_exhaustion_records_failure_and_retains_before_teardown(tmp_path):
    environment, provider, client = _environment(tmp_path, retain_on_failure=True)
    spec = parse_spec(
        """\
name: retain budget failure
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

    result = run_test(spec, FakeProvider([]), environment, budget=ExhaustedBudget())

    assert result.status == "fail"
    assert result.steps[0].status == "skipped"
    assert "time budget exhausted" in (result.steps[0].note or "")
    assert len(provider.retained) == 1
    assert "time budget exhausted" in provider.retained[0][1]
    assert provider.destroyed == []
    assert client.closed == 0
    assert result.failure_capsule is not None


def test_budget_exhaustion_detected_when_entering_teardown(tmp_path):
    # Regression for review P2: the real step starts within budget and passes,
    # then the budget reports exhaustion only at the teardown boundary.
    environment, provider, client = _environment(tmp_path, retain_on_failure=True)
    budget = ExhaustsOnTeardownBudget()
    spec = parse_spec(
        """\
name: terminal budget failure
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

    result = run_test(spec, FakeProvider([]), environment, budget=budget)

    assert budget.calls >= 2
    assert result.status == "fail"
    assert result.steps[0].status == "pass"
    assert result.steps[1].status == "pass"
    assert "time budget exhausted" in (result.steps[1].note or "")
    assert len(provider.retained) == 1
    assert "run budget exhausted before teardown" in provider.retained[0][1]
    assert provider.destroyed == []
    assert client.closed == 0
    assert result.failure_capsule is not None


def test_hyperv_retention_powers_off_without_persisting_guest_ram(tmp_path):
    root = tmp_path / "session"
    root.mkdir()
    calls = []

    def runner(script: str, timeout: float) -> str:
        calls.append(script)
        if "Get-VM" in script and "Stop-VM" in script:
            return "Off"
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
    assert retained.vm_state == "Off"
    assert any("Stop-VM" in call and "-TurnOff" in call for call in calls)
    assert not any("Save-VM" in call for call in calls)
    manifest = json.loads((root / "failure-capsule.json").read_text(encoding="utf-8"))
    assert manifest["vm_name"] == "Argus-retain456"
    assert manifest["reason"] == "assertion failed"
    assert manifest["vm_state"] == "Off"


def test_retain_on_failure_uses_strict_boolean_parsing():
    assert CapsuleSettings.from_mapping({"retain_on_failure": "false"}).retain_on_failure is False
    assert CapsuleSettings.from_mapping({"retain_on_failure": "true"}).retain_on_failure is True
