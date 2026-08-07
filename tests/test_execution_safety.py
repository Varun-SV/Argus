import pytest

from argus.actions import ActionValidationError, validate_action
from argus.adapters.base import AdapterError, Observation, PolicyAdapter
from argus.adapters.windows_safe import SafeWindowsGUIAdapter
from argus.engine.agent import build_action_schema, observation_prompt
from tests.conftest import FakeAdapter


def test_action_schema_normalizes_coordinates():
    action = validate_action({"action": "CLICK", "x": "10", "y": 20})
    assert action["action"] == "click"
    assert action["x"] == 10
    assert action["y"] == 20


def test_action_schema_rejects_unknown_action():
    with pytest.raises(ActionValidationError):
        validate_action({"action": "open_host_terminal"})


def test_policy_adapter_blocks_system_shortcut():
    adapter = PolicyAdapter(FakeAdapter())
    with pytest.raises(AdapterError, match="blocked"):
        adapter.act({"action": "key", "keys": "win+r"})


def test_policy_adapter_allows_target_shortcut():
    inner = FakeAdapter()
    adapter = PolicyAdapter(inner)
    note = adapter.act({"action": "key", "keys": "ctrl+s"})
    assert note == "pressed ctrl+s"
    assert inner.app.action_log == ["key"]


def test_policy_adapter_blocks_malformed_click_before_adapter():
    inner = FakeAdapter()
    adapter = PolicyAdapter(inner)
    with pytest.raises(AdapterError, match="requires element_id or x/y"):
        adapter.act({"action": "click"})
    assert inner.app.action_log == []


def test_policy_adapter_attaches_capabilities_to_observation():
    adapter = PolicyAdapter(FakeAdapter())
    obs = adapter.observe(include_screenshot=False)
    assert obs.action_capabilities == adapter.capabilities()


def test_safe_windows_rejects_coordinate_input():
    adapter = SafeWindowsGUIAdapter()
    with pytest.raises(AdapterError, match="coordinate input is disabled"):
        adapter.validate_action({"action": "click", "x": 20, "y": 30})


def test_safe_windows_rejects_global_keyboard_input():
    adapter = SafeWindowsGUIAdapter()
    with pytest.raises(AdapterError, match="disabled in safe Windows mode"):
        adapter.validate_action({"action": "key", "keys": "ctrl+s"})


def test_safe_windows_rejects_menu_until_semantic_menu_support_exists():
    adapter = SafeWindowsGUIAdapter()
    with pytest.raises(AdapterError, match="disabled in safe Windows mode"):
        adapter.validate_action({"action": "menu", "path": "File->Save"})


def test_safe_windows_capability_schema_only_advertises_semantic_actions():
    schema = build_action_schema(SafeWindowsGUIAdapter().capabilities())
    assert '{"action":"click","element_id":<id>' in schema
    assert '{"action":"type","text":"...","element_id":<id>' in schema
    assert '"x":<px>' not in schema
    assert '"action":"key"' not in schema
    assert '"action":"scroll"' not in schema
    assert '"action":"menu"' not in schema
    assert '"action":"double_click"' not in schema
    assert '"action":"right_click"' not in schema


def test_roam_prompt_uses_observation_capabilities_and_keeps_report_bug():
    obs = Observation(
        window_title="Safe target",
        action_capabilities=SafeWindowsGUIAdapter().capabilities(),
    )
    prompt = observation_prompt(
        obs,
        "Free-roam exploration. Explore the application, try edge cases, and report anything broken.",
        [],
    )
    assert '"action":"report_bug"' in prompt
    assert '"action":"key"' not in prompt
    assert '"action":"done"' not in prompt


def test_safe_windows_rejects_window_without_target_ownership():
    class WrongWindow:
        def wait(self, *_args, **_kwargs):
            return None

        def process_id(self):
            return 999

    class WrongApp:
        def top_window(self):
            return WrongWindow()

    adapter = SafeWindowsGUIAdapter()
    adapter._app = WrongApp()
    with pytest.raises(AdapterError, match="refusing window owned by pid 999"):
        adapter._verify_owned_top_window({123})


def test_safe_windows_semantic_click_without_pywinauto_runtime():
    class Button:
        def __init__(self):
            self.invoked = False

        def invoke(self):
            self.invoked = True

    button = Button()
    adapter = SafeWindowsGUIAdapter()
    adapter._elements = [button]

    note = adapter.execute({"action": "click", "element_id": 0})

    assert button.invoked is True
    assert "semantic click" in note


def test_safe_windows_semantic_text_without_pywinauto_runtime():
    class Edit:
        def __init__(self):
            self.value = None

        def set_edit_text(self, value):
            self.value = value

    edit = Edit()
    adapter = SafeWindowsGUIAdapter()
    adapter._elements = [edit]

    note = adapter.execute({"action": "type", "element_id": 0, "text": "hello"})

    assert edit.value == "hello"
    assert "semantically set" in note
