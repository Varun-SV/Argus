"""Tests for the CLI adapter."""
import sys

import pytest

from argus.adapters.cli_adapter import CLIAdapter
from argus.adapters.base import AdapterError


def test_cli_adapter_run_echo():
    adapter = CLIAdapter()
    if sys.platform == "win32":
        adapter.launch("cmd /c echo hello")
    else:
        adapter.launch("echo hello")
    obs = adapter.observe()
    assert "hello" in (obs.stdout or "")
    assert obs.exit_code == 0
    assert obs.process_alive is True


def test_cli_adapter_exit_code():
    adapter = CLIAdapter()
    if sys.platform == "win32":
        adapter.launch("cmd /c exit 42")
    else:
        adapter.launch("sh -c 'exit 42'")
    obs = adapter.observe()
    assert obs.exit_code == 42


def test_cli_adapter_command_not_found():
    adapter = CLIAdapter()
    adapter.launch("this_command_does_not_exist_xyz_abc_123")
    obs = adapter.observe()
    assert obs.exit_code == 127 or obs.exit_code is not None


def test_cli_adapter_act_done():
    adapter = CLIAdapter()
    result = adapter.act({"action": "done"})
    assert result == "done"


def test_cli_adapter_act_unknown_raises():
    adapter = CLIAdapter()
    with pytest.raises(AdapterError, match="unknown action"):
        adapter.act({"action": "click"})


def test_cli_adapter_find_text_in_stdout():
    adapter = CLIAdapter()
    if sys.platform == "win32":
        adapter.launch("cmd /c echo hello world")
    else:
        adapter.launch("echo hello world")
    obs = adapter.observe()
    assert obs.find_text("hello")
    assert not obs.find_text("xyz_not_present")
