"""Default project storage rejects aliases without restricting explicit stores."""
import os
import subprocess
import sys
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from argus.config import ArgusConfig, KnowledgeConfig, ProviderConfig
from argus.gui.app import ArgusAPI
from argus.ates.store import AtesStoreError


STABLE_DIRECTORY_PATHS = os.name == "nt" or Path("/proc/self/fd").is_dir()


def redirect(link, target):
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       check=True, capture_output=True)
        assert os.lstat(link).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
        assert not link.is_symlink()
    else:
        link.symlink_to(target, target_is_directory=True)


def configuration(project, **options):
    return ArgusConfig(project_dir=project, provider=ProviderConfig(type="ollama", model="test"),
                       knowledge=KnowledgeConfig(type="json", **options))


@pytest.mark.parametrize("component", [".argus", "knowledge"])
def test_default_reset_rejects_redirected_directory_and_preserves_external_files(tmp_path, monkeypatch, component):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    graph = outside / "notepad-exe.graph.json"
    graph.write_text("external sentinel")
    link = project / ".argus"
    if component == "knowledge":
        link.mkdir()
        link = link / "knowledge"
    redirect(link, outside)
    api = ArgusAPI(project)
    monkeypatch.setattr(api, "_config", lambda: configuration(project))
    assert not api.knowledge_reset("notepad.exe")["ok"]
    assert graph.read_text() == "external sentinel"
    # Config is also the CLI/run/roam selection boundary.
    with pytest.raises((ValueError, OSError, AtesStoreError)):
        configuration(project).make_knowledge_store()


def test_explicit_external_store_and_missing_default_directory_are_supported(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    store = configuration(project).make_knowledge_store()
    store.clear_target("notepad.exe")
    store.close()
    assert (project / ".argus" / "knowledge").is_dir()
    external = tmp_path / "external"
    external.mkdir()
    paths = [external / ("notepad-exe." + suffix) for suffix in ("graph.json", "states.ndjson", "bugs.ndjson")]
    for path in paths:
        path.write_text("sentinel")
    store = configuration(project, persist_dir=str(external)).make_knowledge_store()
    store.clear_target("notepad.exe")
    store.close()
    assert all(not path.exists() for path in paths)


def test_disabled_store_does_not_create_or_probe_project_storage(tmp_path):
    project = tmp_path / "does-not-exist"
    assert configuration(project, enabled=False).make_knowledge_store() is None
    assert not project.exists()


@pytest.mark.parametrize("component", [".argus", "knowledge"])
def test_directory_replacement_after_construction_is_denied_or_detected(tmp_path, component):
    store = configuration(tmp_path).make_knowledge_store()
    path = tmp_path / ".argus"
    if component == "knowledge":
        path = path / "knowledge"
    moved = path.with_name(path.name + "-original")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "notepad-exe.graph.json"
    sentinel.write_text("sentinel")
    if os.name == "nt":
        with pytest.raises(OSError):
            path.rename(moved)
        store.clear_target("notepad.exe")
        store.close()
    else:
        path.rename(moved)
        redirect(path, outside)
        with pytest.raises((ValueError, OSError, AtesStoreError)):
            store.clear_target("notepad.exe")
        with pytest.raises((ValueError, OSError, AtesStoreError)):
            store.close()
    assert sentinel.read_text() == "sentinel"


@pytest.mark.skipif(os.name == "nt", reason="Windows directory pins prevent replacement")
def test_replacement_during_clear_cannot_redirect_unlink(tmp_path, monkeypatch):
    store = configuration(tmp_path).make_knowledge_store()
    directory = tmp_path / ".argus" / "knowledge"
    outside = tmp_path / "outside"
    outside.mkdir()
    name = "notepad-exe.graph.json"
    (directory / name).write_text("owned")
    sentinel = outside / name
    sentinel.write_text("external")
    original = store._store.clear_target
    def replace_then_clear(target):
        directory.rename(directory.with_name("original"))
        redirect(directory, outside)
        original(target)
    monkeypatch.setattr(store._store, "clear_target", replace_then_clear)
    with pytest.raises((ValueError, OSError, AtesStoreError)):
        store.clear_target("notepad.exe")
    with pytest.raises((ValueError, OSError, AtesStoreError)):
        store.close()
    assert sentinel.read_text() == "external"
    assert not (directory.with_name("original") / name).exists()


def test_local_vector_directory_cannot_be_redirected(tmp_path):
    (tmp_path / ".argus" / "knowledge").mkdir(parents=True)
    outside = tmp_path / "external-vectors"
    outside.mkdir()
    redirect(tmp_path / ".argus" / "knowledge" / "chroma", outside)
    cfg = ArgusConfig(project_dir=tmp_path, provider=ProviderConfig(type="ollama", model="test"),
                      knowledge=KnowledgeConfig(type="local"))
    with pytest.raises((ValueError, OSError, AtesStoreError)):
        cfg.make_knowledge_store()


@pytest.mark.parametrize("backend", [
    pytest.param("local", marks=pytest.mark.skipif(
        not STABLE_DIRECTORY_PATHS, reason="Default local backend requires stable directory paths")),
    "external",
])
def test_vector_backend_reset_preserves_collection_deletion(tmp_path, monkeypatch, backend):
    from argus.knowledge.store import LocalKnowledgeStore
    from argus.knowledge.remote import RemoteKnowledgeStore
    deleted = []
    class Client:
        def delete_collection(self, name):
            deleted.append(name)
    cls = LocalKnowledgeStore if backend == "local" else RemoteKnowledgeStore
    monkeypatch.setattr(cls, "_chroma" if backend == "local" else "_client", lambda self: Client())
    cfg = ArgusConfig(project_dir=tmp_path, provider=ProviderConfig(type="ollama", model="test"), knowledge=KnowledgeConfig(
        type=backend, vector_url="http://localhost:6333"))
    store = cfg.make_knowledge_store()
    graph = tmp_path / ".argus" / "knowledge" / "notepad-exe.graph.json"
    graph.write_text("{}")
    store.clear_target("notepad.exe")
    store.close()
    assert not graph.exists()
    assert deleted == ["notepad-exe_states", "notepad-exe_bugs"]


def test_docker_storage_alias_is_rejected_before_startup(tmp_path, monkeypatch):
    from argus.knowledge import docker_manager
    root = tmp_path / ".argus"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    redirect(root / "qdrant-data", outside)
    monkeypatch.setattr(docker_manager.DockerManager, "available",
                        lambda self: pytest.fail("Redirected Docker storage reached startup"))
    cfg = configuration(tmp_path)
    cfg.knowledge.type = "docker"
    with pytest.raises((ValueError, OSError, AtesStoreError)):
        cfg.make_knowledge_store()


@pytest.mark.skipif(not STABLE_DIRECTORY_PATHS, reason="Default Docker storage fails with guidance on this host")
def test_docker_mount_uses_pinned_host_directory_not_daemon_self_descriptor(tmp_path, monkeypatch):
    from argus.knowledge import docker_manager
    manager = docker_manager.DockerManager
    captured = []
    monkeypatch.setattr(manager, "available", lambda self: True)
    monkeypatch.setattr(manager, "_container_running", lambda self, name: False)
    monkeypatch.setattr(manager, "_container_exists", lambda self, name: False)
    monkeypatch.setattr(manager, "_wait_http", lambda *args: True)
    monkeypatch.setattr(docker_manager.shutil, "which", lambda name: "docker")
    def run(args, **kwargs):
        captured.append(args)
        return SimpleNamespace(returncode=0, stdout="")
    monkeypatch.setattr(docker_manager.subprocess, "run", run)
    cfg = configuration(tmp_path)
    cfg.knowledge.type = "docker"
    store = cfg.make_knowledge_store()
    command = captured[0]
    mount = command[command.index("-v") + 1].removesuffix(":/qdrant/storage")
    from pathlib import Path
    source = Path(mount)
    assert "/proc/self/" not in mount
    assert source.samefile(tmp_path / ".argus" / "qdrant-data")
    if os.name != "nt" and Path("/proc/self/fd").is_dir():
        assert mount.startswith(f"/proc/{os.getpid()}/fd/")
    store.close()


@pytest.mark.skipif(not STABLE_DIRECTORY_PATHS, reason="Default Chroma storage fails with guidance on this host")
def test_chroma_cache_cannot_follow_reused_descriptors_into_another_project(tmp_path, monkeypatch):
    from argus.knowledge.storage import _VECTOR_PINS
    before = set(_VECTOR_PINS)
    systems = {}
    class Client:
        def __init__(self, path):
            self.path = path
            self.collections = {"notepad-exe_states", "notepad-exe_bugs"}
        def delete_collection(self, name):
            self.collections.discard(name)
    def persistent(path):
        return systems.setdefault(path, Client(path))
    monkeypatch.setitem(sys.modules, "chromadb", SimpleNamespace(PersistentClient=persistent))
    stores = []
    try:
        projects = [tmp_path / "a", tmp_path / "b"]
        for project in projects:
            project.mkdir()
        def local(project):
            cfg = configuration(project)
            cfg.knowledge.type = "local"
            store = cfg.make_knowledge_store()
            stores.append(store)
            return store
        a = local(projects[0])
        a_client = a._store._chroma()
        a.close()
        b = local(projects[1])
        b_client = b._store._chroma()
        assert a_client is not b_client and a_client.path != b_client.path
        b.clear_target("notepad.exe")
        b.close()
        assert len(a_client.collections) == 2 and not b_client.collections
        a_reopened = local(projects[0])
        assert a_reopened._store._chroma() is a_client
        a_reopened.close()
    finally:
        for store in stores:
            store.close()
        # Our simulated cache is disposed here; release only its test-owned pins.
        for identity in set(_VECTOR_PINS) - before:
            _VECTOR_PINS.pop(identity).pin.close()


@pytest.mark.skipif(os.name == "nt", reason="Descriptor-relative file operations are POSIX")
def test_json_default_works_without_descriptor_filesystem_paths(tmp_path, monkeypatch):
    from argus.knowledge.storage import _ProjectDirectories, _DirectoryFiles
    from argus.adapters.base import Observation
    monkeypatch.setattr(_ProjectDirectories, "path", lambda self, pin: _DirectoryFiles(pin))
    cfg = configuration(tmp_path)
    store = cfg.make_knowledge_store()
    obs = Observation(window_title="Portable JSON", elements=[])
    store.record_state(obs, "notepad.exe", "session", 0)
    store.finalize_session("session", "notepad.exe")
    assert store.get_stats("notepad.exe")["notepad.exe"]["states"] == 1
    assert "notepad-exe.graph" in store.get_stats()
    store.clear_target("notepad.exe")
    store.close()
    assert not list((tmp_path / ".argus" / "knowledge").glob("notepad-exe.*"))


@pytest.mark.skipif(os.name == "nt", reason="Descriptor-relative file operations are POSIX")
@pytest.mark.parametrize("backend", ["local", "docker"])
def test_unanchored_default_vector_backend_fails_with_configuration_guidance(tmp_path, monkeypatch, backend):
    from argus.knowledge.storage import _ProjectDirectories, _DirectoryFiles
    monkeypatch.setattr(_ProjectDirectories, "path", lambda self, pin: _DirectoryFiles(pin))
    cfg = configuration(tmp_path)
    cfg.knowledge.type = backend
    with pytest.raises(ValueError, match="knowledge.type to json.*operator-approved"):
        cfg.make_knowledge_store()


@pytest.mark.skipif(os.name == "nt", reason="Descriptor-relative file operations are POSIX")
def test_portable_clear_stays_anchored_during_directory_replacement(tmp_path, monkeypatch):
    from argus.knowledge.storage import _ProjectDirectories, _DirectoryFiles
    monkeypatch.setattr(_ProjectDirectories, "path", lambda self, pin: _DirectoryFiles(pin))
    test_replacement_during_clear_cannot_redirect_unlink(tmp_path, monkeypatch)


@pytest.mark.skipif(os.name == "nt", reason="Descriptor-relative file operations are POSIX")
def test_portable_default_configuration_can_run_a_cli_test(tmp_path, monkeypatch):
    from pathlib import Path
    from argus.config import init_project, load_config
    from tests.conftest import FakeProvider
    from tests.test_gui_api import _wait
    from argus.knowledge.storage import _ProjectDirectories, _DirectoryFiles
    is_dir = Path.is_dir
    monkeypatch.setattr(Path, "is_dir", lambda path: False if path == Path("/proc/self/fd") else is_dir(path))
    monkeypatch.setattr(_ProjectDirectories, "path", lambda self, pin: _DirectoryFiles(pin))
    monkeypatch.setattr(ArgusConfig, "make_provider", lambda self, tracker=None: FakeProvider([]))
    init_project(tmp_path)
    assert load_config(tmp_path).knowledge.type == "json"
    api = ArgusAPI(tmp_path)
    api.init_project()
    job = _wait(api, api.run_tests(["smoke.test.yaml"])["job"]["id"])
    assert job["runs"][0]["status"] == "pass"
