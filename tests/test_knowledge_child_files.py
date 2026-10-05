"""Default graph/file operations reject aliases before touching outside bytes."""
import json
import os
import stat
from pathlib import Path

import pytest

from argus.adapters.base import Observation
from argus.ates.store import AtesStoreError
from argus.knowledge.storage import KnowledgeFileError, _DirectoryFile
from tests.test_knowledge_storage_boundary import configuration
from argus.gui.app import ArgusAPI


def link_file(path, victim, kind):
    if kind == "hard":
        os.link(victim, path)
    else:
        path.symlink_to(victim)


@pytest.mark.parametrize("backend", ["json", "local", "external"])
@pytest.mark.parametrize("kind", ["hard", pytest.param("symbolic", marks=pytest.mark.skipif(
    os.name == "nt", reason="File symlinks require Windows developer mode or elevation"))])
def test_default_graph_alias_cannot_read_or_overwrite_outside(tmp_path, backend, kind):
    if backend != "json":
        pytest.importorskip("networkx")
    cfg = configuration(tmp_path)
    cfg.knowledge.type = backend
    cfg.knowledge.vector_url = "http://localhost:6333"
    if backend == "local" and os.name != "nt" and not Path("/proc/self/fd").is_dir():
        pytest.skip("Default Chroma storage requires native directory anchoring")
    store = cfg.make_knowledge_store()
    outside = tmp_path / "outside"
    outside.write_text("external host configuration sentinel")
    graph = tmp_path / ".argus" / "knowledge" / "app.graph.json"
    link_file(graph, outside, kind)
    with pytest.raises(KnowledgeFileError):
        store.get_stats("app")
    store.close()
    assert outside.read_text() == "external host configuration sentinel"


@pytest.mark.parametrize("backend", ["json", "local", "external"])
def test_cached_graph_replacement_is_not_silently_saved(tmp_path, backend):
    if backend != "json":
        pytest.importorskip("networkx")
    cfg = configuration(tmp_path)
    cfg.knowledge.type = backend
    cfg.knowledge.vector_url = "http://localhost:6333"
    if backend == "local" and os.name != "nt" and not Path("/proc/self/fd").is_dir():
        pytest.skip("Default Chroma storage requires native directory anchoring")
    store = cfg.make_knowledge_store()
    # Cache through the backend's graph method, without initializing vector clients.
    store._store._graph("app")
    store.finalize_session("session", "app")
    graph = tmp_path / ".argus" / "knowledge" / "app.graph.json"
    outside = tmp_path / "outside"
    outside.write_text("external sentinel")
    graph.unlink()
    os.link(outside, graph)
    with pytest.raises(KnowledgeFileError):
        store.finalize_session("session", "app")
    with pytest.raises(KnowledgeFileError):
        store.close()
    assert outside.read_text() == "external sentinel"


@pytest.mark.parametrize("suffix", ["states.ndjson", "bugs.ndjson"])
def test_ndjson_hardlink_cannot_append_or_read_outside(tmp_path, suffix):
    store = configuration(tmp_path).make_knowledge_store()
    outside = tmp_path / "outside"
    outside.write_text("external sentinel")
    path = tmp_path / ".argus" / "knowledge" / f"app.{suffix}"
    os.link(outside, path)
    with pytest.raises(KnowledgeFileError):
        if suffix == "states.ndjson":
            store.record_state(Observation(window_title="App", elements=[]), "app", "session", 0)
        else:
            store.record_finding("bug", "high", "state", [], "app", "session")
    with pytest.raises(KnowledgeFileError):
        store.get_stats("app")
    store.close()
    assert outside.read_text() == "external sentinel"


def test_backend_choice_alias_is_rejected_without_prompt_or_external_write(tmp_path, monkeypatch):
    import argus.knowledge as knowledge
    root = tmp_path / ".argus"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("external sentinel")
    os.link(outside, root / ".knowledge_backend")
    monkeypatch.setattr(knowledge, "_docker_available", lambda: False)
    monkeypatch.setattr("builtins.input", lambda *args: pytest.fail("Unsafe choice must not prompt"))
    cfg = configuration(tmp_path)
    cfg.knowledge.type = "auto"
    with pytest.raises(KnowledgeFileError):
        cfg.make_knowledge_store()
    assert outside.read_text() == "external sentinel"


def test_legitimate_backend_choice_and_json_session_remain_usable(tmp_path, monkeypatch):
    import argus.knowledge as knowledge
    monkeypatch.setattr(knowledge, "_docker_available", lambda: False)
    monkeypatch.setattr(knowledge.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(knowledge.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *args: "1")
    cfg = configuration(tmp_path)
    cfg.knowledge.type = "auto"
    store = cfg.make_knowledge_store()
    assert (tmp_path / ".argus" / ".knowledge_backend").read_text() == "json"
    store.record_state(Observation(window_title="App", elements=[]), "app", "session", 0)
    store.record_finding("bug", "high", "state", [], "app", "session")
    store.finalize_session("session", "app")
    assert store.get_stats("app")["app"] == {
        "states": 1, "transitions": 0, "bugs": 1, "bug_nodes": 0, "sessions": 1}
    store.close()
    store = cfg.make_knowledge_store()
    assert store.get_stats("app")["app"]["states"] == 1
    store.clear_target("app")
    store.close()
    assert not list((tmp_path / ".argus" / "knowledge").glob("app.*"))


@pytest.mark.skipif(os.name != "nt", reason="Windows readonly file semantics")
def test_readonly_windows_graph_remains_inspectable(tmp_path):
    store = configuration(tmp_path).make_knowledge_store()
    graph = tmp_path / ".argus" / "knowledge" / "app.graph.json"
    original = json.dumps({"nodes": {"state": {"visit_count": 1}}, "edges": []})
    graph.write_text(original)
    graph.chmod(stat.S_IREAD)
    try:
        assert store.get_stats("app")["app"]["states"] == 1
        store.close()
        assert graph.read_text() == original
    finally:
        graph.chmod(stat.S_IWRITE)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file namespace replacement")
def test_replacement_between_exists_and_read_is_rejected(tmp_path, monkeypatch):
    store = configuration(tmp_path).make_knowledge_store()
    graph = tmp_path / ".argus" / "knowledge" / "app.graph.json"
    graph.write_text('{"nodes": {}, "edges": []}')
    outside = tmp_path / "outside"
    outside.write_text("external sentinel")
    read = _DirectoryFile.read_text
    def replace(file, encoding=None):
        graph.unlink()
        graph.symlink_to(outside)
        return read(file, encoding)
    monkeypatch.setattr(_DirectoryFile, "read_text", replace)
    with pytest.raises(KnowledgeFileError):
        store.get_stats("app")
    store.close()
    assert outside.read_text() == "external sentinel"


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO and namespace checks")
def test_special_file_and_opened_file_replacement_fail_before_truncation(tmp_path, monkeypatch):
    store = configuration(tmp_path).make_knowledge_store()
    graph = tmp_path / ".argus" / "knowledge" / "app.graph.json"
    os.mkfifo(graph)
    with pytest.raises(KnowledgeFileError):
        (store._store._dir / graph.name).write_text("unsafe")
    graph.unlink()
    graph.write_text("owned sentinel")
    outside = tmp_path / "outside"
    outside.write_text("external sentinel")
    opened = os.open
    def replace(name, flags, *args, **kwargs):
        fd = opened(name, flags, *args, **kwargs)
        if name == graph.name:
            graph.rename(graph.with_name("original"))
            graph.symlink_to(outside)
        return fd
    monkeypatch.setattr(os, "open", replace)
    with pytest.raises(KnowledgeFileError):
        (store._store._dir / graph.name).write_text("unsafe")
    store.close()
    assert outside.read_text() == "external sentinel"
    assert graph.with_name("original").read_text() == "owned sentinel"


def test_explicit_external_graph_keeps_operator_trusted_behavior(tmp_path):
    external = tmp_path / "operator"
    external.mkdir()
    victim = tmp_path / "trusted-linked-file"
    victim.write_text('{"nodes": {}, "edges": []}')
    os.link(victim, external / "app.graph.json")
    store = configuration(tmp_path, persist_dir=str(external)).make_knowledge_store()
    store.get_stats("app")
    store.close()
    assert json.loads(victim.read_text()) == {"nodes": {}, "edges": []}


def test_default_export_cannot_copy_outside_bytes_through_hardlink(tmp_path, monkeypatch):
    api = ArgusAPI(tmp_path)
    api.init_project()
    cfg = configuration(tmp_path)
    monkeypatch.setattr(api, "_config", lambda provider=None: cfg)
    directory = tmp_path / ".argus" / "knowledge"
    directory.mkdir()
    victim = tmp_path / "outside"
    victim.write_text("private external sentinel")
    os.link(victim, directory / "app.graph.json")
    result = api.knowledge_export("app")
    assert not result["ok"]
    assert not (tmp_path / ".argus" / "exports" / "app.graph.json").exists()
    assert victim.read_text() == "private external sentinel"


def test_secure_graph_read_preserves_exact_bytes_and_never_initializes_storage(tmp_path):
    from argus.knowledge.storage import read_project_knowledge_file
    with pytest.raises((OSError, ValueError, AtesStoreError)):
        read_project_knowledge_file(tmp_path, "app.graph.json")
    assert not (tmp_path / ".argus").exists()
    directory = tmp_path / ".argus" / "knowledge"
    directory.mkdir(parents=True)
    raw = b'{"nodes": {}, "edges": []}\r\n'
    (directory / "app.graph.json").write_bytes(raw)
    assert read_project_knowledge_file(tmp_path, "app.graph.json") == raw
