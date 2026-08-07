import pytest

from argus.actions import ActionValidationError, canonicalize_key_chord, validate_action
from argus.adapters.base import Adapter, AdapterError, Observation, PolicyAdapter
from argus.adapters.browser_adapter import _to_playwright_key
from argus.adapters.cli_adapter import CLIAdapter
from argus.adapters.linux_gui import LinuxGUIAdapter, _to_xdotool_key
from argus.adapters.windows_gui import _to_send_keys
from argus.engine.agent import build_action_schema
from argus.policy import ActionPolicyError, enforce_action_policy


@pytest.mark.parametrize(
    "raw",
    [
        "{VK_LWIN}",
        "Super_L",
        "win+r",
        "windows+r",
        "super+r",
        "meta+r",
        "ctrl+{ESC}",
        "ctrl++s",
        "ctrl+unknown_key",
    ],
)
def test_backend_specific_or_unknown_key_syntax_is_rejected(raw):
    with pytest.raises(ActionValidationError):
        validate_action({"action": "key", "keys": raw})


def test_key_chord_is_canonicalized_before_policy_or_adapter():
    action = validate_action({"action": "key", "keys": "Control + Shift + S"})
    assert action["keys"] == "ctrl+shift+s"
    assert canonicalize_key_chord("Escape") == "esc"


@pytest.mark.parametrize(
    "keys",
    ["alt+tab", "alt+esc", "alt+f4", "ctrl+esc", "ctrl+shift+esc", "ctrl+alt+delete"],
)
def test_canonical_host_escape_shortcuts_are_blocked(keys):
    action = validate_action({"action": "key", "keys": keys})
    with pytest.raises(ActionPolicyError):
        enforce_action_policy(action)


def test_target_local_shortcut_remains_allowed():
    action = validate_action({"action": "key", "keys": "ctrl+s"})
    enforce_action_policy(action)
    assert action["keys"] == "ctrl+s"


def test_backend_translators_only_receive_canonical_keys():
    assert _to_send_keys("ctrl+shift+s") == "^+s"
    assert _to_xdotool_key("ctrl+shift+s") == "ctrl+shift+s"
    assert _to_playwright_key("ctrl+shift+s") == "Control+Shift+s"


def test_linux_capabilities_match_implemented_actions():
    caps = LinuxGUIAdapter(auto_xvfb=False).capabilities()
    actions = caps["actions"]
    assert set(actions) == {
        "click",
        "double_click",
        "type",
        "key",
        "scroll",
        "wait",
        "done",
    }
    assert actions["click"] == {"element_id": "none", "coordinates": True}
    assert actions["double_click"] == {"element_id": "none", "coordinates": True}
    assert actions["type"] == {"element_id": "none"}

    schema = build_action_schema(caps)
    assert '"action":"click","x":<px>' in schema
    assert '"action":"double_click","x":<px>' in schema
    assert '"action":"type","text":"...","why"' in schema
    assert '"element_id"' not in schema
    assert '"action":"right_click"' not in schema
    assert '"action":"menu"' not in schema


class _MinimalAdapter(Adapter):
    def launch(self, target: str) -> None:
        pass

    def observe(self, include_screenshot: bool = True) -> Observation:
        return Observation(window_title="minimal")

    def act(self, action: dict) -> str:
        if action["action"] in {"wait", "done"}:
            return action["action"]
        raise AdapterError("unsupported")

    def close(self) -> None:
        pass


def test_base_adapter_capabilities_fail_closed():
    assert set(_MinimalAdapter().capabilities()["actions"]) == {"wait", "done"}


class _OverpoweredAdapter(_MinimalAdapter):
    """Intentionally implements power it never declares."""

    def __init__(self):
        self.dispatched = False

    def act(self, action: dict) -> str:
        self.dispatched = True
        return f"executed {action['action']}"


def test_undeclared_action_cannot_reach_adapter_even_if_act_implements_it():
    adapter = _OverpoweredAdapter()
    with pytest.raises(AdapterError, match="does not declare capability 'type'"):
        adapter.execute({"action": "type", "text": "should never dispatch"})
    assert adapter.dispatched is False


def test_cli_type_compatibility_path_is_blocked_before_command_execution(monkeypatch):
    cli = CLIAdapter()

    def forbidden_run(_command):
        raise AssertionError("CLI _run must not be reached for undeclared type action")

    monkeypatch.setattr(cli, "_run", forbidden_run)
    adapter = PolicyAdapter(cli)
    with pytest.raises(AdapterError, match="does not declare capability 'type'"):
        adapter.act({"action": "type", "text": "echo unsafe"})


def test_cli_run_and_execute_require_explicit_nonempty_command():
    for kind in ("run", "execute"):
        with pytest.raises(ActionValidationError, match=f"{kind} requires a non-empty command"):
            validate_action({"action": kind})
        with pytest.raises(ActionValidationError, match=f"{kind} requires a non-empty command"):
            validate_action({"action": kind, "command": "   "})

    caps = CLIAdapter().capabilities()["actions"]
    assert caps["run"] == {"command": "required"}
    assert caps["execute"] == {"command": "required"}


def test_linux_element_id_is_rejected_by_capability_contract_before_xdotool():
    adapter = LinuxGUIAdapter(auto_xvfb=False)
    with pytest.raises(AdapterError, match="forbids element_id"):
        adapter.execute({"action": "click", "element_id": 3})


@pytest.mark.parametrize(
    "keys",
    ["ctrl+alt+f1", "ctrl+alt+f6", "ctrl+alt+f12", "ctrl+alt+backspace"],
)
def test_linux_host_session_escape_chords_are_blocked_before_xdotool(keys):
    adapter = LinuxGUIAdapter(auto_xvfb=False)
    with pytest.raises(AdapterError, match="blocked"):
        adapter.execute({"action": "key", "keys": keys})
