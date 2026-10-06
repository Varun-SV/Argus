"""PR review regressions at desktop ownership and evidence trust boundaries."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from argus.gui.app import ArgusAPI
from argus.gui.state import ProjectInUse, ProjectLease, project_identity


def test_watch_completed_history_is_bounded_without_dropping_pending_changes():
    from argus.gui.app import _settle_watch_event
    pending = [{"file": str(i), "status": "waiting"} for i in range(7)]
    watch = {"events": pending[:], "settled_count": 0}
    for i in range(500):
        event = {"file": str(i), "status": "running"}
        watch["events"].append(event)
        _settle_watch_event(watch, event, "pass", "completed")
    assert watch["settled_count"] == 500
    assert watch["events"][:7] == pending
    assert len(watch["events"]) == 207
    assert [e["file"] for e in watch["events"][7:]] == [str(i) for i in range(300, 500)]


def test_project_lease_excludes_other_process_and_survives_stale_lock_file(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_GUI_STATE_DIR", str(tmp_path / "state"))
    code = """import sys
from pathlib import Path
from argus.gui.state import ProjectLease, ProjectInUse
try:
    lease = ProjectLease(Path(sys.argv[1]))
except ProjectInUse:
    print('busy', flush=True)
else:
    print('owned', flush=True)
    sys.stdin.readline()
"""
    lease = ProjectLease(tmp_path)
    try:
        child = subprocess.run([sys.executable, "-c", code, str(tmp_path)],
                               input="\n", capture_output=True, text=True, timeout=10)
        assert child.returncode == 0 and child.stdout.strip() == "busy", child.stderr
    finally:
        lease.close()
    child = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "owned"
        with pytest.raises(ProjectInUse):
            ProjectLease(tmp_path)
    finally:
        child.kill()  # an OS crash must release ownership without deleting the file
        child.communicate(timeout=10)
    assert list((tmp_path / "state" / "window-locks").glob("*.lock"))
    recovered = ProjectLease(tmp_path)
    recovered.close()


def test_project_identity_normalizes_aliases(tmp_path):
    assert project_identity(tmp_path / ".") == project_identity(tmp_path)
    if os.name == "nt":
        assert project_identity(Path(str(tmp_path).swapcase())) == project_identity(tmp_path)


def test_chat_persistence_does_not_require_valid_model_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_GUI_STATE_DIR", str(tmp_path / "state"))
    api = ArgusAPI(tmp_path)
    api.init_project()
    config = tmp_path / ".argus" / "config.yaml"
    config.write_text("providers: [\napi_key: sentinel-secret\n")
    chats = [{"id": "latest", "msgs": [{"text": "latest response"}]}]
    assert api.app_info()["ok"] is False
    assert api.save_conversations(chats) == {"ok": True}
    assert ArgusAPI(tmp_path).load_conversations() == chats
    assert "sentinel-secret" not in json.dumps(api.app_info())


def test_failed_durable_save_ack_preserves_last_committed_history(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_GUI_STATE_DIR", str(tmp_path / "state"))
    api = ArgusAPI(tmp_path)
    old = [{"id": "committed", "msgs": []}]
    assert api.save_conversations(old)["ok"]
    def fail_sync(fd):
        raise OSError("sentinel-secret")
    monkeypatch.setattr(os, "fsync", fail_sync)
    failed = api.save_conversations([{"id": "latest", "msgs": []}])
    assert not failed["ok"] and "sentinel-secret" not in json.dumps(failed)
    assert api.load_conversations() == old
    assert not list((tmp_path / "state").rglob("*.tmp"))


def test_unrecognized_forced_environment_never_displays_ready_local_or_launches(tmp_path, monkeypatch):
    api = ArgusAPI(tmp_path)
    api.init_project()
    monkeypatch.setenv("ARGUS_EXECUTION_ENVIRONMENT", "capusle")
    info = api.app_info()
    assert not info["ok"] and info["environment"] is None and info["env_locked"]
    assert info["env_label"] == "Check environment"
    assert "ARGUS_EXECUTION_ENVIRONMENT must be local or capsule" in info["error"]
    card = api.environment()
    assert not card["ok"] and card["environment"] is None
    assert card["rows"] == [{"k": "configuration", "v": "not ready"}]
    assert not api.set_environment("local")["ok"]
    assert not api.run_tests(["smoke.test.yaml"])["ok"]
    assert not api.start_roam("echo ready", "cli")["ok"]
    assert api._jobs == {} and api._session["environment"] is None


def test_evidence_card_uses_verified_snapshot_after_manifest_path_is_replaced(tmp_path, monkeypatch):
    from argus import ates
    from argus.config import ArgusConfig
    from tests.conftest import FakeProvider
    from tests.test_gui_api import _wait

    monkeypatch.setattr(ArgusConfig, "make_provider", lambda self, tracker=None: FakeProvider([]))
    api = ArgusAPI(tmp_path)
    api.init_project()
    cfg = api._config()
    cfg.knowledge.enabled = False
    monkeypatch.setattr(api, "_config", lambda provider=None: cfg)
    run = _wait(api, api.run_tests(["smoke.test.yaml"])["job"]["id"])["runs"][0]
    verify = ates.verify_finalized_run
    snapshots = []

    def swap_after_verification(directory):
        result = verify(directory)
        assert isinstance(result.evidence_manifest_bytes, bytes)
        snapshots.append(json.loads(result.evidence_manifest_bytes))
        replacement = result.evidence_manifest_path.with_suffix(".replacement")
        replacement.write_text(json.dumps({"evidence": {"event_count": 999999, "sha256": "attacker"},
                                          "artifacts": ["attacker"]}))
        os.replace(replacement, result.evidence_manifest_path)
        return result

    monkeypatch.setattr(ates, "verify_finalized_run", swap_after_verification)
    card = api.evidence(run["key"])
    assert card["verified"] and card["state"] == "bound_verified"
    rows = {r["k"]: r["v"] for r in card["rows"]}
    assert rows["ordered events"] == str(snapshots[0]["evidence"]["event_count"])
    assert "999999" not in json.dumps(card) and "attacker" not in json.dumps(card)
    # The next verification still rejects the mutated canonical file.
    assert api.evidence(run["key"])["verified"] is False
