"""The desktop app's Python API, exercised without a window."""

from __future__ import annotations

import sys
import threading
import time

import pytest
import yaml

from argus.config import ArgusConfig, init_project, load_config
from argus.gui.app import ArgusAPI, _StoppableBudget
from argus.tokens import Budget
from tests.conftest import FakeProvider

GOOD_SPEC = """name: Search returns results
target:
  adapter: browser
  launch: "http://localhost:3000"
steps:
  - "Search for notebook"
  - assert:
      text_visible: "results"
"""

CLI_SPEC = yaml.safe_dump({
    "name": "Script says OK",
    "target": {"adapter": "cli",
               "launch": f'"{sys.executable}" -c "print(123)"'},
    "steps": [{"assert": {"exit_code_is": 0}}, {"assert": {"stdout_contains": "123"}}],
}, sort_keys=False)


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for var in ("ARGUS_PROVIDER", "ARGUS_MODEL", "ARGUS_EXECUTION_ENVIRONMENT"):
        monkeypatch.delenv(var, raising=False)
    init_project(tmp_path)
    return tmp_path


@pytest.fixture
def fake_llm(monkeypatch):
    replies: list = []

    def make_provider(self, tracker=None):
        provider = FakeProvider(replies)
        if tracker is not None:
            provider.tracker = tracker
        return provider

    monkeypatch.setattr(ArgusConfig, "make_provider", make_provider)
    return replies


def _wait(api, job_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = api.job_status(job_id)
        if not job["running"]:
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_app_info_reflects_config_and_session(project):
    api = ArgusAPI()
    info = api.app_info()
    assert info["initialized"] is True
    assert info["provider"] == "ollama"
    assert {p["type"] for p in info["providers"]} == {"ollama", "anthropic", "openai"}
    assert info["env_label"] == "Local"

    assert api.set_provider("anthropic")["model"] == "claude-sonnet-4-6"
    assert api.set_provider("litellm")["ok"] is False
    assert "provider: ollama" in (project / ".argus" / "config.yaml").read_text()

    info = api.set_environment("capsule", "libvirt")
    assert info["env_label"] == "Capsule · libvirt/KVM"
    assert api.set_environment("vm")["ok"] is False
    assert api.set_retain(True)["retain"] is True
    assert api.set_memory(False)["memory"] is False


def test_load_config_provider_override_leaves_file_alone(project):
    cfg = load_config(project, provider="openai")
    assert (cfg.provider.type, cfg.provider.model) == ("openai", "gpt-4o")
    assert load_config(project).provider.type == "ollama"


def test_capsule_overrides_are_allowlisted(project):
    cfg = load_config(project)
    with pytest.raises(ValueError):
        cfg.make_execution_environment("cli", "capsule", {"guest_token": "stolen"})
    with pytest.raises(ValueError):
        cfg.make_execution_environment("cli", "capsule", {"provider": "vmware"})


def test_list_tests_and_dry_run(project):
    api = ArgusAPI()
    tests = api.list_tests()
    assert [t["file"] for t in tests] == ["notepad.test.yaml"]
    assert tests[0]["adapter"] == "desktop-gui" and tests[0]["last"] is None
    dry = api.dry_run("all")
    assert dry["ok"] and dry["items"][0]["steps"][1]["kind"] == "assert"
    assert api.dry_run("draft")["ok"] is False


def test_slash_interpret_needs_no_model(project, monkeypatch):
    def boom(self, tracker=None):
        raise AssertionError("slash commands must not call the model")

    monkeypatch.setattr(ArgusConfig, "make_provider", boom)
    api = ArgusAPI()
    assert api.interpret("/run notepad") == {"intent": "run", "args": {"tests": ["notepad.test.yaml"]}}
    assert api.interpret("/nope")["intent"] == "error"


def test_free_text_routes_through_model_and_counts_tokens(project, fake_llm):
    fake_llm.append('{"intent": "tokens", "args": {}}')
    api = ArgusAPI()
    assert api.interpret("how many tokens have I used?") == {"intent": "tokens", "args": {}}
    assert api.token_usage()["session"]["calls"] == 1
    assert api.token_usage()["project"]["calls"] == 1


def test_draft_and_save_never_overwrite(project, fake_llm):
    fake_llm.append(GOOD_SPEC)
    api = ArgusAPI()
    draft = api.draft_test("search works")
    assert draft["ok"] and draft["file"] == "search-returns-results.test.yaml"
    assert api.dry_run("draft")["items"][0]["file"] == draft["file"]

    saved = api.save_test()
    assert saved == {"ok": True, "path": ".argus/search-returns-results.test.yaml",
                     "file": "search-returns-results.test.yaml"}
    assert (project / ".argus" / saved["file"]).read_text() == GOOD_SPEC
    assert api.save_test()["ok"] is True  # already saved: idempotent, nothing rewritten

    api._draft = dict(draft, saved=False)
    assert "already exists" in api.save_test()["error"]
    api._draft = dict(draft, saved=False, file="../escape.test.yaml")
    assert api.save_test()["ok"] is False
    assert not (project / "escape.test.yaml").exists()


def test_stoppable_budget():
    stop = threading.Event()
    budget = _StoppableBudget(Budget(max_seconds=60), stop)
    assert budget.exhausted() is None
    stop.set()
    assert budget.exhausted() == "stopped by you"


def test_run_job_end_to_end_with_evidence(project, fake_llm):
    (project / ".argus" / "cli.test.yaml").write_text(CLI_SPEC, encoding="utf-8")
    api = ArgusAPI()
    started = api.run_tests(["cli.test.yaml"])
    assert started["ok"], started
    job = _wait(api, started["job"]["id"])
    run = job["runs"][0]
    assert run["status"] == "pass", run
    assert [s["status"] for s in run["steps"]] == ["pass", "pass"]
    assert run["key"]

    assert api.list_tests()[0]["last"] in (None, "pass")
    assert [t["last"] for t in api.list_tests() if t["file"] == "cli.test.yaml"] == ["pass"]
    assert api.recent_runs()[0]["test"] == "cli.test.yaml"

    evidence = api.evidence()
    assert evidence["ok"] and evidence["verified"], evidence
    assert evidence["state"] == "bound_verified"
    assert any(r["k"] == "ordered events" for r in evidence["rows"])

    live = api.live()
    assert live["has"] and live["title"] == "cli.test.yaml" and live["running"] is False
    assert api.explain()["ok"] is False  # nothing failed


def test_second_job_is_refused_while_busy(project):
    api = ArgusAPI()
    api._jobs["x"] = {"id": "x", "running": True}
    api._active_job = "x"
    assert "already running" in api.run_tests("all")["error"]
    assert "already running" in api.start_roam("notepad.exe")["error"]


def test_watch_reruns_changed_specs(project, fake_llm):
    spec = project / ".argus" / "cli.test.yaml"
    spec.write_text(CLI_SPEC, encoding="utf-8")
    api = ArgusAPI()
    watch = {"id": "w", "running": True, "pattern": ".argus/*.test.yaml", "events": []}
    worker = threading.Thread(target=api._watch_worker, args=(watch, project, 0.05), daemon=True)
    worker.start()
    time.sleep(0.2)
    spec.write_text(CLI_SPEC + "\n# touched\n", encoding="utf-8")
    deadline = time.time() + 60
    while time.time() < deadline and not (watch["events"] and watch["events"][0]["status"] != "running"):
        time.sleep(0.05)
    watch["running"] = False
    worker.join(timeout=5)
    assert watch["events"][0]["file"] == "cli.test.yaml"
    assert watch["events"][0]["status"] == "pass"


def test_knowledge_export_requires_graph(project):
    api = ArgusAPI()
    assert api.knowledge_export("notepad.exe")["ok"] is False
    key_dir = project / ".argus" / "knowledge"
    key_dir.mkdir()
    from argus.knowledge.fingerprint import target_key

    (key_dir / f"{target_key('notepad.exe')}.graph.json").write_text("{}", encoding="utf-8")
    out = api.knowledge_export("notepad.exe")
    assert out["ok"] and out["path"].startswith(".argus/exports/")


def test_conversations_round_trip(project):
    api = ArgusAPI()
    assert api.load_conversations() == []
    convs = [{"id": f"c{i}", "title": "t", "msgs": []} for i in range(40)]
    assert api.save_conversations(convs)["ok"]
    assert len(api.load_conversations()) == 30


def test_init_reports_created_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    api = ArgusAPI()
    assert api.app_info()["initialized"] is False
    out = api.init_project()
    assert {f["path"] for f in out["files"]} == {
        ".argus/config.yaml", ".argus/notepad.test.yaml", ".argus/runs/", ".argus/roam/"}
    assert all(f["created"] for f in out["files"])
    assert not any(f["created"] for f in api.init_project()["files"])


def test_unexpected_adapter_failure_marks_run_as_error(project, fake_llm, monkeypatch):
    def broken(self, adapter_type, environment_type=None, capsule_overrides=None):
        raise RuntimeError("driver missing")

    monkeypatch.setattr(ArgusConfig, "make_execution_environment", broken)
    (project / ".argus" / "cli.test.yaml").write_text(CLI_SPEC, encoding="utf-8")
    api = ArgusAPI()
    job = _wait(api, api.run_tests(["cli.test.yaml"])["job"]["id"])
    run = job["runs"][0]
    assert run["status"] == "error"
    assert run["notes"] == ["RuntimeError: driver missing"]
    assert api.run_tests(["cli.test.yaml"])["ok"] is True  # the slot was released
