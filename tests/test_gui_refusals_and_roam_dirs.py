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


@pytest.mark.parametrize("text", [
    "roam notepad.exe tomorrow", "tomorrow, roam notepad.exe", "roam notepad.exe in 10 minutes",
    "roam notepad.exe later", "explore notepad.exe tonight",
])
def test_future_time_roams_ask_instead_of_starting_now(text):
    target = split_roam_request(text).target
    routed = validate_intent({"intent": "roam", "args": {"target": target}}, text, {})
    assert routed["intent"] == "chat" and "schedule" in routed["args"]["reply"]


@pytest.mark.parametrize("text,target", [
    ("roam notepad.exe for 5 minutes", "notepad.exe"),
    ("roam https://example.test/later", "https://example.test/later"),
    ("roam python later.py", "python later.py"),
    ('roam "tool --at 5pm"', "tool --at 5pm"),
])
def test_time_words_inside_targets_do_not_block_roams(text, target):
    routed = validate_intent({"intent": "roam", "args": {"target": target}}, text, {})
    assert routed["intent"] == "roam" and routed["args"]["target"] == target


@pytest.mark.parametrize("action,text", [
    ("export", "I would rather not export knowledge for notepad.exe"),
    ("reset", "you should not reset knowledge for notepad.exe"),
    ("reset", "no need to clear the knowledge for notepad.exe"),
    ("export", "I can’t export knowledge for notepad.exe right now"),
])
def test_broader_refusals_never_authorize_knowledge_mutations(action, text):
    routed = validate_intent({"intent": "knowledge", "args": {"action": action, "target": "notepad.exe"}}, text, {})
    assert routed["intent"] == "chat"


@pytest.mark.parametrize("action", ["export", "reset"])
def test_knowledge_mutation_target_must_be_the_one_the_user_named(action):
    text = f"{action} knowledge for notepad.exe"
    swapped = validate_intent({"intent": "knowledge", "args": {"action": action, "target": "chrome.exe"}}, text, {})
    assert swapped["intent"] == "chat"
    named = validate_intent({"intent": "knowledge", "args": {"action": action, "target": "notepad.exe"}}, text, {})
    assert named["intent"] == "knowledge" and named["args"]["target"] == "notepad.exe"
    by_key = validate_intent({"intent": "knowledge", "args": {"action": action, "target": "notepad.exe"}},
                             f"{action} knowledge for notepad-exe", {})
    assert by_key["intent"] == "knowledge"


@pytest.fixture
def api(tmp_path, monkeypatch):
    for var in ("ARGUS_PROVIDER", "ARGUS_MODEL", "ARGUS_EXECUTION_ENVIRONMENT",
                "ARGUS_CAPSULE_RETAIN_ON_FAILURE", "ARGUS_CAPSULE_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ARGUS_GUI_STATE_DIR", str(tmp_path / "user-state"))
    init_project(tmp_path)
    return gui_app.ArgusAPI(tmp_path)


def _write_config(project, text):
    (project / ".argus" / "config.yaml").write_text(text, encoding="utf-8")


def test_implicit_default_provider_stays_selectable_after_switching(api, tmp_path):
    _write_config(tmp_path, "provider: ollama\nproviders:\n  anthropic:\n    model: claude-test\n")
    assert api.set_provider("anthropic")["ok"]
    names = [p["type"] for p in api.app_info()["providers"]]
    assert "ollama" in names and "anthropic" in names
    assert api.set_provider("ollama")["ok"]


def test_invalid_retention_env_is_reported_before_a_capsule_session_looks_ready(api, tmp_path, monkeypatch):
    _write_config(tmp_path, "provider: ollama\nexecution:\n  environment: capsule\n")
    monkeypatch.setenv("ARGUS_CAPSULE_RETAIN_ON_FAILURE", "maybe")
    info = api.app_info()
    assert not info["ok"] and "ARGUS_CAPSULE_RETAIN_ON_FAILURE" in info["error"]
    _write_config(tmp_path, "provider: ollama\nexecution:\n  environment: local\n")
    assert api.app_info()["ok"]  # local sessions never parse the Capsule setting


def test_finished_watch_jobs_are_bounded_but_explain_targets_survive(api):
    total = gui_app._WATCH_JOBS_KEPT + 5
    for i in range(total):
        api._jobs[f"j{i}"] = {"id": f"j{i}", "running": False, "runs": [{"key": f"k{i}"}]}
        api._job_trackers[f"j{i}"] = [object()]
        api._results[f"k{i}"] = {"status": "fail"}
    api._last_failed = "k0"
    api._last_finished = f"k{total - 1}"
    for i in range(total):
        api._retire_watch_job(f"j{i}")
    assert len(api._watch_jobs) == gui_app._WATCH_JOBS_KEPT
    assert "j0" not in api._jobs and "j0" not in api._job_trackers
    assert "k0" in api._results and "k1" not in api._results
    assert f"j{total - 1}" in api._jobs and f"k{total - 1}" in api._results


@pytest.mark.parametrize("text", [
    "run checkout, actually don't", "run checkout but do not", "run checkout. never mind",
    "run checkout, scratch that", "run checkout; on second thought, hold off",
])
def test_trailing_cancellations_take_the_run_back(text):
    routed = validate_intent({"intent": "run", "args": {"tests": ["checkout.test.yaml"]}}, text, TESTS)
    assert routed["intent"] == "chat"


@pytest.mark.parametrize("text", [
    "run checkout not locally", "run checkout, it should not be local",
    "run checkout but I’d rather not run locally", "run checkout without local execution",
])
def test_broader_environment_refusals_never_choose_or_keep_local(text):
    routed = validate_intent({"intent": "run", "args": {"tests": ["checkout.test.yaml"]}}, text, TESTS)
    assert routed["intent"] == "chat"


@pytest.mark.parametrize("text", [
    "run checkout and never retain the failure capsule",
    "run checkout, I would rather not keep the failure capsule",
    "run checkout and no need to keep the failure capsule",
])
def test_broader_retention_refusals_never_retain(text):
    routed = validate_intent({"intent": "run"}, text, TESTS)
    assert routed["intent"] == "run" and routed["args"]["retain"] is False


def test_hyphenated_test_names_are_not_refusals():
    tests = {"tests": [{"file": "no-login.test.yaml", "name": "no-login", "adapter": "cli"}]}
    routed = validate_intent({"intent": "run"}, "run no-login locally", tests)
    assert routed["intent"] == "run" and routed["args"]["environment"] == "local"


@pytest.mark.parametrize("source", ["config", "env"])
def test_unknown_model_provider_is_reported_instead_of_ready(api, tmp_path, monkeypatch, source):
    if source == "config":
        _write_config(tmp_path, "provider: typo\n")
    else:
        monkeypatch.setenv("ARGUS_PROVIDER", "typo")
    info = api.app_info()
    assert not info["ok"] and "typo" in info["error"] and "anthropic" in info["error"]


@pytest.mark.parametrize("text", [
    "roam notepad.exe actually cancel that", "roam notepad.exe, never mind",
    "explore notepad.exe but don't", "roam notepad.exe; scratch that",
])
def test_trailing_cancellations_take_the_roam_back(text):
    routed = validate_intent({"intent": "roam", "args": {"target": split_roam_request(text).target}}, text, {})
    assert routed["intent"] == "chat"


@pytest.mark.parametrize("action,text", [
    ("stop", "stop the run, actually don't"), ("stop", "stop it, but never mind"),
    ("save_test", "save this draft, actually no"), ("init", "initialize the Argus project. never mind"),
    ("write_test", "write a test for login, scratch that"),
])
def test_trailing_cancellations_take_simple_actions_back(action, text):
    assert validate_intent({"intent": action}, text, {"has_draft": True})["intent"] == "chat"


@pytest.mark.parametrize("action,text", [
    ("stop", "stop it, cancel"), ("write_test", "write a test that users don't see errors"),
    ("write_test", "write a test that the cancel button works"),
])
def test_requests_that_merely_mention_cancel_or_dont_still_work(action, text):
    assert validate_intent({"intent": action}, text, {"has_draft": True})["intent"] == action


def test_quoted_test_titles_never_change_run_settings():
    tests = {"tests": [{"file": "a.test.yaml", "name": "Checkout in a capsule", "adapter": "cli"},
                       {"file": "b.test.yaml", "name": "Keep failure capsule", "adapter": "cli"}]}
    for text, wanted in (('run "Checkout in a capsule"', "a.test.yaml"), ('run "Keep failure capsule"', "b.test.yaml")):
        routed = validate_intent({"intent": "run"}, text, tests)
        assert routed["intent"] == "run" and routed["args"]["tests"] == [wanted]
        assert routed["args"].get("environment") is None and routed["args"].get("retain") is None
    explicit = validate_intent({"intent": "run"}, 'run "Checkout in a capsule" in a capsule', tests)
    assert explicit["args"]["environment"] == "capsule"
