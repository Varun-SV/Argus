"""Explicit duration/negation policy and per-execution result persistence."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
from types import SimpleNamespace

import pytest

from argus.config import ArgusConfig
from argus.engine import results
from argus.engine.results import RunResult, load_runs
from argus.gui import app, assistant
from argus.gui.app import ArgusAPI
from tests.conftest import FakeProvider
from tests.test_gui_api import CLI_SPEC, _wait


@pytest.mark.parametrize("phrase", [
    "for 300 minutes", "for 240.1 minutes", "for 5 hours", "for 14401 seconds", "for 10000 minutes",
])
@pytest.mark.parametrize("leading", [True, False])
def test_free_text_over_limit_roam_requests_require_clarification(phrase, leading):
    text = f"{phrase}, roam notepad.exe" if leading else f"roam notepad.exe {phrase}"
    routed = assistant.validate_intent({"intent": "roam", "args": {"target": "notepad.exe", "minutes": 1}}, text, {})
    assert routed["intent"] == "chat" and "240" in routed["args"]["reply"]


@pytest.mark.parametrize("phrase", ["for 240 minutes", "for 4 hours", "for 14400 seconds"])
def test_exact_limit_is_supported_without_shortening(phrase):
    got = assistant.validate_intent({"intent": "roam", "args": {"target": "notepad.exe"}},
                                    "roam notepad.exe " + phrase, {})
    assert got["intent"] == "roam" and got["args"]["minutes"] == 240


def test_over_limit_phrase_inside_quoted_command_is_not_a_roam_modifier():
    target = "python tool.py for 300 minutes"
    got = assistant.validate_intent({"intent": "roam", "args": {"target": target}}, f'roam "{target}"', {})
    assert got["intent"] == "roam" and got["args"]["target"] == target
    assert got["args"]["minutes"] is None


@pytest.mark.parametrize("duration", [240.1, 300, float("inf")])
def test_over_limit_gui_duration_and_project_default_never_allocate(tmp_path, monkeypatch, duration):
    api = ArgusAPI(tmp_path)
    api.init_project()
    cfg = api._config()
    cfg.time_minutes = duration
    monkeypatch.setattr(api, "_config", lambda *args, **kwargs: cfg)
    assert not api.start_roam("notepad.exe", minutes=duration)["ok"]
    assert not api.start_roam("notepad.exe")["ok"]
    assert api._jobs == {}
    with pytest.raises(assistant.IntentError):
        assistant.parse_slash(f"/roam notepad.exe --minutes {duration}", [], "")


@pytest.mark.parametrize("action,command", [
    ("stop", "stop the run"), ("init", "initialize the Argus project"),
    ("save_test", "save this draft"), ("write_test", "create a test for login"),
])
@pytest.mark.parametrize("prefix", [
    "no need to", "you should not", "I would rather not", "please avoid", "I cannot",
    "I can't", "I won't", "I shouldn’t", "I mustn’t", "do not", "never", "without",
])
def test_broader_negations_cannot_authorize_simple_actions(action, command, prefix):
    text = prefix + " " + command
    got = assistant.validate_intent({"intent": action, "args": {"description": "model invented task"}}, text, {})
    assert got["intent"] == "chat"


@pytest.mark.parametrize("action,text", [
    ("stop", "please stop the run"), ("init", "can you initialize the Argus project"),
    ("save_test", "please save this draft"), ("write_test", "write a test for login"),
    ("write_test", "write a test ensuring users cannot create accounts"),
    ("write_test", "write a test verifying users should not create a test without permission"),
    ("write_test", 'write a test named "Never create a test"'),
    ("write_test", 'write a test named "I shouldn’t create an account"'),
])
def test_positive_actions_and_negative_assertions_in_drafts_remain_usable(action, text):
    got = assistant.validate_intent({"intent": action}, text, {})
    assert got["intent"] == action
    if action == "write_test":
        assert got["args"]["description"] == text


def test_negated_draft_only_makes_the_routing_call(tmp_path, monkeypatch):
    api = ArgusAPI(tmp_path)
    api.init_project()
    provider = FakeProvider([json.dumps({"intent": "write_test", "args": {"description": "paid task"}})])
    monkeypatch.setattr(ArgusConfig, "make_provider", lambda *args, **kwargs: provider)
    routed = api.interpret("I would rather not write a test for login")
    assert routed["intent"] == "chat" and len(provider.calls) == 1
    assert api._draft is None


def _result(status="pass"):
    return RunResult(test_name="Repeated", test_file="b.test.yaml", adapter="cli", provider="fake",
                     started_at=1700000000.0, status=status)


@pytest.mark.parametrize("ticks", [(123, 123), (123, 100)])
def test_clock_ties_and_rollback_keep_history_order(tmp_path, monkeypatch, ticks):
    clock = iter(ticks)
    monkeypatch.setattr(results, "_last_storage_order", 0)
    monkeypatch.setattr(results.time, "time_ns", lambda: next(clock))
    first, second = _result(), _result("fail")
    assert second._storage_order > first._storage_order
    second.save(tmp_path)
    first.save(tmp_path)
    assert [row["status"] for row in load_runs(tmp_path)] == ["fail", "pass"]


def test_concurrent_allocations_are_monotonic_with_a_coarse_clock(monkeypatch):
    monkeypatch.setattr(results, "_last_storage_order", 0)
    monkeypatch.setattr(results.time, "time_ns", lambda: 123)
    with ThreadPoolExecutor(max_workers=8) as pool:
        orders = list(pool.map(lambda _: _result()._storage_order, range(32)))
    assert sorted(orders) == list(range(123, 155))


def test_same_second_repeated_results_keep_both_reports_history_and_original_bytes(tmp_path):
    first, second = _result(), _result("fail")
    first_path = first.save(tmp_path)
    original = first_path.read_bytes()
    original_report = (first_path.parent / "report.md").read_bytes()
    second_path = second.save(tmp_path)
    assert first_path != second_path
    assert first_path.read_bytes() == original
    assert (first_path.parent / "report.md").read_bytes() == original_report
    assert json.loads(second_path.read_text())["status"] == "fail"
    assert [r["status"] for r in load_runs(tmp_path)] == ["fail", "pass"]
    assert [r["status"] for r in load_runs(tmp_path, 1)] == ["fail"]
    api = ArgusAPI(tmp_path)
    rows = api.recent_runs()
    assert [r["status"] for r in rows] == ["fail", "pass"]
    assert len({r["id"] for r in rows}) == 2
    assert [api._history_result(row["id"][len("history:"):])["status"] for row in rows] == ["fail", "pass"]


def test_resave_stays_in_same_owned_directory_and_history_entry(tmp_path):
    result = _result()
    directory = result.run_dir(tmp_path)
    assert result.run_dir(tmp_path) == directory
    first_path = result.save(tmp_path)
    result.status = "fail"
    assert result.save(tmp_path) == first_path
    assert len(load_runs(tmp_path)) == 1
    assert load_runs(tmp_path)[0]["status"] == "fail"
    assert all(not key.startswith("_storage") for key in result.to_dict())


@pytest.mark.parametrize("preexisting", ["directory", "flat_history"])
def test_generated_name_collision_never_adopts_or_overwrites_existing_resource(tmp_path, monkeypatch, preexisting):
    monkeypatch.setattr(results, "_next_storage_order", lambda: 123)
    monkeypatch.setattr(results.uuid, "uuid4", lambda: SimpleNamespace(hex="fixed"))
    first = _result()
    first_path = first.save(tmp_path)
    original = first_path.read_bytes()
    second = _result("fail")
    flat = first_path.parent.parent / (first_path.parent.name + ".json")
    if preexisting == "flat_history":
        first_path.unlink()
        (first_path.parent / "report.md").unlink()
        first_path.parent.rmdir()
    else:
        flat.unlink()
    monkeypatch.setattr(results.uuid, "uuid4", lambda: SimpleNamespace(hex="fresh"))
    second_path = second.save(tmp_path)
    assert second_path != first_path
    if preexisting == "directory":
        assert first_path.read_bytes() == original
    else:
        assert flat.read_bytes() == original


def test_exhausted_collisions_preserve_existing_result(tmp_path, monkeypatch):
    monkeypatch.setattr(results, "_next_storage_order", lambda: 123)
    monkeypatch.setattr(results.uuid, "uuid4", lambda: SimpleNamespace(hex="fixed"))
    first_path = _result().save(tmp_path)
    original = first_path.read_bytes()
    with pytest.raises(OSError, match="reserve a unique"):
        _result("fail").save(tmp_path)
    assert first_path.read_bytes() == original and len(load_runs(tmp_path)) == 1


def test_replaced_reserved_directory_is_not_adopted(tmp_path):
    result = _result()
    directory = result.run_dir(tmp_path)
    directory.rename(directory.with_name(directory.name + "-retained"))
    directory.mkdir()
    sentinel = directory / "unrelated"
    sentinel.write_text("untouched")
    with pytest.raises(OSError, match="ownership changed"):
        result.save(tmp_path)
    assert sentinel.read_text() == "untouched"


def test_concurrent_resaves_of_same_result_use_one_history_entry(tmp_path):
    result = _result()
    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = list(pool.map(lambda _: result.save(tmp_path), range(4)))
    assert len(set(paths)) == 1 and len(load_runs(tmp_path)) == 1


def test_concurrent_saves_of_distinct_results_are_unique(tmp_path):
    barrier = threading.Barrier(8)
    def save(index):
        result = _result()
        result.error = str(index)
        barrier.wait()
        return result.save(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(pool.map(save, range(8)))
    assert len(set(paths)) == 8
    assert {json.loads(path.read_text())["error"] for path in paths} == {str(i) for i in range(8)}
    assert len(load_runs(tmp_path)) == 8


def test_legacy_history_remains_readable(tmp_path):
    directory = tmp_path / ".argus" / "runs"
    directory.mkdir(parents=True)
    legacy = _result().to_dict()
    path = directory / "20231114-221320-b.test.yaml.json"
    path.write_text(json.dumps(legacy))
    assert load_runs(tmp_path) == [legacy]
    assert ArgusAPI(tmp_path)._history_result(path.name)["test_file"] == "b.test.yaml"


def test_legacy_and_new_same_second_history_keep_recent_results_first(tmp_path):
    result = _result("fail")
    directory = tmp_path / ".argus" / "runs"
    directory.mkdir(parents=True)
    stamp = results.time.strftime("%Y%m%d-%H%M%S", results.time.localtime(result.started_at))
    legacy = _result().to_dict()
    (directory / f"{stamp}-b.test.yaml.json").write_text(json.dumps(legacy))
    result.save(tmp_path)
    assert [row["status"] for row in load_runs(tmp_path, 1)] == ["fail"]
    assert [row["status"] for row in ArgusAPI(tmp_path).recent_runs()] == ["fail", "pass"]


def test_repeated_gui_execution_persists_distinct_results_and_ates_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_GUI_STATE_DIR", str(tmp_path / "user-state"))
    api = ArgusAPI(tmp_path)
    api.init_project()
    cfg = api._config()
    cfg.knowledge.enabled = False
    monkeypatch.setattr(api, "_config", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(ArgusConfig, "make_provider", lambda *args, **kwargs: FakeProvider([]))
    (cfg.argus_dir / "b.test.yaml").write_text(CLI_SPEC)
    original_save = RunResult.save
    def same_second(result, project):
        result.started_at = 1700000000.0
        return original_save(result, project)
    monkeypatch.setattr(RunResult, "save", same_second)
    job = _wait(api, api.run_tests(["b.test.yaml", "b.test.yaml"])["job"]["id"])
    assert [run["status"] for run in job["runs"]] == ["pass", "pass"]
    keys = [run["key"] for run in job["runs"]]
    assert len(set(keys)) == 2 and len(load_runs(tmp_path)) == 2
    for key in keys:
        assert api.evidence(key)["verified"]
