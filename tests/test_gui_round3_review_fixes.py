"""Review regressions for first use, result history and exact execution scope."""
import json

import pytest

from argus.config import ArgusConfig, KnowledgeConfig
from argus.engine.results import load_runs
from argus.gui.app import ArgusAPI
from tests.conftest import FakeProvider
from tests.test_gui_api import _wait


def test_knowledge_inspection_keeps_fresh_project_uninitialized(tmp_path, monkeypatch):
    api = ArgusAPI(tmp_path)
    monkeypatch.setattr(ArgusConfig, "make_knowledge_store",
                        lambda self: pytest.fail("Inspection must not initialize default storage"))
    assert not api.app_info()["initialized"]
    result = api.knowledge()
    assert not result["ok"] and "/init" in result["error"]
    assert not (tmp_path / ".argus").exists()
    assert not api.app_info()["initialized"]
    assert not ArgusAPI(tmp_path).app_info()["initialized"]


def test_explicit_external_knowledge_remains_inspectable_without_project_setup(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "notepad-exe.graph.json").write_text(json.dumps({"nodes": {}, "edges": []}))
    api = ArgusAPI(project)
    cfg = api._config()
    cfg.knowledge = KnowledgeConfig(type="json", persist_dir=str(external))
    monkeypatch.setattr(api, "_config", lambda provider=None: cfg)
    assert api.knowledge("notepad.exe")["ok"]
    assert not (project / ".argus").exists()


def test_disabled_knowledge_reset_reports_failure_and_preserves_graph(tmp_path, monkeypatch):
    api = ArgusAPI(tmp_path)
    api.init_project()
    cfg = api._config()
    cfg.knowledge = KnowledgeConfig(type="json", enabled=False)
    monkeypatch.setattr(api, "_config", lambda provider=None: cfg)
    directory = tmp_path / ".argus" / "knowledge"
    directory.mkdir()
    graph = directory / "notepad-exe.graph.json"
    graph.write_text("sentinel")
    result = api.knowledge_reset("notepad.exe")
    assert not result["ok"] and "disabled" in result["error"]
    assert graph.read_text() == "sentinel"
    cfg.knowledge.enabled = True
    assert api.knowledge_reset("notepad.exe")["ok"]
    assert not graph.exists()


def test_disappeared_explicit_test_aborts_the_entire_request(tmp_path, monkeypatch):
    api = ArgusAPI(tmp_path)
    api.init_project()
    sample = tmp_path / ".argus" / "smoke.test.yaml"
    other = sample.with_name("second.test.yaml")
    other.write_bytes(sample.read_bytes())
    routed_scope = [row["file"] for row in api.list_tests()]
    assert len(routed_scope) == 2
    other.unlink()
    monkeypatch.setattr(api, "_begin_job", lambda job: pytest.fail("Missing scope must not reserve a job"))
    result = api.run_tests(routed_scope)
    assert not result["ok"] and "second.test.yaml" in result["error"]
    assert "Nothing was run" in result["error"]
    assert api._jobs == {} and api._active_job is None


@pytest.mark.parametrize("malformed", [
    [], None, "bad", 42, b"\xff", {"test_file": []}, {"status": {}},
    {"steps": None}, {"steps": ["bad"]}, {"steps": [{"status": []}]},
    {"tokens": []}, {"tokens": {"total_tokens": "bad"}},
    {"duration_s": "bad"}, {"duration_s": float("nan")}, {"duration_s": float("inf")},
])
def test_malformed_history_does_not_break_commands_or_hide_valid_runs(tmp_path, monkeypatch, malformed):
    api = ArgusAPI(tmp_path)
    api.init_project()
    directory = tmp_path / ".argus" / "runs"
    good = {"test_file": "smoke.test.yaml", "status": "fail", "steps": [{"status": "fail"}]}
    (directory / "good.json").write_text(json.dumps(good))
    if isinstance(malformed, bytes):
        (directory / "bad.json").write_bytes(malformed)
    else:
        (directory / "bad.json").write_text(json.dumps(malformed))
    monkeypatch.setattr(ArgusConfig, "make_provider", lambda self, tracker=None: FakeProvider(["A step failed."]))
    assert load_runs(tmp_path) == [good]
    assert api.list_tests()[0]["last"] == "fail"
    assert api.interpret("/help")["intent"] == "help"
    assert api.interpret("/report")["intent"] == "report"
    assert [r["id"] for r in api.recent_runs()] == ["history:good.json"]
    assert not api.explain("history:bad.json")["ok"]
    assert api.explain("history:good.json")["ok"]


def test_rejected_cli_launch_is_an_error_without_nonexistent_report(tmp_path, monkeypatch):
    api = ArgusAPI(tmp_path)
    api.init_project()
    monkeypatch.setattr(ArgusConfig, "make_provider", lambda self, tracker=None: FakeProvider([]))
    cfg = api._config()
    cfg.knowledge.enabled = False
    monkeypatch.setattr(api, "_config", lambda provider=None: cfg)
    # An embedded NUL is rejected by subprocess before any program is launched.
    job = _wait(api, api.start_roam("invalid\0program", "cli", memory=False)["job"]["id"])
    assert job["status"] == "error" and not job["running"]
    assert job["stopped_reason"].startswith("launch failed:")
    assert job["report"] is None
    evidence = api.evidence(job["key"])
    assert evidence["verified"]
    assert {row["k"]: row["v"] for row in evidence["rows"]}["effective status"] == "error"
