"""Requested ordering, history migration and read-only knowledge regressions."""
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from argus.adapters.base import Observation
from argus.config import ArgusConfig, KnowledgeConfig
from argus.gui.app import ArgusAPI, _conversation_path
from argus.gui.assistant import validate_intent
from argus.gui.state import project_identity
from argus.knowledge.fingerprint import target_key
from argus.knowledge.json_store import JsonKnowledgeStore
from argus.knowledge.remote import RemoteKnowledgeStore
from argus.knowledge.store import LocalKnowledgeStore
from tests.conftest import FakeProvider
from tests.test_gui_api import CLI_SPEC, _wait


@pytest.mark.parametrize("text,expected", [
    ("run b then a", ["b", "a"]),
    ("run b and a", ["b", "a"]),
    ("run b then a then b", ["b", "a", "b"]),
    ("run b followed by a followed by b", ["b", "a", "b"]),
    ("run b and b", ["b"]),
    ('run "long title" then a', ["long", "a"]),
])
def test_requested_order_overrides_sidebar_and_model_order(text, expected):
    context = {"tests": [{"file": f"{name}.test.yaml", "name": title}
                          for name, title in [("a", "a"), ("b", "b"),
                                              ("short", "title"), ("long", "long title")]]}
    result = validate_intent({"intent": "run", "args": {"tests": "all"}}, text, context)
    assert result["args"]["tests"] == [name + ".test.yaml" for name in expected]


def test_requested_repeats_reach_real_runner_in_order(tmp_path, monkeypatch):
    api = ArgusAPI(tmp_path)
    api.init_project()
    cfg = api._config()
    cfg.knowledge.enabled = False
    monkeypatch.setattr(api, "_config", lambda provider=None: cfg)
    monkeypatch.setattr(ArgusConfig, "make_provider", lambda self, tracker=None: FakeProvider([]))
    for name in ("a", "b"):
        spec = yaml.safe_load(CLI_SPEC)
        spec["name"] = name
        (tmp_path / ".argus" / f"{name}.test.yaml").write_text(yaml.safe_dump(spec))
    scope = validate_intent({"intent": "run"}, "run b then a then b",
                            {"tests": api.list_tests()})["args"]["tests"]
    job = _wait(api, api.run_tests(scope)["job"]["id"])
    assert [run["file"] for run in job["runs"]] == scope
    assert [run["status"] for run in job["runs"]] == ["pass"] * 3


@pytest.mark.parametrize("target", ["notepad.exe", "http://localhost:3000", "python app.py"])
def test_knowledge_key_is_inspectable_but_not_a_launch_target(tmp_path, monkeypatch, target):
    api = ArgusAPI(tmp_path)
    api.init_project()
    cfg = api._config()
    cfg.knowledge = KnowledgeConfig(type="json")
    monkeypatch.setattr(api, "_config", lambda provider=None: cfg)
    directory = tmp_path / ".argus" / "knowledge"
    directory.mkdir()
    key = target_key(target)
    (directory / f"{key}.graph.json").write_text('{"nodes": {}, "edges": []}')
    assert api.knowledge()["launch_target"] is None
    assert api.knowledge(key)["launch_target"] is None
    assert api.knowledge(key.upper())["launch_target"] is None
    assert api.knowledge(key[:3])["launch_target"] is None
    assert api.knowledge(target)["launch_target"] == target
    api._last_target = target
    assert api.knowledge(key)["launch_target"] == target
    assert api.knowledge()["launch_target"] == target


def test_history_key_uses_lease_identity_and_native_aliases(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_GUI_STATE_DIR", str(tmp_path / "state"))
    cfg = SimpleNamespace(project_dir=tmp_path)
    path = _conversation_path(cfg)
    assert path.parent.name == hashlib.sha256(project_identity(tmp_path).encode()).hexdigest()[:24]
    assert _conversation_path(SimpleNamespace(project_dir=tmp_path / ".")) == path
    if os.name == "nt":
        assert _conversation_path(SimpleNamespace(project_dir=Path(str(tmp_path).swapcase()))) == path


def test_normalized_history_key_does_not_depend_on_resolved_spelling(tmp_path, monkeypatch):
    # Model a case-insensitive filesystem that preserves the supplied spelling.
    # Native Windows resolve may instead return the on-disk spelling for both.
    from argus.gui import app
    monkeypatch.setenv("ARGUS_GUI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(app, "project_identity", lambda project: str(project).casefold())
    first = _conversation_path(SimpleNamespace(project_dir=tmp_path))
    second = _conversation_path(SimpleNamespace(project_dir=str(tmp_path).swapcase()))
    assert first == second


def test_known_legacy_user_history_migrates_and_canonical_wins(tmp_path, monkeypatch):
    from argus.gui import app
    monkeypatch.setenv("ARGUS_GUI_STATE_DIR", str(tmp_path / "state"))
    # On Linux normcase is unchanged; model a changed identity to exercise migration.
    monkeypatch.setattr(app, "project_identity", lambda project: "normalized:" + str(project))
    cfg = SimpleNamespace(project_dir=tmp_path)
    old = _conversation_path(cfg, legacy=True)
    canonical = _conversation_path(cfg)
    chats = [{"id": "legacy", "msgs": [{"text": "Keep this conversation"}]}]
    old.write_text(json.dumps(chats))
    api = ArgusAPI(tmp_path)
    assert api.load_conversations() == chats
    assert json.loads(canonical.read_text()) == chats
    assert json.loads(old.read_text()) == chats
    latest = [{"id": "canonical", "msgs": []}]
    assert api.save_conversations(latest)["ok"]
    assert api.load_conversations() == latest
    assert json.loads(old.read_text()) == chats


@pytest.fixture(params=["json", "local", "remote"])
def knowledge_store(request, tmp_path, monkeypatch):
    directory = tmp_path / "knowledge"
    if request.param == "json":
        store = JsonKnowledgeStore(directory)
    elif request.param == "local":
        pytest.importorskip("networkx")
        store = LocalKnowledgeStore(directory)
        monkeypatch.setattr(store, "_collection", lambda *args: None)
    else:
        pytest.importorskip("networkx")
        store = RemoteKnowledgeStore(directory, "http://unused.invalid")
        monkeypatch.setattr(store._embedder, "embed", lambda *args: None)
        monkeypatch.setattr(store, "_client", lambda: pytest.fail("Stats must not contact Qdrant"))
    yield store, directory
    store.close()


def test_polling_empty_target_does_not_create_graph_on_finalize_or_close(knowledge_store):
    store, directory = knowledge_store
    for _ in range(3):
        assert store.get_stats("typo.exe")["typo.exe"]["states"] == 0
        assert store.get_stats() == {}
    store.finalize_session("failed-launch", "typo.exe")
    store.close()
    assert not list(directory.glob("*.graph.json"))


def test_read_only_stats_preserve_bytes_and_recorded_live_counts(knowledge_store):
    store, directory = knowledge_store
    first = store.record_state(Observation(window_title="First"), "notepad.exe", "session", 0)
    second = store.record_state(Observation(window_title="Second"), "notepad.exe", "session", 1)
    store.record_transition(first, {"action": "click", "element_id": 1}, second, "notepad.exe", "session")
    assert store.get_stats("notepad.exe")["notepad.exe"]["states"] == 2
    assert store.get_stats("notepad.exe")["notepad.exe"]["transitions"] == 1
    store.finalize_session("session", "notepad.exe")
    store.close()
    store._graphs.clear()  # Fresh reader after writing, with no active graph cache.
    path = directory / "notepad-exe.graph.json"
    path.write_text(path.read_text() + "\n\n")
    before = path.read_bytes()
    assert store.get_stats()["notepad-exe.graph"]["states"] == 2
    assert store.get_stats("notepad.exe")["notepad.exe"]["states"] == 2
    store.close()
    assert path.read_bytes() == before
    assert sorted(p.name for p in directory.glob("*.graph.json")) == [path.name]


def test_failed_roam_live_counters_do_not_leave_learned_target(tmp_path, monkeypatch):
    api = ArgusAPI(tmp_path)
    api.init_project()
    cfg = api._config()
    cfg.knowledge = KnowledgeConfig(type="json")
    monkeypatch.setattr(api, "_config", lambda provider=None: cfg)
    monkeypatch.setattr(ArgusConfig, "make_provider", lambda self, tracker=None: FakeProvider([]))
    # Inject the adapter's launch failure before any state is recorded. The real
    # worker still polls its final counters and closes the active store.
    from argus.adapters.cli_adapter import CLIAdapter
    def fail_launch(self, target):
        raise OSError("Executable unavailable")
    monkeypatch.setattr(CLIAdapter, "launch", fail_launch)
    result = api.start_roam("argus-missing-executable-round-five", "cli", minutes=0.1)
    job = _wait(api, result["job"]["id"])
    assert job["status"] == "error"
    assert not list((tmp_path / ".argus" / "knowledge").glob("*.graph.json"))
    assert not api.knowledge()["ok"]
