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
                     "file": "search-returns-results.test.yaml", "draft_id": draft["id"]}
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


# ---- review regressions -------------------------------------------------------


def test_forced_execution_environment_is_shown_and_enforced(project, monkeypatch):
    monkeypatch.setenv("ARGUS_EXECUTION_ENVIRONMENT", "local")
    api = ArgusAPI()
    api._session["environment"] = "capsule"  # a stale session choice must not win
    info = api.app_info()
    assert info["environment"] == "local" and info["env_label"] == "Local"
    assert "ARGUS_EXECUTION_ENVIRONMENT=local" in api.set_environment("capsule", "hyperv")["error"]
    (project / ".argus" / "cli.test.yaml").write_text(CLI_SPEC, encoding="utf-8")
    refused = api.run_tests(["cli.test.yaml"], {"environment": "capsule"})
    assert refused["ok"] is False and "ARGUS_EXECUTION_ENVIRONMENT" in refused["error"]
    assert api.start_roam("notepad.exe", overrides={"environment": "capsule"})["ok"] is False


def test_run_job_keeps_the_provider_it_started_with(project, fake_llm, monkeypatch):
    seen = []
    original = ArgusConfig.make_provider

    def recording(self, tracker=None):
        seen.append(self.provider.type)
        return original(self, tracker)

    monkeypatch.setattr(ArgusConfig, "make_provider", recording)
    (project / ".argus" / "cli.test.yaml").write_text(CLI_SPEC, encoding="utf-8")
    api = ArgusAPI()
    api.set_provider("openai")
    job_id = api.run_tests(["cli.test.yaml"])["job"]["id"]
    api.set_provider("anthropic")  # switching mid-job must not affect it
    job = _wait(api, job_id)
    assert job["provider_type"] == "openai"
    assert seen and set(seen) == {"openai"}


def test_capsule_roam_passes_failure_capsule_retention(project, monkeypatch):
    captured = {}

    def fake_env(self, adapter_type, environment_type=None, capsule_overrides=None):
        captured.update(env=environment_type, overrides=dict(capsule_overrides or {}))
        raise RuntimeError("no hypervisor here")

    monkeypatch.setattr(ArgusConfig, "make_execution_environment", fake_env)
    api = ArgusAPI()
    api.set_environment("capsule", "libvirt")
    api.set_retain(True)
    job = _wait(api, api.start_roam("notepad.exe", minutes=0.1)["job"]["id"])
    assert captured == {"env": "capsule", "overrides": {"provider": "libvirt", "retain_on_failure": True}}
    assert job["status"] == "error"


@pytest.mark.parametrize("engine_status,card_status", [
    ("pass", "done"), ("fail", "fail"), ("error", "error"),
    ("cancelled", "stopped"), ("outcome_unknown", "unknown"),
])
def test_roam_outcomes_are_not_collapsed_to_done(project, fake_llm, monkeypatch, tmp_path,
                                                 engine_status, card_status):
    from argus.engine.roam_impl import RoamSession

    def fake_roam(**kwargs):
        session = RoamSession(target=kwargs["target"], provider="fake")
        session.execution_status = engine_status
        return session

    monkeypatch.setattr("argus.engine.roam.roam", fake_roam)
    monkeypatch.setattr(ArgusConfig, "make_execution_environment",
                        lambda self, a, e=None, c=None: object())
    api = ArgusAPI()
    job = _wait(api, api.start_roam("notepad.exe", minutes=0.1)["job"]["id"])
    assert job["status"] == card_status


def test_knowledge_export_uses_configured_persist_dir(project):
    from argus.knowledge.fingerprint import target_key

    store = project / "elsewhere"
    store.mkdir()
    (store / f"{target_key('notepad.exe')}.graph.json").write_text("{}", encoding="utf-8")
    cfg = project / ".argus" / "config.yaml"
    cfg.write_text(cfg.read_text() + f"\nknowledge:\n  persist_dir: {store.as_posix()}\n", encoding="utf-8")
    out = ArgusAPI().knowledge_export("notepad.exe")
    assert out["ok"], out


def test_knowledge_card_queries_the_chosen_target(project, monkeypatch):
    from argus.knowledge.fingerprint import target_key

    graphs = project / ".argus" / "knowledge"
    graphs.mkdir()
    (graphs / "notepad-exe.graph.json").write_text("{}", encoding="utf-8")
    asked = []

    class Store:
        def get_stats(self, target=None):
            asked.append(target)
            if target is None:  # the real stores key this view by Path.stem
                return {"notepad-exe.graph": {"states": 0}}
            if target_key(target) == "notepad-exe":
                return {target: {"states": 7, "transitions": 9, "bugs": 2, "sessions": 3}}
            return {target: {}}

        def close(self):
            pass

    monkeypatch.setattr(ArgusConfig, "make_knowledge_store", lambda self: Store())
    api = ArgusAPI()
    k = api.knowledge("notepad.exe")
    assert (k["target"], k["states"], k["transitions"], k["bugs"]) == ("notepad.exe", 7, 9, 2)
    assert api.knowledge("notepad")["states"] == 7  # partial names resolve to a stored graph
    fresh = ArgusAPI().knowledge("")  # after a restart: no target, no last target
    assert (fresh["target"], fresh["states"]) == ("notepad-exe", 7)
    assert not any(t and t.endswith(".graph") for t in asked)


def test_explain_uses_persisted_history(project, fake_llm):
    import json as _json

    runs = project / ".argus" / "runs"
    runs.mkdir(exist_ok=True)
    (runs / "20260101-000000-lint.test.yaml.json").write_text(_json.dumps({
        "test_file": "lint.test.yaml", "status": "fail", "steps": [
            {"index": 0, "kind": "assert", "text": "exit_code_is: 0", "status": "fail",
             "expected": "0", "actual": "3"}]}), encoding="utf-8")
    fake_llm.extend(["Exit code 3.", "Exit code 3 again."])
    api = ArgusAPI()  # fresh process: nothing in memory
    row = api.recent_runs()[0]
    assert row["id"] == "history:20260101-000000-lint.test.yaml.json"
    assert api.explain(row["id"]) == {"ok": True, "text": "Exit code 3.", "test": "lint.test.yaml",
                                      "key": row["id"], "evidence_key": None}
    assert api.explain()["test"] == "lint.test.yaml"  # no key: newest failure on disk
    assert api.explain("history:../config.yaml")["ok"] is False


def test_concurrent_charges_are_all_persisted(project):
    from argus.tokens import TokenTracker, Usage

    api = ArgusAPI()
    cfg = api._config()

    def charge():
        t = TokenTracker()
        t.add(Usage(prompt_tokens=10, completion_tokens=1))
        api._charge(t, cfg)

    threads = [threading.Thread(target=charge) for _ in range(25)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert api.token_usage()["project"]["calls"] == 25
    assert api.token_usage()["session"]["calls"] == 25


def test_starting_watch_keeps_a_pending_stop(project):
    api = ArgusAPI()
    api._stop.set()
    assert api.watch_start()["ok"]
    assert api._stop.is_set()
    api.watch_stop()


def test_job_tokens_count_only_that_job(project, fake_llm):
    fake_llm.append('{"intent": "tokens", "args": {}}')
    (project / ".argus" / "cli.test.yaml").write_text(CLI_SPEC, encoding="utf-8")
    api = ArgusAPI()
    api.interpret("how many tokens?")  # an earlier model call in this session
    assert api.token_usage()["session"]["total_tokens"] > 0
    job = _wait(api, api.run_tests(["cli.test.yaml"])["job"]["id"])
    assert job["tokens"] == 0  # the pure-assertion run used no model tokens
    assert api.live()["tokens"] == 0


def test_run_uses_the_spec_parsed_when_the_job_started(project, fake_llm, monkeypatch):
    spec_file = project / ".argus" / "cli.test.yaml"
    spec_file.write_text(CLI_SPEC, encoding="utf-8")
    def edit_while_queued(self):  # runs at the start of each test's execution
        spec_file.write_text(CLI_SPEC.replace("123", "never-printed"), encoding="utf-8")
        return None

    monkeypatch.setattr(ArgusConfig, "make_knowledge_store", edit_while_queued)
    api = ArgusAPI()
    run = _wait(api, api.run_tests(["cli.test.yaml"])["job"]["id"])["runs"][0]
    assert run["status"] == "pass"
    assert "123" in run["planned"][1]["text"]


def test_save_never_replaces_an_existing_file(project, fake_llm):
    fake_llm.append(GOOD_SPEC)
    api = ArgusAPI()
    draft = api.draft_test("search works")
    dest = project / ".argus" / draft["file"]
    dest.write_text("# written by someone else\n", encoding="utf-8")
    assert "already exists" in api.save_test()["error"]
    assert dest.read_text(encoding="utf-8") == "# written by someone else\n"


def test_draft_actions_use_the_draft_shown_on_the_card(project, fake_llm):
    other = GOOD_SPEC.replace("Search returns results", "Login works")
    api = ArgusAPI()
    fake_llm[:] = [GOOD_SPEC]
    first = api.draft_test("search works")
    fake_llm[:] = [other]
    second = api.draft_test("login works")  # e.g. drafted in another conversation
    assert first["file"] != second["file"]
    assert api.dry_run("draft", first["id"])["items"][0]["file"] == first["file"]
    saved = api.save_test(first["id"])
    assert (saved["file"], saved["draft_id"]) == (first["file"], first["id"])
    assert not (project / ".argus" / second["file"]).exists()
    stale = ArgusAPI().save_test(first["id"])  # after a restart the draft is gone
    assert stale["ok"] is False and "earlier session" in stale["error"]


def test_watch_retries_a_change_made_while_busy(project, fake_llm):
    spec = project / ".argus" / "cli.test.yaml"
    spec.write_text(CLI_SPEC, encoding="utf-8")
    api = ArgusAPI()
    api._jobs["busy"] = {"id": "busy", "running": True}  # a manual run holds the slot
    api._active_job = "busy"
    watch = {"id": "w", "running": True, "pattern": ".argus/*.test.yaml", "events": []}
    worker = threading.Thread(target=api._watch_worker, args=(watch, project, 0.05), daemon=True)
    worker.start()
    time.sleep(0.2)
    spec.write_text(CLI_SPEC + "\n# touched\n", encoding="utf-8")
    deadline = time.time() + 10
    while time.time() < deadline and not (watch["events"] and watch["events"][0]["status"] == "waiting"):
        time.sleep(0.05)
    assert watch["events"][0]["status"] == "waiting"
    api._jobs["busy"]["running"] = False  # the manual run finishes
    deadline = time.time() + 60
    while time.time() < deadline and watch["events"][0]["status"] in ("waiting", "running"):
        time.sleep(0.05)
    watch["running"] = False
    worker.join(timeout=5)
    assert len(watch["events"]) == 1
    assert watch["events"][0]["status"] == "pass"


def test_explain_reports_which_run_it_explained(project, fake_llm):
    import json as _json

    (project / ".argus" / "lint.test.yaml").write_text(
        CLI_SPEC.replace("123", "never-printed"), encoding="utf-8")
    fake_llm.extend(["It failed."])
    api = ArgusAPI()
    job = _wait(api, api.run_tests(["lint.test.yaml"])["job"]["id"])
    key = job["runs"][0]["key"]
    out = api.explain()
    assert (out["key"], out["evidence_key"]) == (key, key)  # evidence follow-up targets this run

    runs = project / ".argus" / "runs"
    (runs / "20000101-000000-old.test.yaml.json").write_text(_json.dumps({
        "test_file": "old.test.yaml", "status": "fail", "steps": []}), encoding="utf-8")
    fake_llm[:] = ["Old failure."]
    old = api.explain("history:20000101-000000-old.test.yaml.json")
    assert old["ok"] and old["evidence_key"] is None  # can't verify evidence for persisted history


def test_evidence_for_a_card_restored_after_restart(project, fake_llm):
    (project / ".argus" / "cli.test.yaml").write_text(CLI_SPEC, encoding="utf-8")
    first = ArgusAPI()
    job = _wait(first, first.run_tests(["cli.test.yaml"])["job"]["id"])
    key = job["runs"][0]["key"]
    assert key.startswith("RUN-")

    restarted = ArgusAPI()  # a new process: the card's key comes from conversations.json
    evidence = restarted.evidence(key)
    assert evidence["ok"] and evidence["verified"], evidence
    assert evidence["run_id"] == key

    missing = restarted.evidence("RUN-doesnotexist")
    assert missing["ok"] is False and "No ATES evidence found" in missing["error"]
    assert restarted.evidence("../../etc")["ok"] is False  # not a RunId, never a path


def test_malformed_scalar_spec_is_listed_as_an_error(project):
    (project / ".argus" / "cli.test.yaml").write_text(CLI_SPEC, encoding="utf-8")
    (project / ".argus" / "bad.test.yaml").write_text(
        CLI_SPEC + "retries: once\n", encoding="utf-8")
    api = ArgusAPI()
    tests = {t["file"]: t for t in api.list_tests()}
    assert tests["cli.test.yaml"]["error"] is None
    assert "once" in tests["bad.test.yaml"]["error"]
    assert api.dry_run(["bad.test.yaml"])["items"][0]["error"]
    run = api.run_tests(["bad.test.yaml"])
    assert run["ok"] and run["job"]["runs"][0]["status"] == "error"


def test_unknown_knowledge_target_does_not_create_a_graph(project, monkeypatch):
    graphs = project / ".argus" / "knowledge"
    graphs.mkdir()
    (graphs / "notepad-exe.graph.json").write_text("{}", encoding="utf-8")
    asked = []

    class Store:
        def get_stats(self, target=None):
            asked.append(target)
            return {target: {"states": 1}}

        def close(self):
            pass

    monkeypatch.setattr(ArgusConfig, "make_knowledge_store", lambda self: Store())
    api = ArgusAPI()
    k = api.knowledge("zzqq")
    assert k["ok"] is False and "notepad-exe" in k["error"]
    assert asked == []  # the store was never asked about the unknown target
    api._last_target = "never-roamed.exe"
    assert api.knowledge("")["ok"] is False and asked == []


def test_watch_reports_deleted_specs(project, fake_llm):
    spec = project / ".argus" / "cli.test.yaml"
    spec.write_text(CLI_SPEC, encoding="utf-8")
    api = ArgusAPI()
    watch = {"id": "w", "running": True, "pattern": ".argus/*.test.yaml", "events": []}
    worker = threading.Thread(target=api._watch_worker, args=(watch, project, 0.05), daemon=True)
    worker.start()
    time.sleep(0.2)
    spec.unlink()
    deadline = time.time() + 10
    while time.time() < deadline and not watch["events"]:
        time.sleep(0.05)
    watch["running"] = False
    worker.join(timeout=5)
    assert watch["events"] and watch["events"][0]["file"] == "cli.test.yaml"
    assert watch["events"][0]["status"] == "removed"


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_draft_save_and_history_reads_never_follow_symlinks(project, fake_llm, monkeypatch, tmp_path_factory):
    import json as _json

    outside = tmp_path_factory.mktemp("outside-history")
    api = ArgusAPI()

    # A repository-controlled history entry must never redirect an explanation read.
    secret = outside / "secret.json"
    secret.write_text(_json.dumps({
        "test_file": "outside.test.yaml", "status": "fail",
        "steps": [{"index": 1, "kind": "assert", "status": "fail", "actual": "secret"}],
    }), encoding="utf-8")
    history_name = "20990101-000000-outside.test.yaml.json"
    (project / ".argus" / "runs" / history_name).symlink_to(secret)
    assert all(row["id"] != "history:" + history_name for row in api.recent_runs())
    assert api.explain("history:" + history_name)["ok"] is False

    # Draft saving must attest .argus itself before creating a new test file.
    fake_llm[:] = [GOOD_SPEC]
    draft = api.draft_test("search works")
    assert draft["ok"]
    real_argus = project / ".argus"
    saved_argus = project / ".argus-real"
    real_argus.rename(saved_argus)
    real_argus.symlink_to(outside, target_is_directory=True)
    try:
        out = api.save_test(draft["id"])
        assert out["ok"] is False
        assert not (outside / draft["file"]).exists()
    finally:
        real_argus.unlink()
        saved_argus.rename(real_argus)


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_conversation_and_export_writes_never_follow_symlinks(project, monkeypatch, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    victim = outside / "victim.txt"
    victim.write_text("keep me", encoding="utf-8")
    api = ArgusAPI()

    gui = project / ".argus" / "gui"
    gui.symlink_to(outside, target_is_directory=True)  # repository-controlled redirect
    assert api.save_conversations([{"id": "c"}])["ok"] is False
    assert api.load_conversations() == []
    gui.unlink()

    gui.mkdir()
    (gui / "conversations.json").symlink_to(victim)
    (gui / "conversations.tmp").symlink_to(victim)  # the old fixed temp name
    assert api.save_conversations([{"id": "c"}])["ok"] is True
    assert victim.read_text(encoding="utf-8") == "keep me"
    assert not (gui / "conversations.json").is_symlink()
    assert api.load_conversations() == [{"id": "c"}]

    graphs = project / ".argus" / "knowledge"
    graphs.mkdir()
    (graphs / "notepad-exe.graph.json").write_text('{"nodes": []}', encoding="utf-8")
    exports = project / ".argus" / "exports"
    exports.mkdir()
    (exports / "notepad-exe.graph.json").symlink_to(victim)
    assert api.knowledge_export("notepad.exe")["ok"] is True
    assert victim.read_text(encoding="utf-8") == "keep me"
    exports_link = project / ".argus" / "exports"
    for child in exports_link.iterdir():
        child.unlink()
    exports_link.rmdir()
    exports_link.symlink_to(outside, target_is_directory=True)
    assert api.knowledge_export("notepad.exe")["ok"] is False
    assert not (outside / "notepad-exe.graph.json").exists()
