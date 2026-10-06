"""Regressions for the review of head d72bfe4: roam refusals, future-time runs, roam dirs."""

from __future__ import annotations

import os
import sys

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


@pytest.mark.parametrize("action,text", [
    ("stop", "stop the run if it hangs"), ("stop", "stop it when checkout finishes"),
    ("stop", "stop the roam once it finds a bug"), ("save_test", "save this draft after it passes"),
    ("init", "initialize the Argus project unless one exists"),
])
def test_conditional_simple_actions_are_not_taken_now(action, text):
    assert validate_intent({"intent": action}, text, {"has_draft": True})["intent"] == "chat"


def test_a_drafted_test_may_describe_conditions():
    text = "write a test that login fails when the password is wrong"
    assert validate_intent({"intent": "write_test"}, text, {})["intent"] == "write_test"


@pytest.mark.parametrize("text", [
    "roam notepad.exe after deployment", "roam notepad.exe when the build passes",
    "once the server starts, roam notepad.exe", "roam notepad.exe if the login page loads",
])
def test_conditional_roams_are_not_started_now(text):
    routed = validate_intent({"intent": "roam", "args": {"target": split_roam_request(text).target}}, text, {})
    assert routed["intent"] == "chat"


def test_condition_words_inside_quoted_targets_and_urls_still_roam():
    for text, target in (('roam "tool --when ready"', "tool --when ready"),
                         ("roam https://example.test/if", "https://example.test/if")):
        routed = validate_intent({"intent": "roam", "args": {"target": target}}, text, {})
        assert routed["intent"] == "roam" and routed["args"]["target"] == target


def test_knowledge_export_waits_for_an_active_store(api, tmp_path):
    graphs = tmp_path / ".argus" / "knowledge"
    graphs.mkdir(exist_ok=True)
    (graphs / "notepad-exe.graph.json").write_text("{}", encoding="utf-8")
    api._active_ks = object()
    out = api.knowledge_export("notepad.exe")
    assert not out["ok"] and "Wait" in out["error"]
    assert not (tmp_path / ".argus" / "exports" / "notepad-exe.graph.json").exists()


class _ProbeCountingProvider:
    """Wraps FakeProvider to count vision probes across every provider a job creates."""
    probes = 0

    @classmethod
    def make(cls, offline=False):
        from tests.conftest import FakeProvider
        from argus.providers.base import ProviderError

        class Provider(FakeProvider):
            def _detect_vision(self):
                cls.probes += 1
                if offline:
                    raise ProviderError("provider offline")
                return True
        return Provider([])


def _write_specs(project, specs):
    import yaml
    for name, steps in specs.items():
        (project / ".argus" / f"{name}.test.yaml").write_text(yaml.safe_dump({
            "name": name, "target": {"adapter": "cli", "launch": f'"{sys.executable}" -c "print(123)"'},
            "steps": steps}, sort_keys=False), encoding="utf-8")


def _run_and_wait(api, files):
    import time
    started = api.run_tests(files)
    assert started["ok"], started
    deadline = time.time() + 60
    while api.job_status(started["job"]["id"])["running"]:
        assert time.time() < deadline
        time.sleep(0.05)
    return api.job_status(started["job"]["id"])


def test_assertion_only_specs_never_probe_the_model(api, tmp_path, monkeypatch):
    _ProbeCountingProvider.probes = 0
    monkeypatch.setattr(gui_app.ArgusConfig, "make_provider",
                        lambda self, tracker=None: _ProbeCountingProvider.make(offline=True))
    _write_specs(tmp_path, {"only-asserts": [{"assert": {"exit_code_is": 0}}]})
    job = _run_and_wait(api, ["only-asserts.test.yaml"])
    assert job["runs"][0]["status"] == "pass", job["runs"][0]
    assert _ProbeCountingProvider.probes == 0


def test_a_multi_test_job_probes_vision_once(api, tmp_path, monkeypatch):
    _ProbeCountingProvider.probes = 0
    monkeypatch.setattr(gui_app.ArgusConfig, "make_provider",
                        lambda self, tracker=None: _ProbeCountingProvider.make())
    _write_specs(tmp_path, {f"t{i}": ["Check the output", {"assert": {"exit_code_is": 0}}] for i in range(3)})
    job = _run_and_wait(api, [f"t{i}.test.yaml" for i in range(3)])
    assert len(job["runs"]) == 3
    assert _ProbeCountingProvider.probes == 1


@pytest.mark.parametrize("text", [
    "start watch when deployment finishes", "watch test files if the build passes",
    "start watch, actually don't", "start watching the tests. never mind",
])
def test_conditional_or_withdrawn_watch_requests_do_nothing(text):
    assert validate_intent({"intent": "watch", "args": {"action": "start"}}, text, {})["intent"] == "chat"


@pytest.mark.parametrize("text,action", [("start watch", "start"), ("watch the tests", "start"), ("stop watch", "stop")])
def test_plain_watch_requests_still_work(text, action):
    routed = validate_intent({"intent": "watch"}, text, {})
    assert routed["intent"] == "watch" and routed["args"]["action"] == action


@pytest.mark.parametrize("action,text", [
    ("export", "export knowledge for notepad.exe, actually don't"),
    ("reset", "reset knowledge for notepad.exe. never mind"),
])
def test_withdrawn_knowledge_mutations_are_not_authorized(action, text):
    routed = validate_intent({"intent": "knowledge", "args": {"action": action, "target": "notepad.exe"}}, text, {})
    assert routed["intent"] == "chat"


@pytest.mark.parametrize("text", [
    "export knowledge for notepad.exe after deployment", "export knowledge for notepad.exe tomorrow",
    "reset knowledge for notepad.exe in 5 minutes", "reset knowledge for notepad.exe once the run ends",
])
def test_deferred_knowledge_mutations_are_not_authorized(text):
    action = "export" if text.startswith("export") else "reset"
    routed = validate_intent({"intent": "knowledge", "args": {"action": action, "target": "notepad.exe"}}, text, {})
    assert routed["intent"] == "chat"


@pytest.mark.parametrize("text", [
    "switch to local, actually don't", "switch to local after deployment", "switch to local tomorrow",
    "I would rather not switch to local", "use a capsule when the build passes",
])
def test_withdrawn_deferred_or_refused_environment_changes_do_nothing(text):
    assert validate_intent({"intent": "environment", "args": {"environment": "local"}}, text, {})["intent"] == "chat"


@pytest.mark.parametrize("text", [
    "switch to anthropic, actually don't", "use anthropic tomorrow",
    "switch to anthropic when the build passes", "you should not switch to anthropic",
])
def test_withdrawn_deferred_or_refused_provider_switches_do_nothing(text):
    ctx = {"providers": ["ollama", "anthropic"]}
    assert validate_intent({"intent": "switch_provider", "args": {"provider": "anthropic"}}, text, ctx)["intent"] == "chat"


@pytest.mark.parametrize("intent,text", [("stop", "stop the run in 5 minutes"), ("watch", "start watch tomorrow")])
def test_scheduled_stop_and_watch_requests_do_nothing(intent, text):
    assert validate_intent({"intent": intent}, text, {})["intent"] == "chat"


def test_plain_environment_and_provider_changes_still_work():
    assert validate_intent({"intent": "environment"}, "switch to local", {})["args"]["environment"] == "local"
    assert validate_intent({"intent": "environment"}, "use a libvirt capsule", {})["args"]["capsule_provider"] == "libvirt"
    ctx = {"providers": ["ollama", "anthropic"]}
    for text in ("switch to anthropic", "use anthropic"):
        assert validate_intent({"intent": "switch_provider", "args": {"provider": "anthropic"}}, text, ctx)["intent"] == "switch_provider"


@pytest.mark.parametrize("intent,text", [
    ("run", "run checkout, I changed my mind"), ("run", "run checkout. I take that back"),
    ("stop", "stop the run, I've changed my mind"),
    ("knowledge", "export knowledge for notepad.exe, I take it back"),
])
def test_changed_my_mind_withdraws_the_request(intent, text):
    args = {"action": "export", "target": "notepad.exe"} if intent == "knowledge" else {}
    assert validate_intent({"intent": intent, "args": args}, text, TESTS)["intent"] == "chat"


@pytest.mark.parametrize("intent,text", [
    ("run", "run checkout in half an hour"), ("run", "run checkout in two minutes"),
    ("run", "run checkout in an hour and a half"), ("roam", "roam notepad.exe in half an hour"),
    ("environment", "switch to local in a bit"),
])
def test_word_based_and_fractional_delays_are_deferred(intent, text):
    args = {"target": split_roam_request(text).target} if intent == "roam" else {}
    assert validate_intent({"intent": intent, "args": args}, text, TESTS)["intent"] == "chat"


@pytest.mark.parametrize("text", [
    "run checkout provided that the build passes", "run checkout in case it breaks",
    "run checkout whenever the server is up", "run checkout till it passes",
])
def test_run_uses_the_shared_condition_vocabulary(text):
    assert validate_intent({"intent": "run"}, text, TESTS)["intent"] == "chat"


@pytest.mark.parametrize("text,retain", [
    ("run checkout and don't retain the failure capsule", False),
    ("run checkout without retaining the failure capsule", False),
    ("run checkout and keep the failure capsule", True),
])
def test_retention_settings_are_not_read_as_cancellations_or_exclusions(text, retain):
    routed = validate_intent({"intent": "run"}, text, TESTS)
    assert routed["intent"] == "run" and routed["args"]["tests"] == ["checkout.test.yaml"]
    assert routed["args"]["retain"] is retain


@pytest.mark.parametrize("text,target", [
    ("export knowledge for happy.exe", "app"), ("export knowledge for chromebook", "chrome"),
    ("export knowledge for notepad.exe", "notepad"),
])
def test_knowledge_targets_must_be_whole_mentions(text, target):
    routed = validate_intent({"intent": "knowledge", "args": {"action": "export", "target": target}}, text, {})
    assert routed["intent"] == "chat"


@pytest.mark.parametrize("text,target", [
    ("export knowledge for notepad.exe", "notepad.exe"), ("export knowledge for notepad-exe", "notepad.exe"),
    ("reset knowledge for Visual Studio Code", "Visual Studio Code"),
    ("export knowledge for http://localhost:3000.", "http://localhost:3000"),
])
def test_whole_knowledge_mentions_still_match(text, target):
    action = "reset" if text.startswith("reset") else "export"
    routed = validate_intent({"intent": "knowledge", "args": {"action": action, "target": target}}, text, {})
    assert routed["intent"] == "knowledge" and routed["args"]["target"] == target


def test_a_malformed_provider_entry_never_becomes_the_session_provider(api, tmp_path):
    _write_config(tmp_path, "provider: ollama\nproviders:\n  anthropic:\n    api_key_env: [BAD]\n")
    out = api.set_provider("anthropic")
    assert not out["ok"] and "anthropic" in out["error"]
    assert api._session["provider"] is None and api.app_info()["ok"]
    assert api.set_provider("ollama")["ok"]


def test_rejected_environment_change_is_rolled_back(api, tmp_path, monkeypatch):
    _write_config(tmp_path, "provider: ollama\nexecution:\n  environment: local\n")
    monkeypatch.setenv("ARGUS_CAPSULE_RETAIN_ON_FAILURE", "maybe")
    out = api.set_environment("capsule", "libvirt")
    assert not out["ok"] and "ARGUS_CAPSULE_RETAIN_ON_FAILURE" in out["error"]
    assert api._session["environment"] is None and api._session["capsule_provider"] is None
    info = api.app_info()
    assert info["ok"] and info["environment"] == "local"


def test_settings_between_test_names_keep_the_later_tests():
    tests = {"tests": [{"file": "checkout.test.yaml", "name": "checkout", "adapter": "cli"},
                       {"file": "profile.test.yaml", "name": "profile", "adapter": "cli"}]}
    routed = validate_intent({"intent": "run"}, "run checkout in a capsule then profile", tests)
    assert routed["intent"] == "run" and routed["args"]["environment"] == "capsule"
    assert routed["args"]["tests"] == ["checkout.test.yaml", "profile.test.yaml"]


@pytest.mark.parametrize("intent,text", [
    ("run", "I want to know whether you can run checkout"), ("run", "I wonder if you can run checkout"),
    ("roam", "I would like to know whether Argus can roam notepad.exe"),
    ("stop", "I'm not sure whether you should stop the run"),
])
def test_embedded_capability_questions_are_not_authorization(intent, text):
    args = {"target": "notepad.exe"} if intent == "roam" else {}
    assert validate_intent({"intent": intent, "args": args}, text, TESTS)["intent"] == "chat"


@pytest.mark.parametrize("text", [
    "write a test to check whether I can log in",
    "draft a test that verifies whether users can reset passwords",
    "write a test to find out if checkout works",
])
def test_drafts_about_questions_are_still_draft_requests(text):
    routed = validate_intent({"intent": "write_test"}, text, {})
    assert routed["intent"] == "write_test" and routed["args"]["description"] == text


def test_questions_about_drafting_are_still_questions():
    assert validate_intent({"intent": "write_test"}, "I want to know whether you can write a test", {})["intent"] == "chat"


@pytest.mark.parametrize("text,action,target", [
    ("export knowledge for notepad.exe", "export", "notepad.exe"),
    ("export knowledge for notepad.exe please", "export", "notepad.exe"),
    ("reset the knowledge graph for http://localhost:3000.", "reset", "http://localhost:3000"),
    ("export notepad.exe knowledge", "export", "notepad.exe"),
    ('export knowledge for "Visual Studio Code"', "export", "Visual Studio Code"),
])
def test_dropped_knowledge_target_is_taken_from_the_users_words(text, action, target):
    routed = validate_intent({"intent": "knowledge", "args": {"action": action}}, text, {})
    assert routed["intent"] == "knowledge" and routed["args"]["target"] == target


@pytest.mark.parametrize("text", ["export the knowledge", "export knowledge for the last app", "export knowledge for it"])
def test_unnamed_knowledge_targets_keep_the_last_target_default(text):
    routed = validate_intent({"intent": "knowledge", "args": {"action": "export"}}, text, {})
    assert routed["intent"] == "knowledge" and routed["args"]["target"] == ""


@pytest.mark.parametrize("intent,text", [
    ("run", "run checkout two hours from now"), ("run", "run checkout 5 minutes from now"),
    ("run", "run checkout an hour from now"), ("stop", "stop the run ten minutes from now"),
    ("environment", "switch to local 5 minutes from now"),
])
def test_delays_written_as_from_now_are_deferred(intent, text):
    assert validate_intent({"intent": intent}, text, TESTS)["intent"] == "chat"


@pytest.mark.parametrize("text,action", [
    ('export knowledge for notepad.exe because "I need a backup"', "export"),
    ('reset knowledge for notepad.exe and note "cleanup"', "reset"),
])
def test_quoted_asides_are_not_knowledge_targets(text, action):
    routed = validate_intent({"intent": "knowledge", "args": {"action": action}}, text, {})
    assert routed["intent"] == "knowledge" and routed["args"]["target"] == "notepad.exe"


@pytest.mark.parametrize("memory,expect_store", [(False, False), (True, True)])
def test_memory_off_roams_get_no_knowledge_store(api, monkeypatch, memory, expect_store):
    import time
    from argus.engine.roam_impl import RoamSession

    seen, made = {}, []
    sentinel = type("Store", (), {"close": lambda self: None, "get_stats": lambda self, t=None: {}})()

    def fake_roam(**kwargs):
        seen["store"] = kwargs["knowledge_store"]
        session = RoamSession(target=kwargs["target"], provider="fake")
        session.execution_status = "pass"
        return session

    monkeypatch.setattr("argus.engine.roam.roam", fake_roam)
    monkeypatch.setattr(gui_app.ArgusConfig, "make_execution_environment", lambda self, a, e=None, c=None: object())
    monkeypatch.setattr(gui_app.ArgusConfig, "make_knowledge_store", lambda self: made.append(1) or sentinel)
    monkeypatch.setattr(gui_app.ArgusConfig, "make_provider", lambda self, tracker=None: _ProbeCountingProvider.make())
    started = api.start_roam("notepad.exe", minutes=0.1, memory=memory)
    assert started["ok"], started
    deadline = time.time() + 30
    while api.job_status(started["job"]["id"])["running"]:
        assert time.time() < deadline
        time.sleep(0.05)
    assert (seen["store"] is sentinel) is expect_store and bool(made) is expect_store


def test_ambiguous_slash_run_is_refused_instead_of_running_every_match():
    from argus.gui.assistant import IntentError, parse_slash
    tests = [{"file": "checkout.test.yaml", "name": "checkout", "adapter": "cli"},
             {"file": "checkout-destructive.test.yaml", "name": "checkout destructive", "adapter": "cli"}]
    with pytest.raises(IntentError, match="matches 2 tests"):
        parse_slash("/run check", tests)
    with pytest.raises(IntentError, match="matches 2 tests"):
        parse_slash("/dry-run check", tests)
    assert parse_slash("/run checkout", tests)["args"]["tests"] == ["checkout.test.yaml"]
    assert parse_slash("/run checkout-destructive.test.yaml", tests)["args"]["tests"] == ["checkout-destructive.test.yaml"]
    assert parse_slash("/run all", tests)["args"]["tests"] == "all"


def test_history_follows_allocation_order_even_when_the_clock_moves_back(tmp_path, monkeypatch):
    from argus.engine import results
    from argus.engine.results import RunResult, load_runs
    monkeypatch.setattr(results, "_last_storage_order", 0)
    clock = iter([2_000_000_000_000_000_000, 1_000_000_000_000_000_000])
    monkeypatch.setattr(results.time, "time_ns", lambda: next(clock))
    first = RunResult(test_name="a", test_file="a.test.yaml", adapter="cli", provider="fake",
                      started_at=1_800_000_000.0, status="pass")
    # The wall clock moved back an hour, so the later run has an earlier stamp.
    second = RunResult(test_name="a", test_file="a.test.yaml", adapter="cli", provider="fake",
                       started_at=1_800_000_000.0 - 3600, status="fail")
    first.save(tmp_path)
    second.save(tmp_path)
    assert [r["status"] for r in load_runs(tmp_path)] == ["fail", "pass"]


def test_finished_foreground_jobs_are_bounded(api):
    total = gui_app._FINISHED_JOBS_KEPT + 7
    for i in range(total):
        api._jobs[f"f{i}"] = {"id": f"f{i}", "running": False, "kind": "run", "runs": [{"key": f"r{i}"}]}
        api._job_trackers[f"f{i}"] = [object()]
        api._results[f"r{i}"] = {"status": "pass"}
    api._last_failed = "r0"
    running = {"id": "live", "running": True, "kind": "roam", "key": None}
    api._jobs["live"] = running
    api._retire_finished_job("live")
    for i in range(total):
        api._retire_finished_job(f"f{i}")
    assert "live" in api._jobs  # a running job is never dropped
    assert "f0" not in api._jobs and "r0" in api._results  # explain target survives
    assert "f1" not in api._jobs and "r1" not in api._results
    assert f"f{total - 1}" in api._jobs
    assert len([j for j in api._finished_jobs if j != "live"]) == gui_app._FINISHED_JOBS_KEPT


def test_a_fresh_process_allocates_after_persisted_history_despite_clock_rollback(tmp_path, monkeypatch):
    from argus.engine import results
    from argus.engine.results import RunResult, load_runs
    clock = iter([2_000_000_000_000_000_000, 2_000_000_000_000_000_000,
                  1_000_000_000_000_000_000, 1_000_000_000_000_000_000])
    monkeypatch.setattr(results.time, "time_ns", lambda: next(clock))
    monkeypatch.setattr(results, "_last_storage_order", 0)
    first = RunResult(test_name="a", test_file="a.test.yaml", adapter="cli", provider="fake",
                      started_at=1_800_000_000.0, status="pass")
    first.claim_project_order(tmp_path)
    first.save(tmp_path)
    # A new `argus run` process: the in-memory counter starts over and the clock is behind.
    monkeypatch.setattr(results, "_last_storage_order", 0)
    second = RunResult(test_name="a", test_file="a.test.yaml", adapter="cli", provider="fake",
                       started_at=1_800_000_000.0 - 3600, status="fail")
    second.claim_project_order(tmp_path)
    second.save(tmp_path)
    assert [r["status"] for r in load_runs(tmp_path)] == ["fail", "pass"]


def test_order_counter_counts_history_written_before_it_existed(tmp_path, monkeypatch):
    from argus.engine import results
    from argus.engine.results import RunResult, reserve_storage_order
    monkeypatch.setattr(results, "_last_storage_order", 0)
    RunResult(test_name="a", test_file="a.test.yaml", adapter="cli", provider="fake").save(tmp_path)
    newest = max(int(results._HISTORY_NAME.fullmatch(p.name)[2])
                 for p in (tmp_path / ".argus" / "runs").glob("*.json"))
    (tmp_path / ".argus" / "runs" / ".storage-order").unlink(missing_ok=True)
    monkeypatch.setattr(results, "_last_storage_order", 0)
    monkeypatch.setattr(results.time, "time_ns", lambda: 1)
    assert reserve_storage_order(tmp_path) == newest + 1


def _reserve_in_fresh_process(args):
    project, count = args
    from pathlib import Path
    from argus.engine import results
    results.time.time_ns = lambda: 1  # every process's clock is behind the history
    return [results.reserve_storage_order(Path(project)) for _ in range(count)]


def test_concurrent_processes_reserve_unique_orders_after_clock_rollback(tmp_path):
    import multiprocessing
    from argus.engine.results import RunResult, reserve_storage_order
    RunResult(test_name="a", test_file="a.test.yaml", adapter="cli", provider="fake").save(tmp_path)
    floor = reserve_storage_order(tmp_path)
    with multiprocessing.get_context("spawn").Pool(4) as pool:
        batches = pool.map(_reserve_in_fresh_process, [(str(tmp_path), 25)] * 4)
    orders = [o for batch in batches for o in batch]
    assert len(set(orders)) == 100 and min(orders) > floor
    assert all(batch == sorted(batch) for batch in batches)


def test_run_test_claims_the_project_order(tmp_path, monkeypatch):
    from argus.engine import runner_impl
    seen = []

    class Result:
        def __init__(self, **kw):
            pass

        def claim_project_order(self, project):
            seen.append(project)
            raise RuntimeError("stop")

    monkeypatch.setattr(runner_impl, "RunResult", Result)
    with pytest.raises(RuntimeError, match="stop"):
        runner_impl.run_test(type("Spec", (), {"name": "a", "file_name": "a.test.yaml", "adapter": "cli"})(),
                             type("P", (), {"describe": lambda self: "fake"})(), object(), project_dir=tmp_path)
    assert seen == [tmp_path]


@pytest.mark.parametrize("text,target", [
    ("export knowledge for https://example.test/search?q=one", "https://example.test/search?q=one"),
    ("export knowledge for https://example.test/search?q=one&page=2, please", "https://example.test/search?q=one&page=2"),
    ("export knowledge for https://example.test/a,b?x=1 now", "https://example.test/a,b?x=1"),
    ("export knowledge for notepad.exe?", "notepad.exe"),
])
def test_url_query_markers_stay_in_knowledge_targets(text, target):
    for routed_target in ("", target):
        got = validate_intent({"intent": "knowledge", "args": {"action": "export", "target": routed_target}}, text, {})
        assert got == {"intent": "knowledge", "args": {"action": "export", "target": target}}


def test_saved_conversations_keep_busy_ones_beyond_the_cap(api):
    convs = [{"id": f"c{i}", "title": "t", "msgs": [], "busy": i >= 25} for i in range(45)]
    assert api.save_conversations(convs)["ok"]
    saved = [c["id"] for c in api.load_conversations()]
    assert saved == [f"c{i}" for i in range(45)]
    assert api.save_conversations([{"id": f"c{i}", "title": "t", "msgs": []} for i in range(45)])["ok"]
    assert len(api.load_conversations()) == 30


@pytest.mark.parametrize("text", [
    "don't check the provider connection", "do not ping the model", "no need to check my provider",
    "skip the provider check", "never test the model connection",
    "how do I check my provider connection?", "should I check my provider?",
    "check my provider connection later", "check the provider after this run",
    "check my provider connection, actually don't", "check the model, never mind",
    "hello there",
])
def test_provider_pings_need_a_present_unrefused_request(text):
    routed = validate_intent({"intent": "providers"}, text, {"providers": ["openai"]})
    assert routed["intent"] == "chat"


@pytest.mark.parametrize("text", [
    "check my provider connection", "is my provider working?", "can you check the model connection?",
    "does my model support vision?", "ping openai",
])
def test_explicit_provider_checks_still_route(text):
    assert validate_intent({"intent": "providers"}, text, {"providers": ["openai"]})["intent"] == "providers"


@pytest.mark.parametrize("text", [
    "show me the current environment", "where will tests run?", "what environment am I using?",
    "which environment is selected?", "where do my tests execute?",
])
def test_environment_inspection_returns_the_current_setting(text):
    assert validate_intent({"intent": "environment", "args": {"environment": "capsule"}}, text, {}) == {
        "intent": "environment", "args": {}}


@pytest.mark.parametrize("text", [
    "should I switch to local?", "switch to a capsule tomorrow", "don't show the environment", "hello",
])
def test_environment_changes_keep_their_guards(text):
    assert validate_intent({"intent": "environment", "args": {"environment": "local"}}, text, {})["intent"] == "chat"


def _write_stub(api):
    cfg = api._config()
    session = cfg.argus_dir / "roam" / "session-1"
    session.mkdir(parents=True)
    stub = session / "regression-1.test.yaml"
    stub.write_text("name: Regression\ntarget:\n  adapter: cli\n  launch: echo ready\n"
                    "steps:\n  - assert:\n      exit_code_is: 0\n", encoding="utf-8")
    return stub.relative_to(cfg.project_dir).as_posix()


def test_regression_stub_works_from_a_restored_card_without_its_job(api):
    relative = _write_stub(api)
    assert "gone-job" not in api._jobs
    draft = api.regression_stub("gone-job", 0, relative)
    assert draft["ok"] and draft["from_finding"] and draft["name"] == "Regression"


@pytest.mark.parametrize("path", [
    ".argus/config.yaml", "../outside/regression-1.test.yaml", ".argus/roam/session-1/notes.test.yaml",
    ".argus/regression-x.test.yaml", None,
])
def test_regression_stub_card_path_is_only_a_roam_regression_file(api, tmp_path, path):
    _write_stub(api)
    (tmp_path / ".argus" / "regression-x.test.yaml").write_text("name: x\n", encoding="utf-8")
    assert not api.regression_stub("gone-job", 0, path)["ok"]


def test_regression_stub_rejects_an_absolute_card_path(api):
    relative = _write_stub(api)
    absolute = str(api._config().project_dir / relative)
    assert not api.regression_stub("gone-job", 0, absolute)["ok"]


@pytest.mark.parametrize("kind", ["directory", "dangling"])
def test_unreadable_test_entry_is_reported_without_hiding_healthy_tests(api, tmp_path, kind):
    argus_dir = tmp_path / ".argus"
    (argus_dir / "good.test.yaml").write_text(
        "name: good\ntarget:\n  adapter: cli\n  launch: echo ok\nsteps:\n  - assert:\n      exit_code_is: 0\n",
        encoding="utf-8")
    bad = argus_dir / "broken.test.yaml"
    if kind == "directory":
        bad.mkdir()
    else:
        try:
            bad.symlink_to(argus_dir / "missing.yaml")
        except OSError:
            pytest.skip("symlinks unavailable")
    if bad.name not in [p.name for p in gui_app.discover_tests(tmp_path)]:
        pytest.skip("discovery already skips this entry")
    entries = {e["file"]: e for e in api.list_tests()}
    assert entries["good.test.yaml"]["error"] is None and entries["good.test.yaml"]["name"] == "good"
    assert entries["broken.test.yaml"]["error"]


def test_inaccessible_order_counter_fails_the_run_instead_of_running_unordered(tmp_path):
    from argus.engine import runner_impl
    (tmp_path / ".argus" / "runs" / ".storage-order").mkdir(parents=True)  # can't be opened as a file

    class Adapter:
        def launch(self, target):
            raise AssertionError("the test must not run without a history reservation")

    spec = type("Spec", (), {"name": "a", "file_name": "a.test.yaml", "adapter": "cli", "launch": "echo"})()
    result = runner_impl.run_test(spec, type("P", (), {"describe": lambda self: "fake"})(), Adapter(),
                                  project_dir=tmp_path)
    assert result.status == "error" and "run history" in result.error and "not run" in result.error


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock")
def test_unsupported_locking_raises_at_once_instead_of_spinning(tmp_path, monkeypatch):
    import errno
    import fcntl
    from argus.engine import results
    calls = []

    def flock(fd, op):
        calls.append(op)
        raise OSError(errno.ENOLCK, "no locks available")

    monkeypatch.setattr(fcntl, "flock", flock)
    with pytest.raises(OSError) as raised:
        results.reserve_storage_order(tmp_path)
    assert raised.value.errno == errno.ENOLCK and len(calls) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock")
def test_contended_lock_waits_a_bounded_time(tmp_path):
    import errno
    import fcntl
    from argus.engine import results
    path = tmp_path / "counter"
    path.write_bytes(b"")
    with open(path, "r+b") as holder, open(path, "r+b") as waiter:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        with pytest.raises(OSError) as raised:
            with results._file_locked(waiter, timeout=0.2):
                pass
    assert raised.value.errno == errno.ETIMEDOUT


@pytest.mark.parametrize("failures,expect", [
    ([], "locked"), (["busy", "busy"], "locked"), (["bad"], "raise"), (["busy"] * 1000, "timeout"),
])
def test_windows_lock_retries_only_contention_and_is_bounded(tmp_path, monkeypatch, failures, expect):
    import errno
    from types import SimpleNamespace
    from argus.engine import results
    pending = list(failures)
    calls = []

    def locking(fd, mode, size):
        calls.append(mode)
        if mode == "nb" and pending:
            kind = pending.pop(0)
            raise OSError(errno.EACCES if kind == "busy" else errno.EINVAL, kind)

    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(LK_NBLCK="nb", LK_UNLCK="un", locking=locking))
    monkeypatch.setattr(results.time, "sleep", lambda _: None)
    path = tmp_path / "counter"
    path.write_bytes(b"")
    original = results.os.name
    with open(path, "r+b") as stream:
        monkeypatch.setattr(results.os, "name", "nt")
        try:
            if expect == "locked":
                with results._file_locked(stream):
                    pass
                assert calls[-1] == "un" and calls.count("nb") == len(failures) + 1
            else:
                with pytest.raises(OSError) as raised:
                    with results._file_locked(stream, timeout=0 if expect == "timeout" else 30):
                        pass
                want = errno.EINVAL if expect == "raise" else errno.ETIMEDOUT
                assert raised.value.errno == want and "un" not in calls
                if expect == "raise":
                    assert calls == ["nb"]
        finally:
            monkeypatch.setattr(results.os, "name", original)  # before pytest itself looks at os.name


def test_hard_linked_order_counter_never_rewrites_the_outside_file(tmp_path):
    from argus.engine.results import reserve_storage_order
    project = tmp_path / "project"
    (project / ".argus" / "runs").mkdir(parents=True)
    sentinel = tmp_path / "outside.txt"
    sentinel.write_bytes(b"unrelated host data")
    try:
        os.link(sentinel, project / ".argus" / "runs" / ".storage-order")
    except OSError:
        pytest.skip("hard links unavailable")
    with pytest.raises(OSError, match="singly linked regular file"):
        reserve_storage_order(project)
    assert sentinel.read_bytes() == b"unrelated host data"


def test_counter_replaced_by_another_file_after_open_is_refused(tmp_path, monkeypatch):
    from argus.engine import results
    runs = tmp_path / ".argus" / "runs"
    runs.mkdir(parents=True)
    (runs / ".storage-order").write_bytes(b"5")
    other = tmp_path / "other"
    other.write_bytes(b"7")
    real_lstat = os.lstat
    monkeypatch.setattr(results.os, "lstat", lambda p, *a, **k: real_lstat(other if str(p).endswith(".storage-order") else p))
    with pytest.raises(OSError, match="singly linked regular file"):
        results.reserve_storage_order(tmp_path)
    assert other.read_bytes() == b"7"


def _prior_pass_then_unreservable(tmp_path, monkeypatch):
    from argus.engine import results
    from argus.engine.results import RunResult
    prior = RunResult(test_name="b", test_file="b.test.yaml", adapter="cli", provider="fake", status="pass")
    prior.claim_project_order(tmp_path)
    prior.save(tmp_path)
    monkeypatch.setattr(results, "_last_storage_order", 0)
    monkeypatch.setattr(results.time, "time_ns", lambda: 1)
    counter = tmp_path / ".argus" / "runs" / ".storage-order"
    counter.unlink()
    counter.mkdir()


def test_unreserved_error_result_is_not_published_under_an_unverified_order(tmp_path, monkeypatch):
    from argus.engine import runner_impl
    from argus.engine.results import UnreservedRunError, load_runs
    _prior_pass_then_unreservable(tmp_path, monkeypatch)
    spec = type("Spec", (), {"name": "b", "file_name": "b.test.yaml", "adapter": "cli", "launch": "echo"})()
    result = runner_impl.run_test(spec, type("P", (), {"describe": lambda self: "fake"})(), object(),
                                  project_dir=tmp_path)
    assert result.status == "error"
    with pytest.raises(UnreservedRunError, match="Not added to run history"):
        result.save(tmp_path)
    assert [r["status"] for r in load_runs(tmp_path)] == ["pass"]


def test_cli_reports_an_unreserved_result_instead_of_saving_or_crashing(tmp_path, monkeypatch, capsys):
    from argus import cli
    from argus.engine.results import RunResult, load_runs
    _prior_pass_then_unreservable(tmp_path, monkeypatch)
    result = RunResult(test_name="b", test_file="b.test.yaml", adapter="cli", provider="fake", status="error")
    with pytest.raises(OSError):
        result.claim_project_order(tmp_path)
    cli._save_result(result, tmp_path)
    assert "Not added to run history" in capsys.readouterr().out
    assert [r["status"] for r in load_runs(tmp_path)] == ["pass"]


def test_gui_run_without_a_reservation_reports_and_keeps_history_ordered(api, tmp_path, monkeypatch):
    from argus.config import ArgusConfig
    from argus.engine.results import load_runs
    from tests.conftest import FakeProvider
    from tests.test_gui_api import CLI_SPEC, _wait
    cfg = api._config()
    cfg.knowledge.enabled = False
    monkeypatch.setattr(api, "_config", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(ArgusConfig, "make_provider", lambda *args, **kwargs: FakeProvider([]))
    (cfg.argus_dir / "b.test.yaml").write_text(CLI_SPEC)
    _prior_pass_then_unreservable(tmp_path, monkeypatch)
    job = _wait(api, api.run_tests(["b.test.yaml"])["job"]["id"])
    run = job["runs"][0]
    assert run["status"] == "error"
    assert any("Not added to run history" in note for note in run["notes"])
    assert [r["status"] for r in load_runs(tmp_path)] == ["pass"]


def test_a_never_claimed_result_reserves_at_save_after_a_clock_rollback(tmp_path, monkeypatch):
    from argus.engine import results
    from argus.engine.results import RunResult, load_runs
    earlier = RunResult(test_name="a", test_file="a.test.yaml", adapter="cli", provider="fake", status="pass")
    earlier.save(tmp_path)  # an earlier process
    # A new process whose clock is behind: run_test(...) without project_dir, then save(project).
    monkeypatch.setattr(results, "_counter_sync", {})
    monkeypatch.setattr(results, "_last_storage_order", 0)
    monkeypatch.setattr(results.time, "time_ns", lambda: 1)
    later = RunResult(test_name="a", test_file="a.test.yaml", adapter="cli", provider="fake", status="fail")
    assert later._storage_order == 1
    later.save(tmp_path)
    assert later._storage_order > earlier._storage_order
    assert [r["status"] for r in load_runs(tmp_path)] == ["fail", "pass"]


def test_save_time_reservation_failure_is_not_published(tmp_path, monkeypatch):
    from argus.engine.results import RunResult, UnreservedRunError, load_runs
    counter = tmp_path / ".argus" / "runs" / ".storage-order"
    counter.mkdir(parents=True)
    result = RunResult(test_name="a", test_file="a.test.yaml", adapter="cli", provider="fake", status="pass")
    with pytest.raises(OSError):
        result.save(tmp_path)
    with pytest.raises(UnreservedRunError):
        result.save(tmp_path)
    assert load_runs(tmp_path) == []


def _watch_project(tmp_path):
    project = tmp_path / "project"
    (project / ".argus").mkdir(parents=True)
    outside = tmp_path / "outside.test.yaml"
    outside.write_text("name: outside\n", encoding="utf-8")
    return project, outside


def test_watch_never_versions_a_symlinked_spec(tmp_path):
    project, outside = _watch_project(tmp_path)
    try:
        (project / ".argus" / "linked.test.yaml").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    (project / ".argus" / "own.test.yaml").write_text("name: own\n", encoding="utf-8")
    before = gui_app._test_versions(project)
    assert before["linked.test.yaml"] is None and before["own.test.yaml"] is not None
    outside.write_text("name: outside\ntarget:\n  launch: something-else\n", encoding="utf-8")
    after = gui_app._test_versions(project, before)
    assert after["linked.test.yaml"] is None  # never a change the watch re-runs
    with pytest.raises(OSError, match="symlink"):
        gui_app._test_version(project / ".argus" / "linked.test.yaml")


def test_watch_ignores_specs_under_a_linked_argus_directory(tmp_path):
    project, outside = _watch_project(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "x.test.yaml").write_text("name: x\n", encoding="utf-8")
    (project / ".argus").rmdir()
    try:
        (project / ".argus").symlink_to(elsewhere, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert gui_app._test_versions(project) == {}


def test_null_capsule_hardware_keeps_defaults_and_still_loads(tmp_path, monkeypatch):
    from argus.config import load_config
    monkeypatch.delenv("ARGUS_CAPSULE_MEMORY_MB", raising=False)
    monkeypatch.delenv("ARGUS_CAPSULE_CPU_COUNT", raising=False)
    (tmp_path / ".argus").mkdir()
    (tmp_path / ".argus" / "config.yaml").write_text(
        "provider: ollama\nexecution:\n  environment: local\n  capsule:\n    memory_mb: null\n    cpu_count: null\n",
        encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.execution.capsule.memory_mb is None and cfg.execution.capsule.cpu_count is None


@pytest.mark.parametrize("text", [
    "stop watching", "cancel watch mode", "stop the watch", "turn off watch mode", "stop the watcher",
])
def test_watch_only_stops_never_become_the_global_stop(text):
    assert validate_intent({"intent": "stop"}, text, {}) == {"intent": "watch", "args": {"action": "stop"}}
    assert validate_intent({"intent": "watch"}, text, {}) == {"intent": "watch", "args": {"action": "stop"}}


@pytest.mark.parametrize("text", ["stop the run", "stop everything", "stop the run and the watch", "please stop"])
def test_run_stops_stay_global(text):
    assert validate_intent({"intent": "stop"}, text, {})["intent"] == "stop"


@pytest.mark.parametrize("text", ["don't stop watching", "stop watching tomorrow", "should I stop watching?"])
def test_watch_stop_refusals_stop_nothing(text):
    assert validate_intent({"intent": "stop"}, text, {})["intent"] == "chat"


def test_start_watching_phrasing_starts_the_watch():
    assert validate_intent({"intent": "watch"}, "start watching tests", {}) == {"intent": "watch", "args": {"action": "start"}}


def _gui_runner(api, monkeypatch):
    from argus.config import ArgusConfig
    from tests.conftest import FakeProvider
    cfg = api._config()
    cfg.knowledge.enabled = False
    monkeypatch.setattr(api, "_config", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(ArgusConfig, "make_provider", lambda *args, **kwargs: FakeProvider([]))
    return cfg


def test_an_unreadable_entry_fails_only_its_own_run(api, monkeypatch):
    from tests.test_gui_api import CLI_SPEC, _wait
    cfg = _gui_runner(api, monkeypatch)
    for stale in cfg.argus_dir.glob("*.test.yaml"):
        stale.unlink()
    (cfg.argus_dir / "good.test.yaml").write_text(CLI_SPEC)
    (cfg.argus_dir / "broken.test.yaml").mkdir()
    started = api.run_tests("all")
    assert started["ok"]
    job = _wait(api, started["job"]["id"])
    runs = {r["file"]: r for r in job["runs"]}
    assert runs["good.test.yaml"]["status"] == "pass"
    assert runs["broken.test.yaml"]["status"] == "error" and "could not read" in runs["broken.test.yaml"]["notes"][0]


def test_watch_runs_refuse_a_spec_swapped_for_a_link(api, tmp_path, monkeypatch):
    import yaml
    from tests.test_gui_api import _wait
    cfg = _gui_runner(api, monkeypatch)
    marker = tmp_path / "outside-ran"
    outside = tmp_path / "outside.test.yaml"
    outside.write_text(yaml.safe_dump({
        "name": "outside", "target": {"adapter": "cli", "launch": (
            f'"{sys.executable}" -c "open(r\'{marker}\', \'w\').close()"')},
        "steps": [{"assert": {"exit_code_is": 0}}]}), encoding="utf-8")
    try:
        (cfg.argus_dir / "swapped.test.yaml").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    api._watch_scope.active = True  # as the watch worker starts it
    try:
        job = _wait(api, api.run_tests(["swapped.test.yaml"])["job"]["id"])
    finally:
        api._watch_scope.active = False
    assert job["runs"][0]["status"] == "error" and not marker.exists()


def test_watch_worker_runs_with_own_files_only(api, monkeypatch):
    calls = []

    def fake_run(tests):
        calls.append(api._watch_scope.active)
        watch["running"] = False
        return {"ok": False, "error": "stop here"}

    watch = {"running": True, "events": [], "settled_count": 0}
    monkeypatch.setattr(api, "run_tests", fake_run)
    monkeypatch.setattr(gui_app, "_test_versions", lambda project, seen=None: {"a.test.yaml": ("new",)})
    api._watch_worker(watch, api._config().project_dir, poll=0, initial_versions={"a.test.yaml": ("old",)})
    assert calls == [True] and not api._watch_scope.active


@pytest.mark.parametrize("when", [
    "an hour and a half from now", "one hour and a half from now", "a minute and a half from now",
    "two hours and a half from now",
])
@pytest.mark.parametrize("intent,command", [
    ("run", "run checkout"), ("stop", "stop the run"),
])
def test_unit_first_and_a_half_delays_never_act_now(intent, command, when):
    args = {"tests": ["checkout.test.yaml"]} if intent == "run" else {}
    assert validate_intent({"intent": intent, "args": args}, f"{command} {when}", TESTS)["intent"] == "chat"


def test_unit_first_and_a_half_delay_never_exports_knowledge_now():
    routed = validate_intent({"intent": "knowledge", "args": {"action": "export", "target": "notepad.exe"}},
                             "export knowledge for notepad.exe an hour and a half from now", {})
    assert routed["intent"] == "chat"


def test_dry_run_reports_an_unreadable_entry_and_keeps_healthy_items(api, tmp_path):
    from tests.test_gui_api import CLI_SPEC
    argus_dir = tmp_path / ".argus"
    for stale in argus_dir.glob("*.test.yaml"):
        stale.unlink()
    (argus_dir / "good.test.yaml").write_text(CLI_SPEC)
    (argus_dir / "bad.test.yaml").mkdir()
    out = api.dry_run("all")
    assert out["ok"]
    items = {i["file"]: i for i in out["items"]}
    assert items["good.test.yaml"]["error"] is None and items["good.test.yaml"]["steps"]
    assert "could not read" in items["bad.test.yaml"]["error"]


@pytest.mark.parametrize("text", [
    "The CI will run checkout", "The system can run checkout", "we run checkout nightly in CI",
    "our pipeline executes checkout on merge",
])
def test_descriptive_run_statements_never_execute(text):
    assert validate_intent({"intent": "run", "args": {"tests": ["checkout.test.yaml"]}}, text, TESTS)["intent"] == "chat"


@pytest.mark.parametrize("text", [
    "run checkout", "Run checkout locally", "please run checkout", "can you run checkout?",
    "Argus, run checkout", "I want you to run checkout", "let's run checkout", "now rerun checkout",
    "in a capsule, run checkout",
])
def test_requests_to_run_still_execute(text):
    assert validate_intent({"intent": "run", "args": {"tests": ["checkout.test.yaml"]}}, text, TESTS)["intent"] == "run"


@pytest.mark.parametrize("text", [
    "The app will stop responding at login", "The run tends to abort on timeout", "it keeps stopping",
])
def test_descriptive_stop_statements_never_stop(text):
    assert validate_intent({"intent": "stop"}, text, {})["intent"] == "chat"


@pytest.mark.parametrize("text", ["stop", "Stop!", "stop the run", "please stop", "can you stop it", "cancel the roam"])
def test_requests_to_stop_still_stop(text):
    assert validate_intent({"intent": "stop"}, text, {})["intent"] == "stop"


@pytest.mark.parametrize("doc", [{}, {"steps": [], "tokens": {}}, {"status": "pass"}, {"test_file": "a.test.yaml"}])
def test_history_records_need_identity_and_status(doc):
    from argus.engine.results import valid_run_history
    assert not valid_run_history(doc)


def test_malformed_newest_history_cannot_hide_real_runs(tmp_path):
    import json
    from argus.engine.results import RunResult, load_runs
    RunResult(test_name="a", test_file="a.test.yaml", adapter="cli", provider="fake", status="pass").save(tmp_path)
    runs = tmp_path / ".argus" / "runs"
    for i in range(3):
        (runs / f"29991231-235959-{99999999999999999990 + i:020d}-{'f' * 32}-x.json").write_text(
            json.dumps({"steps": [], "tokens": {}}))
    assert [r["status"] for r in load_runs(tmp_path, 1)] == ["pass"]


class _RemoteOnlyStore:
    def __init__(self, has):
        self.has, self.cleared, self.closed = has, [], False

    def has_remote_target(self, target):
        return self.has

    def clear_target(self, target):
        self.cleared.append(target)

    def close(self):
        self.closed = True


@pytest.mark.parametrize("has", [True, False])
def test_remote_only_knowledge_can_be_reset(api, monkeypatch, has):
    from argus.config import ArgusConfig, KnowledgeConfig
    cfg = api._config()
    cfg.knowledge = KnowledgeConfig(type="external", vector_url="http://127.0.0.1:1")
    store = _RemoteOnlyStore(has)
    monkeypatch.setattr(api, "_config", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(ArgusConfig, "make_knowledge_store", lambda self: store)
    result = api.knowledge_reset("notepad.exe")
    assert result["ok"] is has and store.closed
    assert store.cleared == (["notepad.exe"] if has else [])


def test_remote_store_reports_collections_for_a_target(monkeypatch):
    from types import SimpleNamespace
    from argus.knowledge.remote import RemoteKnowledgeStore
    store = object.__new__(RemoteKnowledgeStore)
    names = [SimpleNamespace(name="notepad-exe_states"), SimpleNamespace(name="other_bugs")]
    client = SimpleNamespace(get_collections=lambda: SimpleNamespace(collections=names))
    monkeypatch.setattr(store, "_client", lambda: client, raising=False)
    assert store.has_remote_target("notepad.exe")
    assert not store.has_remote_target("calc.exe")


def test_live_screenshots_stay_fresh_when_the_clock_moves_back(api, monkeypatch):
    clock = iter([2_000_000_000.0, 1_000_000_000.0])
    monkeypatch.setattr(gui_app.time, "time", lambda: next(clock, 1.0))
    api._set_screenshot(b"first")
    shown = api.capture_live(0)["ts"]
    api._set_screenshot(b"second")
    newer = api.capture_live(shown)
    assert newer["ts"] > shown and newer["b64"]


@pytest.mark.parametrize("text", [
    "The CI says: run checkout", "Please explain: run checkout", "The CI says, run checkout",
    "The README says run checkout",
])
def test_reported_or_quoted_instructions_never_run(text):
    assert validate_intent({"intent": "run", "args": {"tests": ["checkout.test.yaml"]}}, text, TESTS)["intent"] == "chat"


@pytest.mark.parametrize("text", ["The log says: stop the run", "Explain: stop"])
def test_reported_stop_instructions_never_stop(text):
    assert validate_intent({"intent": "stop"}, text, {})["intent"] == "chat"


@pytest.mark.parametrize("doc", [
    {"test_file": "", "status": ""}, {"test_file": "   ", "status": "pass"},
    {"test_file": "a.test.yaml", "status": ""}, {"test_file": "a.test.yaml", "status": "maybe"},
])
def test_history_records_need_a_named_test_and_a_known_status(doc):
    from argus.engine.results import valid_run_history
    assert not valid_run_history(doc)
    assert valid_run_history({"test_file": "a.test.yaml", "status": "pass"})


def _remote_store(tmp_path, client):
    from argus.knowledge.remote import RemoteKnowledgeStore
    store = object.__new__(RemoteKnowledgeStore)
    store._dir, store._graphs = tmp_path, {}
    store._client = lambda: client
    return store


class _Collections:
    def __init__(self, names, fail):
        self.names, self.fail = set(names), fail

    def get_collections(self):
        from types import SimpleNamespace
        return SimpleNamespace(collections=[SimpleNamespace(name=n) for n in sorted(self.names)])

    def delete_collection(self, name):
        if self.fail:
            raise ConnectionError("qdrant unavailable")
        self.names.discard(name)


def test_remote_clear_raises_when_collections_survive(tmp_path):
    client = _Collections({"notepad-exe_states", "notepad-exe_bugs"}, fail=True)
    with pytest.raises(RuntimeError, match="Could not delete the remote knowledge"):
        _remote_store(tmp_path, client).clear_target("notepad.exe")
    ok = _Collections({"notepad-exe_states"}, fail=False)
    _remote_store(tmp_path, ok).clear_target("notepad.exe")
    assert ok.names == set()


def test_reset_reports_a_failed_remote_deletion(api, monkeypatch):
    from argus.config import ArgusConfig, KnowledgeConfig
    cfg = api._config()
    cfg.knowledge = KnowledgeConfig(type="external", vector_url="http://127.0.0.1:1")
    store = _remote_store(cfg.argus_dir, _Collections({"notepad-exe_states"}, fail=True))
    store.close = lambda: None
    monkeypatch.setattr(api, "_config", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(ArgusConfig, "make_knowledge_store", lambda self: store)
    result = api.knowledge_reset("notepad.exe")
    assert not result["ok"] and "Could not delete the remote knowledge" in result["error"]


TWO_TESTS = {"tests": [{"file": "checkout.test.yaml", "name": "checkout", "adapter": "cli"},
                       {"file": "smoke.test.yaml", "name": "smoke", "adapter": "cli"}]}


@pytest.mark.parametrize("text", ["The CI says: please test checkout", "The log says: can you check checkout"])
def test_reported_test_or_check_requests_never_run(text):
    routed = validate_intent({"intent": "run", "args": {"tests": ["checkout.test.yaml"]}}, text, TWO_TESTS)
    assert routed["intent"] == "chat"


@pytest.mark.parametrize("routed", [["smoke.test.yaml"], ["checkout.test.yaml"]])
@pytest.mark.parametrize("text", [
    "The CI says: run checkout, but please run smoke", "The CI says: run checkout. Please run smoke",
])
def test_a_later_explicit_request_survives_reported_speech(text, routed):
    got = validate_intent({"intent": "run", "args": {"tests": routed}}, text, TWO_TESTS)
    assert got == {"intent": "run", "args": {"tests": ["smoke.test.yaml"]}}
