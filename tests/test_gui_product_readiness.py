"""Desktop first use and provisioning integration without native VM allocation."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from argus.config import CapsuleConfig, ExecutionConfig, load_config
from argus.gui.app import ArgusAPI, run_gui


def test_unselected_project_never_initializes_launch_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    api = ArgusAPI(project_required=True)
    info = api.app_info()
    assert info["project_required"] and not info["initialized"]
    assert info["project"] == ""
    with pytest.raises(ValueError, match="Open a project folder"):
        api.init_project()
    assert not (tmp_path / ".argus").exists()
    assert api.help()["groups"]


@pytest.mark.parametrize("config", ["providers: [\napi_key: sentinel-secret", "[]", "budgets: {time_minutes: nope}"])
def test_invalid_configuration_is_visible_without_echoing_config_secrets(tmp_path, config):
    directory = tmp_path / ".argus"
    directory.mkdir()
    (directory / "config.yaml").write_text(config)
    info = ArgusAPI(tmp_path).app_info()
    assert info["ok"] is False and not info["project_required"]
    assert "config.yaml" in info["error"]
    assert "sentinel-secret" not in json.dumps(info)
    with pytest.raises(ValueError, match="config.yaml") as failure:
        ArgusAPI(tmp_path).run_tests()
    assert "sentinel-secret" not in str(failure.value)


def test_gui_setup_sample_runs_real_cli_and_verifies_evidence(tmp_path, monkeypatch):
    from tests.test_gui_api import _wait
    from tests.conftest import FakeProvider
    from argus.config import ArgusConfig

    monkeypatch.setenv("ARGUS_GUI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("ARGUS_EXECUTION_ENVIRONMENT", raising=False)
    monkeypatch.setattr(ArgusConfig, "make_provider", lambda self, tracker=None: FakeProvider([]))
    api = ArgusAPI(tmp_path)
    assert api.init_project()["ok"]
    assert not (tmp_path / ".argus" / "notepad.test.yaml").exists()
    sample = tmp_path / ".argus" / "smoke.test.yaml"
    initial = sample.read_bytes()
    api.init_project()
    assert sample.read_bytes() == initial
    cfg = load_config(tmp_path)
    cfg.knowledge.enabled = False
    monkeypatch.setattr(api, "_config", lambda provider=None: cfg)
    result = _wait(api, api.run_tests(["smoke.test.yaml"])["job"]["id"])
    run = result["runs"][0]
    assert run["status"] == "pass"
    evidence = api.evidence(run["key"])
    assert evidence["ok"] and evidence["state"] == "bound_verified"
    assert run["result"]["tokens"]["calls"] == 0


@pytest.mark.parametrize("kind", ["run", "roam"])
def test_queued_job_uses_captured_config_when_project_config_changes(tmp_path, monkeypatch, kind):
    from tests.test_gui_api import _wait
    from tests.conftest import FakeProvider
    from argus.config import ArgusConfig

    monkeypatch.setattr(ArgusConfig, "make_provider", lambda self, tracker=None: FakeProvider([]))
    monkeypatch.setenv("ARGUS_GUI_STATE_DIR", str(tmp_path / "state"))
    api = ArgusAPI(tmp_path)
    api.init_project()
    config = tmp_path / ".argus" / "config.yaml"
    config.write_text(config.read_text() + "\nknowledge:\n  enabled: false\n")
    target = yaml.safe_load((tmp_path / ".argus" / "smoke.test.yaml").read_text())["target"]["launch"]
    begin = api._begin_job

    def start_then_edit(job):
        result = begin(job)
        config.write_text("providers: [\napi_key: sentinel-secret\n")
        return result

    monkeypatch.setattr(api, "_begin_job", start_then_edit)
    started = api.run_tests(["smoke.test.yaml"]) if kind == "run" else api.start_roam(target, "cli", minutes=0.01, memory=False)
    job = _wait(api, started["job"]["id"])
    assert (job["runs"][0]["status"] if kind == "run" else job["status"]) == ("pass" if kind == "run" else "done")
    assert "sentinel-secret" not in json.dumps(job)


def test_knowledge_close_failure_does_not_strand_roam_worker(tmp_path, monkeypatch):
    from argus.config import ArgusConfig
    from argus.engine.roam_impl import RoamSession
    from tests.test_gui_api import _wait
    from tests.conftest import FakeProvider

    class Store:
        def get_stats(self, target):
            return {}

        def close(self):
            raise OSError("sentinel-secret")

    monkeypatch.setattr(ArgusConfig, "make_provider", lambda self, tracker=None: FakeProvider([]))
    monkeypatch.setattr(ArgusConfig, "make_knowledge_store", lambda self: Store())
    def completed_roam(**kwargs):
        session = RoamSession(target=kwargs["target"], provider="fake")
        session.execution_status = "pass"
        return session

    monkeypatch.setattr("argus.engine.roam.roam", completed_roam)
    api = ArgusAPI(tmp_path)
    api.init_project()
    job = _wait(api, api.start_roam("echo ready", "cli")["job"]["id"])
    assert job["status"] == "done" and not job["running"]
    assert any("Knowledge state could not be saved" in line for line in job["log"])
    assert "sentinel-secret" not in json.dumps(job)
    assert api._active_ks is None and api._job_tracker is None


@pytest.mark.parametrize("blocked", [False, True])
def test_derived_environment_card_preserves_generation_path_and_reports_readiness(tmp_path, monkeypatch, blocked):
    cfg = load_config(tmp_path)
    cfg.execution = ExecutionConfig("capsule", CapsuleConfig(provider="libvirt",
        environment_definition="env.yaml", image_cache_root="images", guest_token_ref="secret://legacy"))
    inspected = []

    def security():
        if blocked:
            raise ValueError("Protected delivery has not been verified.")

    env = SimpleNamespace(settings=SimpleNamespace(environment_id="env-sha256-" + "a" * 64),
        provider=object(), _validate_provider_host_platform=lambda provider: None,
        _validate_provisioned_security=security,
        prepare=lambda: pytest.fail("readiness inspection must not create a VM"))

    def factory(adapter, kind, overrides):
        inspected.append((adapter, kind, overrides))
        return env

    monkeypatch.setattr(cfg, "make_execution_environment", factory)
    api = ArgusAPI(tmp_path)
    monkeypatch.setattr(api, "_config", lambda provider=None: cfg)
    card = api.environment()
    rows = {row["k"]: row["v"] for row in card["rows"]}
    assert rows["image source"] == "verified ISO-derived cache"
    assert rows["image"] == "images" and rows["definition"] == "env.yaml"
    assert "fresh generation" in rows["guest control"]
    assert (rows["configuration"] == "not ready") == blocked
    assert "secret://legacy" not in json.dumps(card)
    assert inspected == [("cli", "capsule", {"provider": "libvirt", "retain_on_failure": False})]
    if blocked:
        assert "No VM was created" in card["note"]


class _Event(list):
    def __iadd__(self, handler):
        self.append(handler)
        return self


def test_native_project_windows_have_separate_state_and_active_close_waits(tmp_path, monkeypatch):
    windows = []

    def create_window(title, **kwargs):
        window = SimpleNamespace(title=title, api=kwargs["js_api"],
            events=SimpleNamespace(closing=_Event()), selected=None, scripts=[])
        window.create_file_dialog = lambda kind: window.selected
        window.evaluate_js = lambda script: window.scripts.append(script)
        windows.append(window)
        return window

    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(create_window=create_window,
        start=lambda: None, FileDialog=SimpleNamespace(FOLDER="folder")))
    monkeypatch.chdir(tmp_path)
    run_gui()
    first = windows[0]
    assert first.api.app_info()["project_required"]
    assert first.api.open_project() == {"ok": True, "cancelled": True}
    project = tmp_path / "selected"
    project.mkdir()
    first.selected = [str(project)]
    assert first.api.open_project()["ok"]
    second = windows[1]
    assert second.api.app_info()["project"] == str(project.resolve())
    assert second.api._jobs is not first.api._jobs
    assert second.api._drafts is not first.api._drafts
    assert second.api._persist_lock is first.api._persist_lock
    assert first.api.app_info()["project_required"]
    assert not (tmp_path / ".argus").exists()
    second.api._active_job = "active"
    second.api._jobs["active"] = {"running": True}
    assert second.events.closing[0]() is False
    assert second.api._stop.is_set() and second.scripts
    second.evaluate_js = lambda script: (_ for _ in ()).throw(RuntimeError("bridge unavailable"))
    assert second.events.closing[0]() is False
    second.api._jobs["active"]["running"] = False
    assert second.events.closing[0]() is True
    assert "closing" in second.api._begin_job({"id": "late", "running": True})
    assert second.api.watch_start()["ok"] is False
