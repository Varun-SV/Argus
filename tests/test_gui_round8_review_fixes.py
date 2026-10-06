"""Readiness, recorded outcomes, duration intent and resolved knowledge reporting."""
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from argus.gui import app, assistant
from argus.gui.app import ArgusAPI
from argus.knowledge import create_knowledge_store
from argus.knowledge.json_store import JsonKnowledgeStore
from argus.knowledge.remote import RemoteKnowledgeStore
from argus.knowledge.store import LocalKnowledgeStore


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_GUI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("ARGUS_EXECUTION_ENVIRONMENT", raising=False)
    monkeypatch.delenv("ARGUS_CAPSULE_PROVIDER", raising=False)
    api = ArgusAPI(tmp_path)
    api.init_project()
    cfg = api._config()
    cfg.knowledge.enabled = False
    monkeypatch.setattr(api, "_config", lambda *args, **kwargs: cfg)
    return api, cfg


class HeldThread:
    def __init__(self, target, args, **kwargs):
        self.target, self.args = target, args

    def start(self):
        pass


@pytest.mark.parametrize("source", ["project", "environment", "session"])
def test_invalid_capsule_provider_blocks_readiness_and_allocation(configured, monkeypatch, source):
    api, cfg = configured
    cfg.execution.environment = "capsule"
    if source == "project":
        cfg.execution.capsule.provider = "typo"
    elif source == "environment":
        monkeypatch.setenv("ARGUS_CAPSULE_PROVIDER", "typo")
    else:
        api._session["capsule_provider"] = "typo"
    for card in (api.app_info(), api.environment()):
        assert not card["ok"] and not card["env_locked"]
        assert "provider must be auto, hyperv or libvirt" in card["error"]
    assert not api.run_tests()["ok"]
    assert not api.start_roam("notepad.exe")["ok"]
    assert api._jobs == {}
    assert api.set_environment("local")["ok"]
    monkeypatch.setattr(app.threading, "Thread", HeldThread)
    assert api.start_roam("notepad.exe")["ok"]


def test_provider_precedence_and_correction_before_roam_allocation(configured, monkeypatch):
    api, cfg = configured
    cfg.execution.environment = "capsule"
    cfg.execution.capsule.provider = "typo"
    monkeypatch.setenv("ARGUS_CAPSULE_PROVIDER", " LibVirt ")
    assert api.app_info()["capsule_provider"] == "libvirt"
    assert api.set_environment("capsule", "hyperv")["ok"]
    assert api.app_info()["capsule_provider"] == "hyperv"
    assert api._job_environment(cfg, {"capsule_provider": " AUTO "}) == ("capsule", "auto", False, None)
    assert api._job_environment(cfg, {"capsule_provider": "typo"})[-1]
    api._session["capsule_provider"] = None
    monkeypatch.setenv("ARGUS_CAPSULE_PROVIDER", "typo")
    monkeypatch.setattr(app.threading, "Thread", HeldThread)
    started = api.start_roam("notepad.exe", overrides={"capsule_provider": "libvirt"})
    assert started["ok"] and started["job"]["capsule_provider"] == "libvirt"


def test_valid_local_job_override_and_forced_environment(configured, monkeypatch):
    api, cfg = configured
    cfg.execution.environment = "capsule"
    cfg.execution.capsule.provider = "typo"
    assert api._job_environment(cfg, {"environment": "local"})[-1] is None
    monkeypatch.setenv("ARGUS_EXECUTION_ENVIRONMENT", "capsule")
    assert api._job_environment(cfg, {"environment": "local", "capsule_provider": "auto"})[-1]
    assert not api.app_info()["ok"] and api.app_info()["env_locked"]


@pytest.mark.parametrize("status,expected", [
    ("pass", "done"), ("fail", "fail"), ("error", "error"),
    ("outcome_unknown", "unknown"), ("cancelled", "stopped"), ("unexpected", "unknown"), (None, "done"),
])
@pytest.mark.parametrize("late_stop", [True, False])
def test_roam_recorded_outcomes_survive_stop_in_card_and_result(configured, monkeypatch, status, expected, late_stop):
    api, cfg = configured
    def roam(**kwargs):
        if late_stop:
            api.stop()
        session = SimpleNamespace(findings=[], stopped_reason="recorded reason", tokens={}, ates_run_id="test-run")
        if status is not None:
            session.execution_status = status
        return session
    monkeypatch.setattr(app.threading, "Thread", HeldThread)
    monkeypatch.setattr(cfg, "make_provider", lambda *args: object())
    monkeypatch.setattr(cfg, "make_execution_environment", lambda *args: object())
    monkeypatch.setattr("argus.engine.roam.roam", roam)
    started = api.start_roam("notepad.exe")
    job = api._jobs[started["job"]["id"]]
    api._roam_worker(job, cfg)
    assert api.job_status(job["id"])["status"] == expected
    assert api._results[job["key"]]["status"] == expected
    assert job["stopped_reason"] == "recorded reason" and not job["running"]


@pytest.mark.parametrize("duration", ["0", "0.0", "-1", "-.5"])
@pytest.mark.parametrize("unit", ["seconds", "minutes", "hours"])
@pytest.mark.parametrize("leading", [True, False])
def test_invalid_free_text_duration_never_authorizes_roam(duration, unit, leading):
    modifier = f"for {duration} {unit}"
    text = f"{modifier}, roam notepad.exe" if leading else f"roam notepad.exe {modifier}"
    request = assistant.split_roam_request(text)
    assert request.problem and "greater than zero" in request.problem
    got = assistant.validate_intent({"intent": "roam", "args": {"target": "notepad.exe"}}, text, {})
    assert got["intent"] == "chat"


@pytest.mark.parametrize("modifier,minutes", [
    ("", None), (" for 30 seconds", 0.5), (" for .5 minutes", 0.5),
    (" for +2 hours", 120), (" for 240 minutes", 240),
])
def test_positive_and_unspecified_duration_remain_supported(modifier, minutes):
    got = assistant.validate_intent({"intent": "roam", "args": {"target": "notepad.exe"}},
                                    "roam notepad.exe" + modifier, {})
    assert got["intent"] == "roam" and got["args"]["minutes"] == minutes


def test_quoted_command_durations_are_literal_and_invalid_outside_duration_is_rejected():
    command = "python tool.py --minutes 0 for -1 minutes"
    text = f'roam "{command}"'
    got = assistant.validate_intent({"intent": "roam", "args": {"target": command}}, text, {})
    assert got["intent"] == "roam" and got["args"]["target"] == command
    assert got["args"]["minutes"] is None
    assert assistant.split_roam_request(text + " for 0 minutes").problem


@pytest.mark.parametrize("duration", [0, -1, float("nan"), float("inf"), -float("inf"), "bad"])
def test_direct_invalid_duration_does_not_allocate(configured, duration):
    api, _ = configured
    assert not api.start_roam("notepad.exe", minutes=duration)["ok"]
    assert api._jobs == {}


@pytest.mark.parametrize("duration", ["0", "-1", "nan", "inf"])
def test_slash_invalid_duration_remains_rejected(duration):
    with pytest.raises(assistant.IntentError):
        assistant.parse_slash(f"/roam notepad.exe --minutes {duration}", [], "")


def test_absent_duration_uses_configured_default(configured, monkeypatch):
    api, cfg = configured
    cfg.time_minutes = 7
    monkeypatch.setattr(app.threading, "Thread", HeldThread)
    assert api.start_roam("notepad.exe")["job"]["minutes"] == 7


@pytest.mark.parametrize("configured_backend", ["json", "local", "docker", "qdrant", "external"])
def test_knowledge_card_reports_real_json_and_fallback_through_wrapper(configured, monkeypatch, configured_backend):
    api, cfg = configured
    cfg.knowledge.enabled = True
    cfg.knowledge.type = configured_backend
    cfg.knowledge.vector_url = None
    if configured_backend in {"local", "docker", "qdrant"} and os.name != "nt" and not Path("/proc/self/fd").is_dir():
        pytest.skip("Default optional backend anchoring currently requires Windows handles or Linux procfs")
    graph = cfg.argus_dir / "knowledge" / "notepad-exe.graph.json"
    graph.parent.mkdir()
    graph.write_text('{"nodes": {}, "edges": []}')
    calls = []
    def unavailable(*args, **kwargs):
        calls.append(True)
        return None
    monkeypatch.setattr("argus.knowledge._start_docker_qdrant", unavailable)
    monkeypatch.setattr("argus.knowledge._make_local", lambda *args: None)
    card = api.knowledge("notepad.exe")
    assert card["ok"] and "JSON graph" in card["backend"]
    assert "no vector backend" in card["backend"] and "chroma" not in card["backend"].lower()
    assert ("fallback from" in card["backend"]) == (configured_backend != "json")
    assert len(calls) == (1 if configured_backend in {"docker", "qdrant"} else 0)


@pytest.mark.parametrize("kind", ["local", "remote"])
def test_lazy_backend_description_never_initializes_client_or_embeddings(tmp_path, monkeypatch, kind):
    store = (LocalKnowledgeStore(tmp_path) if kind == "local" else
             RemoteKnowledgeStore(tmp_path, "https://secret:sentinel@invalid.example"))
    def forbidden(*args, **kwargs):
        pytest.fail("Describing capabilities must not initialize optional resources")
    monkeypatch.setattr(store._embedder, "_load", forbidden)
    if kind == "local":
        monkeypatch.setattr(store, "_chroma", forbidden)
    else:
        monkeypatch.setattr(store, "_client", forbidden)
    first = store.backend_info()
    assert "not checked" in first["label"] and "sentinel" not in json.dumps(first)
    store._embedder._available = False
    if kind == "local":
        store._chroma_ok = False
        assert "Chroma vectors (unavailable)" in store.backend_info()["label"]
        store._chroma_ok = True
        assert "Chroma vectors (initialized)" in store.backend_info()["label"]
    else:
        store._qdrant_ok = False
        assert "Qdrant vectors (unavailable)" in store.backend_info()["label"]
        store._qdrant = object()
        assert "connectivity not verified" in store.backend_info()["label"]
    assert "embeddings (unavailable)" in store.backend_info()["label"]
    store._embedder._available = True
    assert "embeddings (loaded)" in store.backend_info()["label"]


def test_local_factory_failure_reports_actual_json(tmp_path, monkeypatch):
    monkeypatch.setattr("argus.knowledge._make_local", lambda *args: None)
    store = create_knowledge_store(store_type="local", persist_dir=tmp_path)
    assert isinstance(store, JsonKnowledgeStore) and store.backend_info()["type"] == "json"


def test_failed_qdrant_client_creation_is_reported_without_a_second_attempt(tmp_path, monkeypatch):
    calls = []
    def unavailable(**kwargs):
        calls.append(True)
        raise ImportError("not installed")
    monkeypatch.setitem(sys.modules, "qdrant_client", SimpleNamespace(QdrantClient=unavailable))
    store = RemoteKnowledgeStore(tmp_path, "http://unused.invalid")
    assert store._client() is None
    assert "Qdrant vectors (unavailable)" in store.backend_info()["label"]
    assert calls == [True]
