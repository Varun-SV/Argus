from __future__ import annotations

from argus.capsule.base import CapsuleError
from argus.engine.runner import run_test
from argus.engine.spec import parse_spec
from tests.conftest import FakeProvider
from tests.test_failure_capsules import (
    FailingRetentionProvider,
    RetentionClient,
    _environment,
)


class FailingCloseClient(RetentionClient):
    def close_session(self) -> None:
        self.closed += 1
        raise CapsuleError("guest close rpc failed")


def _passing_spec(*, explicit_teardown: bool):
    teardown = "\nteardown:\n  - close\n" if explicit_teardown else "\n"
    return parse_spec(
        """\
name: guest close failure
target:
  adapter: desktop-gui
  launch: fake.exe
steps:
  - assert:
      process_running: true
"""
        + teardown
    )


def test_guest_close_failure_retains_before_destroy_on_explicit_teardown(tmp_path):
    client = FailingCloseClient()
    environment, provider, client = _environment(
        tmp_path,
        retain_on_failure=True,
        client=client,
    )

    result = run_test(
        _passing_spec(explicit_teardown=True),
        FakeProvider([]),
        environment,
    )

    assert result.status == "error"
    assert "guest session close failed" in (result.error or "")
    assert "guest close rpc failed" in (result.error or "")
    assert client.closed == 1
    assert len(provider.retained) == 1
    assert "guest session close failed" in provider.retained[0][1]
    assert provider.destroyed == []
    assert result.failure_capsule is not None
    assert result.failure_capsule["failure_id"] == "failure123"
    assert result.failure_capsule_error is None


def test_guest_close_failure_in_final_cleanup_cannot_return_pass(tmp_path):
    client = FailingCloseClient()
    environment, provider, client = _environment(
        tmp_path,
        retain_on_failure=True,
        client=client,
    )

    result = run_test(
        _passing_spec(explicit_teardown=False),
        FakeProvider([]),
        environment,
    )

    assert result.status == "error"
    assert "cleanup failed" in (result.error or "")
    assert "guest session close failed" in (result.error or "")
    assert client.closed == 1
    assert len(provider.retained) == 1
    assert provider.destroyed == []
    assert result.failure_capsule is not None
    assert result.failure_capsule_error is None


def test_guest_close_retention_failure_preserves_recovery_coordinates(tmp_path):
    client = FailingCloseClient()
    provider = FailingRetentionProvider(tmp_path / "capsule")
    environment, provider, client = _environment(
        tmp_path,
        retain_on_failure=True,
        provider=provider,
        client=client,
    )

    result = run_test(
        _passing_spec(explicit_teardown=True),
        FakeProvider([]),
        environment,
    )

    assert result.status == "error"
    assert client.closed == 1
    assert len(provider.retained) == 1
    assert provider.destroyed == []
    assert result.failure_capsule is None
    assert result.failure_capsule_error is not None
    assert result.failure_capsule_error["status"] == "retention_failed"
    assert result.failure_capsule_error["vm_name"] == "Argus-failure-test"
    assert result.failure_capsule_error["root_dir"] == str(provider.root)
    assert "manifest persistence failed" in result.failure_capsule_error["error"]
