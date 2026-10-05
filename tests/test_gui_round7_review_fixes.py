"""User intent, environment readiness and watch-start acknowledgement regressions."""
import json

import pytest

from argus.config import ArgusConfig
from argus.gui import app, assistant
from argus.gui.app import ArgusAPI
from tests.conftest import FakeProvider
from tests.test_gui_api import GOOD_SPEC


CONTEXT = {"tests": [{"file": name + ".test.yaml", "name": name}
                     for name in ("checkout", "safe", "destructive")]}


@pytest.mark.parametrize("text", [
    "no need to run checkout", "you should not run checkout", "I would rather not run checkout",
    "please avoid running checkout", "I won't run checkout", "you shouldn’t run checkout",
    "please do not execute checkout", "never run checkout", "can you please not run checkout",
    "run either safe or destructive", "maybe run safe or destructive", "run safe or checkout",
    "perhaps run safe", "run all tests or checkout", "run all tests maybe",
])
def test_model_cannot_turn_negative_or_uncertain_request_into_execution(text):
    routed = assistant.validate_intent({"intent": "run", "args": {"tests": "all"}}, text, CONTEXT)
    assert routed["intent"] == "chat"


@pytest.mark.parametrize("text,files", [
    ("run safe and checkout", ["safe", "checkout"]),
    ("run safe then checkout then safe", ["safe", "checkout", "safe"]),
    ('run "Either or maybe"', ["title"]),
    ("run maybe.test.yaml", ["maybe"]),
    ("run no.test.yaml", ["no"]),
    ('run "Never run checkout"', ["negative-title"]),
    ('run "All tests or maybe"', ["all-title"]),
])
def test_explicit_scope_order_and_literal_test_names_remain_supported(text, files):
    context = {"tests": CONTEXT["tests"] + [
        {"file": "title.test.yaml", "name": "Either or maybe"},
        {"file": "maybe.test.yaml", "name": "maybe"},
        {"file": "no.test.yaml", "name": "no"},
        {"file": "negative-title.test.yaml", "name": "Never run checkout"},
        {"file": "all-title.test.yaml", "name": "All tests or maybe"}]}
    result = assistant.validate_intent({"intent": "run"}, text, context)
    assert result == {"intent": "run", "args": {"tests": [f + ".test.yaml" for f in files]}}


@pytest.mark.parametrize("description", ["Unrelated destructive workflow", "", None])
def test_router_cannot_replace_original_draft_prompt(tmp_path, monkeypatch, description):
    api = ArgusAPI(tmp_path)
    api.init_project()
    request = "Write a test for https://example.invalid/?mode=safe with --dry-run. " + "detail " * 350 + "Never submit a payment."
    provider = FakeProvider([json.dumps({"intent": "write_test", "args": {"description": description}}), GOOD_SPEC])
    monkeypatch.setattr(ArgusConfig, "make_provider", lambda *a, **k: provider)
    routed = api.interpret(request)
    assert routed["args"]["description"] == request
    assert api.draft_test(routed["args"]["description"])["ok"]
    assert provider.calls[-1]["user"] == request


def test_invalid_project_default_is_not_local_readiness_or_host_policy(tmp_path, monkeypatch):
    monkeypatch.delenv("ARGUS_EXECUTION_ENVIRONMENT", raising=False)
    api = ArgusAPI(tmp_path)
    api.init_project()
    cfg = api._config()
    cfg.execution.environment = "capusle"
    monkeypatch.setattr(api, "_config", lambda *a, **k: cfg)
    for card in (api.app_info(), api.environment()):
        assert not card["ok"] and card["environment"] is None and not card["env_locked"]
        assert "execution.environment must be local or capsule" in card["error"]
    assert not api.run_tests()["ok"]
    assert not api.start_roam("echo ready", "cli")["ok"]
    assert api._jobs == {}
    assert api._job_environment(cfg, {"environment": "capsule"}) == ("capsule", "hyperv", False, None)
    assert api._job_environment(cfg, {"environment": "typo"})[-1]
    assert api.set_environment("local")["ok"]
    assert api.app_info()["environment"] == "local"
    monkeypatch.setenv("ARGUS_EXECUTION_ENVIRONMENT", "capsule")
    assert api.environment()["environment"] == "capsule"
    assert api.environment()["env_locked"]
    assert api._job_environment(cfg, {"environment": "local"})[-1]


@pytest.mark.parametrize("created", [False, True])
def test_changes_after_watch_success_are_detected_even_if_worker_has_not_started(tmp_path, monkeypatch, created):
    api = ArgusAPI(tmp_path)
    api.init_project()
    path = tmp_path / ".argus" / "sample.test.yaml"
    if not created:
        path.write_text("name: before\n")
    scheduled = []
    class DelayedThread:
        def __init__(self, target, args, kwargs=None, **options):
            scheduled.append((target, args, kwargs))
        def start(self):
            pass
    monkeypatch.setattr(app.threading, "Thread", DelayedThread)
    assert api.watch_start()["ok"]
    path.write_text("name: changed\n")
    calls, ticks = [], []
    def run(names):
        calls.append(names)
        api._jobs["done"] = {"running": False, "runs": [{"status": "pass", "notes": [], "result": {}}]}
        return {"ok": True, "job": {"id": "done"}}
    def tick(_):
        ticks.append(True)
        if len(ticks) == 3:
            api.watch_stop()
    monkeypatch.setattr(api, "run_tests", run)
    monkeypatch.setattr(app.time, "sleep", tick)
    target, args, kwargs = scheduled[0]
    target(*args, **kwargs)
    assert calls == [[path.name]]
    assert api.watch_status()["events"][0]["status"] == "pass"
