"""Regressions for the review of head d72bfe4: roam refusals, future-time runs, roam dirs."""

from __future__ import annotations

import pytest

from argus.config import init_project
from argus.gui import app as gui_app
from argus.gui.assistant import split_roam_request, validate_intent

TESTS = {"tests": [{"file": "checkout.test.yaml", "name": "checkout", "adapter": "cli"}]}


@pytest.mark.parametrize("text", [
    "you should not roam notepad.exe", "no need to explore notepad.exe",
    "I would rather not roam notepad.exe", "I’d rather not roam notepad.exe",
    "I won't roam notepad.exe", "I can't roam notepad.exe",
])
def test_broader_refusals_never_authorize_a_roam(text):
    routed = validate_intent({"intent": "roam", "args": {"target": "notepad.exe"}}, text, {})
    assert routed["intent"] == "chat"


@pytest.mark.parametrize("text,memory", [
    ("without memory, roam notepad.exe", False),
    ("roam notepad.exe without memory", False),
    ("with memory roam notepad.exe", True),
])
def test_memory_modifiers_are_not_mistaken_for_refusals(text, memory):
    request = split_roam_request(text)
    assert request.problem is None
    assert request.target == "notepad.exe" and request.modifiers == {"memory": memory}


@pytest.mark.parametrize("when", [
    "tomorrow", "later", "next week", "in 5 minutes", "in an hour", "tonight",
    "at 5pm", "on Friday", "this evening",
])
def test_future_time_run_requests_ask_instead_of_running_now(when):
    routed = validate_intent({"intent": "run", "args": {"tests": ["checkout.test.yaml"]}},
                             f"run checkout {when}", TESTS)
    assert routed["intent"] == "chat" and "schedule" in routed["args"]["reply"]


@pytest.mark.parametrize("text", ["run checkout", "run checkout now", "run checkout in a capsule"])
def test_immediate_run_requests_still_run(text):
    routed = validate_intent({"intent": "run"}, text, TESTS)
    assert routed["intent"] == "run" and routed["args"]["tests"] == ["checkout.test.yaml"]


def test_same_second_roams_get_separate_directories(tmp_path, monkeypatch):
    init_project(tmp_path)
    cfg = gui_app.load_config(tmp_path)
    monkeypatch.setattr(gui_app.time, "strftime", lambda *_: "20260101-000000")
    first, second = gui_app._reserve_roam_dir(cfg), gui_app._reserve_roam_dir(cfg)
    assert first != second and first.is_dir() and second.is_dir()
    assert first.parent == second.parent == (tmp_path / ".argus" / "roam").resolve()
    assert first.name.startswith("20260101-000000-")
