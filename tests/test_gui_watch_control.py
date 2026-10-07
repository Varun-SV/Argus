"""Watch authorization/file versions and truthful run cancellation outcomes."""
import os

import pytest
import yaml

from argus.config import ArgusConfig
from argus.gui import app, assistant
from argus.gui.app import ArgusAPI, _run_card_status, _test_versions
from argus.engine.results import RunResult, load_runs
from tests.conftest import FakeProvider
from tests.test_gui_api import CLI_SPEC, _wait


@pytest.mark.parametrize("command,action", [("/watch", "start"), (" /watch START ", "start"),
                                             ("/watch stop", "stop"), ("/watch STOP ", "stop")])
def test_watch_accepts_only_explicit_supported_commands(command, action):
    assert assistant.parse_slash(command)["args"]["action"] == action


@pytest.mark.parametrize("argument", ["stpo", "stop please", "start stop", "off", "--stop", "start now"])
def test_invalid_watch_arguments_are_errors_not_start_requests(tmp_path, argument):
    with pytest.raises(assistant.IntentError, match="/watch start"):
        assistant.parse_slash("/watch " + argument)
    api = ArgusAPI(tmp_path)
    api.init_project()
    result = api.interpret("/watch " + argument)
    assert result["intent"] == "error" and "/watch stop" in result["args"]["text"]
    assert not api.watch_status()["running"] and api._jobs == {}


@pytest.mark.parametrize("text", [
    "don't watch tests", "dont watch tests", "don\u2019t watch tests", "never start watch",
    "please do not start watch", "do not enable watch", "never stop watch",
    "please do not stop watch", "don't turn off watch", "do not monitor test changes",
    "please avoid watching tests", "could you please not start watch",
    "no watch for tests", "I can't watch tests", "I won't start watch",
    "you shouldn't enable watch", "you mustn't stop watch", "continue without starting watch",
])
@pytest.mark.parametrize("model_action", ["start", "stop"])
def test_negation_fences_model_watch_intents(text, model_action):
    result = assistant.validate_intent({"intent": "watch", "args": {"action": model_action}}, text, {})
    assert result["intent"] == "chat"


@pytest.mark.parametrize("text,action", [("please watch tests", "start"), ("start watch", "start"),
                                         ("enable watch for test changes", "start"),
                                         ("stop watch", "stop"), ("please turn off watch", "stop")])
def test_positive_watch_authorization_remains_supported(text, action):
    assert assistant.validate_intent({"intent": "watch"}, text, {})["args"]["action"] == action


def configured_api(tmp_path, monkeypatch):
    api = ArgusAPI(tmp_path)
    api.init_project()
    cfg = api._config()
    cfg.knowledge.enabled = False
    monkeypatch.setattr(api, "_config", lambda provider=None: cfg)
    monkeypatch.setattr(ArgusConfig, "make_provider", lambda self, tracker=None: FakeProvider([]))
    return api


@pytest.mark.parametrize("phase", ["return", "save"])
def test_late_stop_preserves_real_failure_and_stops_only_queued_runs(tmp_path, monkeypatch, phase):
    from argus.engine import runner
    api = configured_api(tmp_path, monkeypatch)
    spec = yaml.safe_load(CLI_SPEC)
    spec["steps"][1]["assert"]["stdout_contains"] = "absent sentinel"
    (tmp_path / ".argus" / "fail.test.yaml").write_text(yaml.safe_dump(spec))
    (tmp_path / ".argus" / "queued.test.yaml").write_text(CLI_SPEC)
    if phase == "return":
        original = runner.run_test
        def return_after_stop(*args, **kwargs):
            result = original(*args, **kwargs)
            api.stop()
            return result
        monkeypatch.setattr(runner, "run_test", return_after_stop)
    else:
        original = RunResult.save
        def save_then_stop(result, project_dir):
            path = original(result, project_dir)
            api.stop()
            return path
        monkeypatch.setattr(RunResult, "save", save_then_stop)
    job = _wait(api, api.run_tests(["fail.test.yaml", "queued.test.yaml"])["job"]["id"])
    failed, queued = job["runs"]
    assert failed["status"] == failed["result"]["status"] == "fail"
    assert queued["status"] == "stopped" and queued.get("result") is None
    assert api._results[api._last_failed]["status"] == "fail"
    assert load_runs(tmp_path)[0]["status"] == "fail"
    evidence = api.evidence(failed["key"])
    assert evidence["ok"] and evidence["verified"]


def test_real_user_interruption_is_stopped_from_recorded_result(tmp_path, monkeypatch):
    from argus.engine import runner
    api = configured_api(tmp_path, monkeypatch)
    (tmp_path / ".argus" / "interrupt.test.yaml").write_text(CLI_SPEC)
    original = runner.check_assertion
    def stop_after_assertion(*args, **kwargs):
        step = original(*args, **kwargs)
        api.stop()  # Next step observes Stop and records its interruption.
        return step
    monkeypatch.setattr(runner, "check_assertion", stop_after_assertion)
    job = _wait(api, api.run_tests(["interrupt.test.yaml"])["job"]["id"])
    run = job["runs"][0]
    assert run["status"] == "stopped"
    assert run["result"]["steps"][1]["note"] == "skipped: stopped by you"
    assert not any(step["status"] in ("fail", "error") for step in run["result"]["steps"])


@pytest.mark.parametrize("data,expected", [
    ({"status": "fail", "steps": []}, "fail"),
    ({"status": "error", "steps": [{"status": "skipped", "note": "skipped: stopped by you"}]}, "error"),
    ({"status": "fail", "steps": [{"status": "fail"}, {"status": "skipped", "note": "skipped: stopped by you"}]}, "fail"),
    ({"status": "fail", "error": "ATES finalization failed", "steps": [{"status": "skipped", "note": "skipped: stopped by you"}]}, "fail"),
    ({"status": "fail", "steps": [{"status": "skipped", "note": "skipped: time limit reached"}]}, "fail"),
    ({"status": "fail", "steps": [{"kind": "teardown", "status": "pass", "note": "run budget exhausted before teardown: stopped by you"}]}, "stopped"),
    ({"status": "pass", "steps": []}, "pass"),
])
def test_interruption_never_hides_failures_errors_or_other_budget_limits(data, expected):
    assert _run_card_status(data) == expected


@pytest.mark.parametrize("replacement", [False, True])
def test_watch_detects_same_timestamp_same_size_edit_once(tmp_path, monkeypatch, replacement):
    directory = tmp_path / ".argus"
    directory.mkdir()
    path = directory / "sample.test.yaml"
    path.write_text("name: Before\n")
    original = path.stat()
    api = ArgusAPI(tmp_path)
    watch = {"id": "w", "running": True, "events": []}
    ticks, runs = [], []
    def poll_sleep(seconds):
        ticks.append(True)
        if len(ticks) == 1:
            destination = path.with_suffix(".replacement") if replacement else path
            destination.write_text("name: AfterX\n")
            os.utime(destination, ns=(original.st_atime_ns, original.st_mtime_ns))
            if replacement:
                destination.replace(path)
            assert path.stat().st_size == original.st_size
            assert path.stat().st_mtime_ns == original.st_mtime_ns
        elif len(ticks) == 3:
            watch["running"] = False
    def run_tests(names):
        runs.append(names)
        api._jobs["run"] = {"running": False, "runs": [{"status": "pass", "notes": [], "result": {"steps": []}}]}
        return {"ok": True, "job": {"id": "run"}}
    monkeypatch.setattr(app.time, "sleep", poll_sleep)
    monkeypatch.setattr(api, "run_tests", run_tests)
    api._watch_worker(watch, tmp_path, poll=0.01)
    assert runs == [[path.name]]
    assert len(watch["events"]) == 1 and watch["events"][0]["status"] == "pass"


def test_transient_read_failure_keeps_version_and_retry_detects_change(tmp_path, monkeypatch):
    (tmp_path / ".argus").mkdir()
    path = tmp_path / ".argus" / "sample.test.yaml"
    path.write_text("first")
    before = _test_versions(tmp_path)
    original = app._test_version
    def denied(path):
        raise PermissionError("temporarily locked")
    monkeypatch.setattr(app, "_test_version", denied)
    assert _test_versions(tmp_path, before) == before
    assert _test_versions(tmp_path) == {path.name: None}
    path.write_text("other")
    monkeypatch.setattr(app, "_test_version", original)
    assert _test_versions(tmp_path, before) != before
    path.unlink()
    assert _test_versions(tmp_path, before) == {}


def test_unstable_read_retries_instead_of_accepting_mixed_snapshot(tmp_path, monkeypatch):
    (tmp_path / ".argus").mkdir()
    path = tmp_path / ".argus" / "sample.test.yaml"
    path.write_text("first")
    before = _test_versions(tmp_path)
    original = app.os.fstat
    calls = []
    def change_during_read(fd):
        calls.append(True)
        if len(calls) == 2:
            path.write_text("second")
        return original(fd)
    monkeypatch.setattr(app.os, "fstat", change_during_read)
    assert _test_versions(tmp_path, before) == before
    monkeypatch.setattr(app.os, "fstat", original)
    assert _test_versions(tmp_path, before) != before


@pytest.mark.skipif(os.name == "nt", reason="POSIX nonblocking special-file check")
def test_special_test_file_cannot_block_watching(tmp_path):
    (tmp_path / ".argus").mkdir()
    path = tmp_path / ".argus" / "fifo.test.yaml"
    os.mkfifo(path)
    assert _test_versions(tmp_path) == {path.name: None}
