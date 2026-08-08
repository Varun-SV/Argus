import pytest

from argus.adapters import create_adapter
from argus.adapters.base import AdapterError, create_adapter as create_platform_adapter
from argus.execution import (
    ExecutionEnvironmentError,
    LocalExecutionEnvironment,
    create_execution_environment,
)
from tests.conftest import FakeAdapter


def test_public_adapter_factory_routes_sessions_through_local_environment():
    environment = create_adapter("cli")

    assert isinstance(environment, LocalExecutionEnvironment)
    assert environment.environment_type == "local"
    assert environment.type_name == "cli"
    assert environment.isolated is False
    assert environment.location == "host"
    # Existing callers that inspect PolicyAdapter.inner still work through the
    # environment compatibility delegation.
    assert environment.inner.type_name == "cli"


def test_low_level_platform_factory_still_returns_guarded_adapter():
    adapter = create_platform_adapter("cli")

    assert not isinstance(adapter, LocalExecutionEnvironment)
    assert adapter.type_name == "cli"


def test_local_environment_delegates_lifecycle_observation_and_actions():
    raw = FakeAdapter()
    environment = LocalExecutionEnvironment(raw)

    assert environment._prepared is False
    environment.launch("fake.exe")
    assert environment._prepared is True
    assert raw.launched_with == "fake.exe"

    obs = environment.observe(include_screenshot=False)
    assert obs.window_title == "Fake App"
    assert obs.action_capabilities == raw.capabilities()

    note = environment.act(
        {"action": "type", "text": "hello", "element_id": 1}
    )
    assert note == "typed 'hello'"
    assert raw.app.text_content == "hello"

    environment.close()
    assert environment._prepared is False
    assert raw.app.alive is False


def test_local_environment_keeps_adapter_execution_policy_for_raw_adapters():
    environment = LocalExecutionEnvironment(FakeAdapter())

    with pytest.raises(AdapterError, match="system-level key combination"):
        environment.act({"action": "key", "keys": "alt+f4"})


def test_environment_metadata_describes_shared_local_execution():
    environment = LocalExecutionEnvironment(FakeAdapter())

    info = environment.info()
    assert info.environment_type == "local"
    assert info.adapter_type == "fake"
    assert info.isolated is False
    assert info.location == "host"
    assert environment.describe_environment() == "local:fake (host, shared)"


def test_environment_factory_rejects_unimplemented_execution_location():
    with pytest.raises(ExecutionEnvironmentError, match="available: local"):
        create_execution_environment("cli", environment_type="capsule")
