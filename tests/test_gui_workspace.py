import json
import shlex
import sys

import yaml

from argus.config import load_config
from argus.gui.app import ArgusAPI
from tests.test_gui_api import _wait


def test_desktop_setup_runs_project_command_without_hosted_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_GUI_STATE_DIR", str(tmp_path / "state"))
    for key in ("ARGUS_PROVIDER", "ARGUS_API_KEY", "OPENAI_API_KEY", "ARGUS_EXECUTION_ENVIRONMENT"):
        monkeypatch.delenv(key, raising=False)
    project = tmp_path / "project"
    project.mkdir()
    api = ArgusAPI(project)
    assert api.init_project()["ok"]
    settings = api.desktop_settings()
    assert api.save_desktop_settings("model", settings["revision"], {
        "provider": "openai", "model": "test-model", "use_by_default": True,
    })["ok"]
    (project / "sample.py").write_text("print('working inside the project')\n")
    form = api.test_form()["form"]
    form.update(name="Relative command", target=f"{shlex.quote(sys.executable)} sample.py")
    form["rows"].append({"phase": "steps", "kind": "assert", "assertion": "stdout_contains",
                         "value": "working inside the project"})
    saved = api.save_test_form(form)
    assert saved["ok"] and not saved["requires_model"]
    started = api.run_tests([saved["file"]])
    run = _wait(api, started["job"]["id"])["runs"][0]
    assert run["status"] == "pass"
    assert run["result"]["tokens"]["calls"] == 0
    assert api.evidence(run["key"])["verified"]
    reopened = ArgusAPI(project)
    assert reopened.recent_runs()[0]["evidence_key"] == run["key"]
    assert reopened.evidence(run["key"])["verified"]


def test_model_secret_round_trip_and_stale_form_cannot_overwrite(tmp_path, monkeypatch):
    monkeypatch.setattr("argus.secrets.default_secret_dir", lambda: tmp_path / "data")
    for key in ("ARGUS_PROVIDER", "ARGUS_MODEL", "ARGUS_API_KEY", "ARGUS_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    api = ArgusAPI(tmp_path)
    assert api.desktop_settings()["revision"] == ""
    sentinel = "credential-marker-4f98"
    selected = api.save_desktop_settings("model", "", {
        "provider": "openai", "model": "project-model", "api_key": sentinel, "use_by_default": True,
    })
    assert selected["ok"] and selected["revision"]
    config = tmp_path / ".argus" / "config.yaml"
    assert sentinel not in config.read_text()
    assert sentinel not in json.dumps(selected)
    assert ArgusAPI(tmp_path).desktop_settings()["effective_model"] == "project-model"
    assert load_config(tmp_path).provider.api_key == sentinel
    other = tmp_path / "other-project"
    (other / ".argus").mkdir(parents=True)
    (other / ".argus" / "config.yaml").write_bytes(config.read_bytes())
    assert load_config(other).provider.api_key == ""
    assert not api.save_desktop_settings("model", "", {
        "provider": "openai", "model": "stale-model",
    })["ok"]


def test_test_editor_preserves_phases_and_rejects_concurrent_edits(tmp_path):
    api = ArgusAPI(tmp_path)
    api.init_project()
    path = tmp_path / ".argus" / "smoke.test.yaml"
    original = yaml.safe_load(path.read_text())
    original["setup"] = [{"assert": {"exit_code_is": 0}}]
    original["future_setting"] = {"mode": "preserved"}
    path.write_text(yaml.safe_dump(original))
    form = api.test_form(path.name)
    assert form["form"]["rows"][0]["phase"] == "setup"
    form["form"]["name"] = "Edited command"
    assert api.save_test_form(form["form"], path.name, form["revision"])["ok"]
    saved = yaml.safe_load(path.read_text())
    assert saved["setup"] == original["setup"]
    assert saved["future_setting"] == original["future_setting"]
    assert not api.save_test_form(form["form"], path.name, form["revision"])["ok"]


def test_broken_settings_recover_in_app_with_private_backup(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_GUI_STATE_DIR", str(tmp_path / "user"))
    api = ArgusAPI(tmp_path)
    api.init_project()
    path = tmp_path / ".argus" / "config.yaml"
    invalid = b"provider: [\nsecret: SENTINEL"
    path.write_bytes(invalid)
    repair = api.desktop_settings()
    assert repair["recoverable"] and "SENTINEL" not in json.dumps(repair)
    assert api.restore_desktop_settings(repair["revision"])["ok"]
    assert next((tmp_path / "user" / "settings-recovery").glob("*.yaml")).read_bytes() == invalid
    assert api.app_info()["ok"]


def test_windows_iso_preflight_keeps_protected_bootstrap_gate(tmp_path):
    api = ArgusAPI(tmp_path)
    assert "protected Hyper-V bootstrap" in api.image_build_status()["hosts"]["windows-11"]
    assert not api.start_image_build({"os": "windows-11"})["ok"]
    assert api.image_build_status()["state"] == "idle"
