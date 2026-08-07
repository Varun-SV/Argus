import pytest

from argus.actions import ActionValidationError, validate_action
from argus.adapters.base import AdapterError, PolicyAdapter
from argus.adapters.windows_safe import SafeWindowsGUIAdapter
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


def test_safe_windows_rejects_coordinate_input():
    adapter = SafeWindowsGUIAdapter()
    with pytest.raises(AdapterError, match="coordinate input is disabled"):
        adapter.validate_action({"action": "click", "x": 20, "y": 30})


def test_safe_windows_rejects_global_keyboard_input():
    adapter = SafeWindowsGUIAdapter()
    with pytest.raises(AdapterError, match="host input"):
        adapter.validate_action({"action": "key", "keys": "ctrl+s"})


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
