"""Regressions for the review of head 585f16b (history, run dirs, /stop, knowledge paths)."""

from __future__ import annotations

import json

import pytest

from argus.config import init_project
from argus.engine.results import RunResult, load_runs
from argus.gui.app import ArgusAPI


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for var in ("ARGUS_PROVIDER", "ARGUS_MODEL", "ARGUS_EXECUTION_ENVIRONMENT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ARGUS_GUI_STATE_DIR", str(tmp_path / "user-state"))
    init_project(tmp_path)
    return tmp_path


def _save(project, name, status="pass", started_at=1_700_000_000.0):
    result = RunResult(test_name=name, test_file=name, adapter="cli", provider="fake",
                       status=status, started_at=started_at)
    result.save(project)
    return result


def test_history_limit_counts_only_valid_records(project):
    _save(project, "older.test.yaml", started_at=1_700_000_000.0)
    runs = project / ".argus" / "runs"
    # Newer than every valid row and malformed: they must not use up the limit.
    for i in range(8):
        (runs / f"20991231-2359{i:02d}-{'9' * 20}-{'a' * 32}-bad{i}.json").write_text("[]", encoding="utf-8")

    assert [r["test_file"] for r in load_runs(project, 8)] == ["older.test.yaml"]
    api = ArgusAPI()
    assert [r["test_file"] for _, r in api._history(8)] == ["older.test.yaml"]


def test_long_test_filename_still_saves_a_bounded_run_directory(project):
    name = "x" * 190 + ".test.yaml"
    result = _save(project, name)
    run_dir = result.run_dir(project)
    assert (run_dir / "result.json").is_file()
    assert len(run_dir.name.encode()) + len(".json") < 255
    assert [r["test_file"] for r in load_runs(project, 5)] == [name]


def test_distinct_long_names_do_not_collide(project):
    a = _save(project, "y" * 200 + "-a.test.yaml").run_dir(project).name
    b = _save(project, "y" * 200 + "-b.test.yaml").run_dir(project).name
    assert a.rsplit("-", 1)[1] != b.rsplit("-", 1)[1]


def test_stop_works_while_the_config_is_malformed(project):
    api = ArgusAPI()
    (project / ".argus" / "config.yaml").write_text("provider: [unclosed\n", encoding="utf-8")
    assert api.interpret("/stop")["intent"] == "stop"
    assert api.interpret("/help")["intent"] == "help"


def test_relative_persist_dir_is_anchored_to_the_opened_project(tmp_path, monkeypatch):
    for var in ("ARGUS_PROVIDER", "ARGUS_MODEL", "ARGUS_EXECUTION_ENVIRONMENT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ARGUS_GUI_STATE_DIR", str(tmp_path / "user-state"))
    launch, opened = tmp_path / "launch", tmp_path / "opened"
    for root in (launch, opened):
        root.mkdir()
        init_project(root)
        cfg = root / ".argus" / "config.yaml"
        cfg.write_text(cfg.read_text() + "\nknowledge:\n  type: json\n  persist_dir: graphs\n",
                       encoding="utf-8")
        (root / "graphs").mkdir()
    (launch / "graphs" / "launch-only.graph.json").write_text(json.dumps({}), encoding="utf-8")
    monkeypatch.chdir(launch)

    api = ArgusAPI(opened)
    assert api._config().knowledge_persist_dir() == opened / "graphs"
    out = api.knowledge("launch-only")
    assert not out["ok"] and out.get("targets", []) == []
    reset = api.knowledge_reset("launch-only")
    assert not reset["ok"]
    assert (launch / "graphs" / "launch-only.graph.json").exists()
    assert not api.knowledge_export("launch-only")["ok"]
