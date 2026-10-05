"""Reset validates before creating storage and shares job-reservation authority."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from argus.config import ArgusConfig, KnowledgeConfig
from argus.gui.app import ArgusAPI


def setup(tmp_path, monkeypatch):
    api = ArgusAPI(tmp_path)
    api.init_project()
    cfg = api._config()
    cfg.knowledge = KnowledgeConfig(type="json")
    monkeypatch.setattr(api, "_config", lambda provider=None: cfg)
    directory = tmp_path / ".argus" / "knowledge"
    directory.mkdir()
    graph = directory / "app.graph.json"
    graph.write_text(json.dumps({"nodes": {}, "edges": []}))
    return api, cfg, graph


@pytest.mark.parametrize("initialized", [False, True])
def test_invalid_reset_does_not_construct_store_or_initialize_storage(tmp_path, monkeypatch, initialized):
    api = ArgusAPI(tmp_path)
    if initialized:
        api.init_project()
    monkeypatch.setattr(ArgusConfig, "make_knowledge_store",
                        lambda self: pytest.fail("Invalid reset must not create a store"))
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    result = api.knowledge_reset("typo")
    assert not result["ok"]
    if not initialized:
        assert "/init" in result["error"]
        assert not api.app_info()["initialized"]
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")) == before


@pytest.mark.parametrize("phase", ["run startup", "roam startup", "between tests", "cached store"])
def test_reset_rejects_active_knowledge_and_reserved_jobs(tmp_path, monkeypatch, phase):
    api, cfg, graph = setup(tmp_path, monkeypatch)
    if phase == "cached store":
        store = cfg.make_knowledge_store()
        store._store._graph("app")  # Cache it as a writer, not a stats-only reader.
        api._active_ks = store
    else:
        assert api._begin_job({"id": phase, "running": True}) is None
        assert api._active_ks is None
    result = api.knowledge_reset("app")
    assert not result["ok"] and "current job" in result["error"]
    assert graph.exists()
    if phase == "cached store":
        store.close()
        api._active_ks = None
    else:
        api._jobs[phase]["running"] = False
    assert api.knowledge_reset("app")["ok"]
    assert not graph.exists()


def test_job_reservation_waits_for_reset_store_to_close(tmp_path, monkeypatch):
    api, cfg, graph = setup(tmp_path, monkeypatch)
    factory = cfg.make_knowledge_store
    entered, release, trying, closed = [threading.Event() for _ in range(4)]
    def store():
        ks = factory()
        clear, close = ks._store.clear_target, ks._store.close
        def wait_then_clear(target):
            entered.set()
            assert release.wait(5)
            clear(target)
        def mark_closed():
            close()
            closed.set()
        monkeypatch.setattr(ks._store, "clear_target", wait_then_clear)
        monkeypatch.setattr(ks._store, "close", mark_closed)
        return ks
    monkeypatch.setattr(cfg, "make_knowledge_store", store)
    def reserve():
        trying.set()
        result = api._begin_job({"id": "next", "running": True})
        assert closed.is_set()
        return result
    with ThreadPoolExecutor(2) as pool:
        resetting = pool.submit(api.knowledge_reset, "app")
        try:
            assert entered.wait(5)
            reserving = pool.submit(reserve)
            assert trying.wait(5)
            assert not reserving.done()
        finally:
            release.set()
        assert resetting.result(timeout=5)["ok"]
        assert reserving.result(timeout=5) is None
    assert not graph.exists()


def test_explicit_external_target_can_reset_without_project_setup(tmp_path, monkeypatch):
    api = ArgusAPI(tmp_path)
    cfg = api._config()
    external = tmp_path / "external"
    external.mkdir()
    graph = external / "app.graph.json"
    graph.write_text("{}")
    cfg.knowledge = KnowledgeConfig(type="json", persist_dir=str(external))
    monkeypatch.setattr(api, "_config", lambda provider=None: cfg)
    assert api.knowledge_reset("app")["ok"]
    assert not graph.exists()
    assert not (tmp_path / ".argus").exists()
