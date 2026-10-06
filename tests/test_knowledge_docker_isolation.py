"""Docker protocol tests: project selection and lifecycle never cross storage."""
import copy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from argus.knowledge import docker_manager, create_knowledge_store
from argus.knowledge.docker_manager import DockerManager, DockerOwnershipError
from argus.knowledge.remote import RemoteKnowledgeStore
from argus.adapters.base import Observation


class DockerDaemon:
    """Stateful Docker protocol substitute; never invokes real Docker or HTTP."""
    def __init__(self):
        self.containers = {}
        self.commands = []
        self.fail = set()
        self.denied = False
        self.native_run = docker_manager.subprocess.run

    def run(self, command, **kwargs):
        if command[0] != "docker":
            return self.native_run(command, **kwargs)
        self.commands.append(command)
        args = command[1:]
        if args[0] in self.fail or self.denied:
            return SimpleNamespace(returncode=1, stdout="", stderr="denied")
        output = ""
        if args[0] == "ps":
            name = args[args.index("--filter") + 1][len("name=^/"):-1]
            info = self.containers.get(name)
            if info:
                output = info["Id"][:12]
        elif args[0] == "inspect":
            info = next(c for c in self.containers.values() if c["Id"].startswith(args[-1]))
            output = json.dumps([info])
        elif args[0] == "run":
            name = args[args.index("--name") + 1]
            source = args[args.index("-v") + 1].removesuffix(":/qdrant/storage")
            labels = dict(args[i + 1].split("=", 1) for i, a in enumerate(args) if a == "--label")
            ident = hashlib.sha256(name.encode()).hexdigest()
            info = {
                "Id": ident, "Name": "/" + name,
                "Config": {"Image": DockerManager.QDRANT_IMAGE, "Labels": labels,
                           "Env": ["QDRANT__SERVICE__API_KEY=" + kwargs["env"]["QDRANT__SERVICE__API_KEY"]]},
                "State": {"Running": True},
                "HostConfig": {"NetworkMode": "default", "PortBindings": {
                    "6333/tcp": [{"HostIp": "127.0.0.1", "HostPort": ""}]}},
                "NetworkSettings": {"Ports": {
                    "6333/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(40000 + len(self.containers))}],
                    "6334/tcp": None}},
                "Mounts": [{"Source": source, "Destination": "/qdrant/storage", "Type": "bind", "RW": True}],
            }
            filesystem = Path(source).stat()
            info["_mounted_identity"] = f"{filesystem.st_dev}:{filesystem.st_ino}"
            self.containers[name] = info
            output = ident
        elif args[0] in {"start", "stop"}:
            info = next(c for c in self.containers.values() if c["Id"] == args[1])
            info["State"]["Running"] = args[0] == "start"
            output = args[1]
        elif args[0] == "exec":
            info = next(c for c in self.containers.values() if c["Id"] == args[1])
            output = info["_mounted_identity"]
        elif args[0] == "rm":
            name = next(n for n, c in self.containers.items() if c["Id"] == args[1])
            del self.containers[name]
            output = args[1]
        return SimpleNamespace(returncode=0, stdout=output, stderr="")


@pytest.fixture
def daemon(monkeypatch):
    daemon = DockerDaemon()
    monkeypatch.setattr(docker_manager.shutil, "which", lambda _: "docker")
    monkeypatch.setattr(docker_manager.subprocess, "run", daemon.run)
    monkeypatch.setattr(DockerManager, "available", lambda _: True)
    monkeypatch.setattr(DockerManager, "_wait_http", lambda *a, **k: True)
    return daemon


def manager(tmp_path, name="a"):
    return DockerManager(tmp_path / name / ".argus")


def test_owned_create_reopen_restart_and_project_scoped_status_stop(tmp_path, daemon):
    a, b = manager(tmp_path), manager(tmp_path, "b")
    url_a, url_b = a.ensure_qdrant(), b.ensure_qdrant()
    assert url_a != url_b and "localhost:6333" not in (url_a, url_b)
    assert manager(tmp_path).ensure_qdrant() == url_a
    assert a.status()["qdrant"] and b.status()["qdrant"]
    assert b.stop("qdrant")
    assert not b.status()["qdrant"] and a.status()["qdrant"]
    assert b.ensure_qdrant() == url_b
    assert len([c for c in daemon.commands if c[1] == "run"]) == 2
    assert a.stop("qdrant")
    assert not a.status()["qdrant"] and b.status()["qdrant"]


@pytest.mark.parametrize("running", [False, True])
def test_same_name_with_foreign_storage_cannot_start_connect_or_stop(tmp_path, daemon, running):
    a = manager(tmp_path)
    a.ensure_qdrant()
    info = next(iter(daemon.containers.values()))
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    info["Mounts"][0]["Source"] = str(foreign)
    info["State"]["Running"] = running
    before = len(daemon.commands)
    with pytest.raises(DockerOwnershipError, match="ownership"):
        a.ensure_qdrant()
    assert not a.stop("qdrant") and not a.status()["qdrant"]
    assert not any(c[1] in {"start", "stop", "run"} for c in daemon.commands[before:])


@pytest.mark.parametrize("failure", ["run", "start"])
def test_failed_startup_never_accepts_healthy_unrelated_port(tmp_path, daemon, monkeypatch, failure):
    a = manager(tmp_path)
    if failure == "start":
        a.ensure_qdrant()
        a.stop("qdrant")
    daemon.fail.add(failure)
    healthy_checks = []
    monkeypatch.setattr(a, "_wait_http", lambda url, **k: healthy_checks.append(url) or True)
    assert a.ensure_qdrant() is None
    assert healthy_checks == []


@pytest.mark.parametrize("bad", ["labels", "mount", "network", "public-port", "extra-binding", "identity", "malformed"])
def test_malformed_or_unowned_inspect_is_not_readiness(tmp_path, daemon, bad):
    a = manager(tmp_path)
    a.ensure_qdrant()
    info = next(iter(daemon.containers.values()))
    if bad == "labels":
        info["Config"]["Labels"] = {}
    elif bad == "mount":
        info["Mounts"].append({"Source": "/other", "Destination": "/qdrant", "RW": True})
    elif bad == "network":
        info["HostConfig"]["NetworkMode"] = "host"
    elif bad == "public-port":
        info["NetworkSettings"]["Ports"]["6333/tcp"][0]["HostIp"] = "0.0.0.0"
    elif bad == "extra-binding":
        info["NetworkSettings"]["Ports"]["6334/tcp"] = [{"HostIp": "0.0.0.0", "HostPort": "6334"}]
    elif bad == "identity":
        info["Name"] = "/another-project"
    else:
        info["Config"] = ["unexpected"]
    with pytest.raises(DockerOwnershipError):
        a.ensure_qdrant()


def test_denied_listing_is_not_absence(tmp_path, daemon):
    daemon.denied = True
    with pytest.raises(DockerOwnershipError):
        manager(tmp_path).ensure_qdrant()
    assert not any(c[1] == "run" for c in daemon.commands)


def test_qdrant_ownership_failure_still_attempts_separate_neo4j_stop(tmp_path, daemon):
    a = manager(tmp_path)
    a.ensure_qdrant()
    next(iter(daemon.containers.values()))["Config"]["Labels"] = {}
    assert not a.stop()
    assert any(c[1:] == ["stop", "argus-neo4j"] for c in daemon.commands)


def test_cli_without_project_storage_keeps_separate_neo4j_shutdown(tmp_path, daemon, monkeypatch):
    from click.testing import CliRunner
    from argus.cli import main
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["knowledge", "docker", "down"])
    assert result.exit_code == 0
    # Only a read-only lookup for a still-running project Qdrant, then the Neo4j stop.
    assert [c[1] for c in daemon.commands] == ["ps", "stop"]
    assert daemon.commands[-1] == ["docker", "stop", "argus-neo4j"]
    assert not (tmp_path / ".argus").exists()


@pytest.mark.parametrize("command", ["up", "down", "status"])
def test_cli_rejects_redirected_project_storage_before_docker_actions(tmp_path, daemon, monkeypatch, command):
    from click.testing import CliRunner
    from argus.cli import main
    from tests.test_knowledge_storage_boundary import redirect
    owner = tmp_path / "owner"
    owner.mkdir()
    b = DockerManager(owner / ".argus")
    b.ensure_qdrant()
    attacker = tmp_path / "other"
    attacker.mkdir()
    redirect(attacker / ".argus", owner / ".argus")
    monkeypatch.chdir(attacker)
    before = len(daemon.commands)
    result = CliRunner().invoke(main, ["knowledge", "docker", command])
    assert result.exit_code != 0 and "Could not verify project Docker storage" in result.output
    assert daemon.commands[before:] == []
    assert b.status()["qdrant"]


@pytest.mark.skipif(os.name != "nt" and not Path("/proc/self/fd").is_dir(),
                    reason="Secure managed Docker storage requires Windows handles or Linux descriptors")
def test_cli_and_default_store_select_same_project_resource(tmp_path, daemon, monkeypatch):
    from click.testing import CliRunner
    from argus.cli import main
    from argus.config import ArgusConfig, KnowledgeConfig, ProviderConfig
    from argus.knowledge.storage import project_docker_manager
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["knowledge", "docker", "up"])
    assert result.exit_code == 0, result.output
    cfg = ArgusConfig(tmp_path, ProviderConfig("ollama", "fake"), knowledge=KnowledgeConfig(type="docker"))
    store = cfg.make_knowledge_store()
    assert isinstance(store._store, RemoteKnowledgeStore)
    with project_docker_manager(tmp_path) as cli:
        assert cli.ensure_qdrant() == store._store._vector_url
        assert cli.status()["qdrant"]
        assert cli.stop("qdrant")
    with pytest.raises(DockerOwnershipError):
        store._store._client()
    store.close()


@pytest.mark.skipif(os.name != "nt" and not Path("/proc/self/fd").is_dir(),
                    reason="Secure managed Docker storage requires Windows handles or Linux descriptors")
def test_explicit_graph_storage_keeps_managed_docker_project_boundary(tmp_path, daemon):
    from argus.config import ArgusConfig, KnowledgeConfig, ProviderConfig
    from tests.test_knowledge_storage_boundary import redirect
    from argus.ates.store import AtesStoreError
    external = tmp_path / "operator-graphs"
    configs = [ArgusConfig(tmp_path / name, ProviderConfig("ollama", "fake"),
                          knowledge=KnowledgeConfig(type="docker", persist_dir=str(external / name)))
               for name in ("a", "b")]
    for cfg in configs:
        cfg.project_dir.mkdir()
    stores = [cfg.make_knowledge_store() for cfg in configs]
    assert stores[0]._store._vector_url != stores[1]._store._vector_url
    assert all(store._store._dir == external / name for store, name in zip(stores, ("a", "b")))
    for store in stores:
        store.close()
    third = tmp_path / "other"
    third.mkdir()
    redirect(third / ".argus", configs[1].argus_dir)
    cfg = ArgusConfig(third, ProviderConfig("ollama", "fake"),
                      knowledge=KnowledgeConfig(type="docker", persist_dir=str(external / "other")))
    before = len(daemon.commands)
    with pytest.raises((AtesStoreError, OSError, ValueError)):
        cfg.make_knowledge_store()
    assert daemon.commands[before:] == []


@pytest.mark.parametrize("race", [False, True])
@pytest.mark.parametrize("operation", ["retrieve", "reset"])
def test_existing_store_cannot_follow_a_reallocated_port_even_during_request(tmp_path, daemon, monkeypatch, race, operation):
    import sys
    urls = [manager(tmp_path, name).ensure_qdrant() for name in ("a", "b")]
    containers = list(daemon.containers.values())
    assert urls[0].client_options() != urls[1].client_options()
    b_data = {"app_states": [SimpleNamespace(score=1.0, payload={"state_id": "B"})]}
    class AuthenticatedClient:
        def __init__(self, url, api_key):
            self.url, self.key = url, api_key
        def authenticate(self):
            bound = next(c for c in containers if c["State"]["Running"] and
                         DockerManager._qdrant_url(c) == self.url)
            if DockerManager._qdrant_key(bound) != self.key:
                raise PermissionError("Wrong container credential")
        def search(self, collection_name, **kwargs):
            self.authenticate()
            return b_data.get(collection_name, [])
        def delete_collection(self, name):
            self.authenticate()
            b_data.pop(name, None)
    monkeypatch.setitem(sys.modules, "qdrant_client", SimpleNamespace(QdrantClient=AuthenticatedClient))
    store = RemoteKnowledgeStore(tmp_path / "graphs", urls[0])
    store._embedder = SimpleNamespace(embed=lambda _: [1.0])
    assert store._client().key == urls[0].client_options()["api_key"]
    graph = tmp_path / "graphs" / "app.graph.json"
    graph.write_text('{"directed":true,"multigraph":false,"graph":{"keep":"local-state"},"nodes":[],"edges":[],"links":[]}')
    def reuse():
        containers[0]["State"]["Running"] = False
        containers[1]["NetworkSettings"]["Ports"]["6333/tcp"] = copy.deepcopy(
            containers[0]["NetworkSettings"]["Ports"]["6333/tcp"])
    def operation_call():
        return store.retrieve(Observation("app"), "app") if operation == "retrieve" else store.clear_target("app")
    if race:
        original = store._client
        def client_before_race():
            client = original()
            reuse()
            return client
        monkeypatch.setattr(store, "_client", client_before_race)
        result = operation_call()
        if operation == "retrieve":
            assert result.similar_states == []
    else:
        reuse()
        with pytest.raises(DockerOwnershipError):
            operation_call()
        assert graph.exists()
    assert b_data["app_states"][0].payload["state_id"] == "B"
    # Private keys travel through inherited environment, never command arguments.
    for command in daemon.commands:
        assert all(urls[0].client_options()["api_key"] not in arg for arg in command)


def test_legacy_running_writer_requires_operator_action_and_is_never_adopted(tmp_path, daemon):
    a = manager(tmp_path)
    a.ensure_qdrant()
    scoped = next(iter(daemon.containers))
    old = daemon.containers.pop(scoped)
    old["Name"] = "/argus-qdrant"
    old["Id"] = hashlib.sha256(b"legacy").hexdigest()
    daemon.containers["argus-qdrant"] = old
    before = len(daemon.commands)
    with pytest.raises(DockerOwnershipError, match="Legacy"):
        a.ensure_qdrant()
    assert not any(c[1] in {"start", "stop", "run"} for c in daemon.commands[before:])
    old["State"]["Running"] = False
    assert a.ensure_qdrant()
    assert not old["State"]["Running"]


@pytest.mark.parametrize("alias", ["auto", "docker", "qdrant"])
def test_same_target_retrieval_and_reset_are_isolated_through_factory(tmp_path, daemon, monkeypatch, alias):
    import argus.knowledge as knowledge
    monkeypatch.setattr(knowledge, "_docker_available", lambda: True)
    stores = [create_knowledge_store(store_type=alias, data_dir=tmp_path / name, interactive=False)
              for name in ("a", "b")]
    assert all(isinstance(store, RemoteKnowledgeStore) for store in stores)
    assert stores[0]._vector_url != stores[1]._vector_url
    databases = {store._vector_url: {"app_states": [SimpleNamespace(score=1.0, payload={"state_id": str(i)})],
                                    "app_bugs": []} for i, store in enumerate(stores)}
    class Client:
        def __init__(self, url):
            self.database = databases[url]
        def search(self, collection_name, **kwargs):
            return self.database.get(collection_name, [])
        def delete_collection(self, name):
            self.database.pop(name, None)
    for store in stores:
        store._qdrant = Client(store._vector_url)
        store._embedder = SimpleNamespace(embed=lambda _: [1.0])
    assert stores[0].retrieve(Observation("app"), "app").similar_states[0].state_id == "0"
    assert stores[1].retrieve(Observation("app"), "app").similar_states[0].state_id == "1"
    stores[0].clear_target("app")
    assert stores[0].retrieve(Observation("app"), "app").similar_states == []
    assert stores[1].retrieve(Observation("app"), "app").similar_states[0].state_id == "1"
    for store in stores:
        store.close()


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="Linux directory descriptor lifecycle")
def test_descriptor_reuse_cannot_prove_an_old_mount_owned(tmp_path, daemon):
    directory = tmp_path / "a" / ".argus" / "qdrant-data"
    directory.mkdir(parents=True)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        a = DockerManager(directory.parent, Path(f"/proc/{os.getpid()}/fd/{fd}"))
        assert a.ensure_qdrant()
        old_info = copy.deepcopy(next(iter(daemon.containers.values())))
    finally:
        os.close(fd)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    foreign_fd = os.open(foreign, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert old_info["Mounts"][0]["Source"].endswith(str(fd))
        # The old spelling is no proof: the live mounted object is authoritative.
        assert DockerManager(directory.parent).ensure_qdrant()
        next(iter(daemon.containers.values()))["_mounted_identity"] = "0:0"
        with pytest.raises(DockerOwnershipError):
            DockerManager(directory.parent).ensure_qdrant()
    finally:
        os.close(foreign_fd)


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="Linux mounted-directory attestation")
def test_running_mount_reopens_after_creator_exit_but_unavailable_attestation_fails_closed(tmp_path, daemon):
    a = manager(tmp_path)
    url = a.ensure_qdrant()
    info = next(iter(daemon.containers.values()))
    info["Mounts"][0]["Source"] = "/proc/999999999/fd/17"
    assert manager(tmp_path).ensure_qdrant() == url
    assert a.status()["qdrant"]
    daemon.fail.add("exec")
    with pytest.raises(DockerOwnershipError):
        a.ensure_qdrant()
    assert not a.stop("qdrant")


@pytest.mark.parametrize("cause", ["foreign_storage", "inspect_fails"])
def test_unverifiable_qdrant_status_is_unknown_not_stopped(tmp_path, daemon, cause):
    a = manager(tmp_path)
    a.ensure_qdrant()
    info = next(iter(daemon.containers.values()))
    if cause == "foreign_storage":
        foreign = tmp_path / "foreign"
        foreign.mkdir()
        info["Mounts"][0]["Source"] = str(foreign)
    else:
        daemon.fail.add("inspect")
    assert info["State"]["Running"]
    assert a.status()["qdrant"] is None


def test_cli_prints_unknown_for_unverifiable_qdrant(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from click.testing import CliRunner
    from argus import cli
    from argus.knowledge import storage

    class Unverifiable:
        def status(self):
            return {"qdrant": None, "neo4j": False}

    @contextmanager
    def project_manager(_project):
        yield Unverifiable()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(storage, "project_docker_manager", project_manager)
    out = CliRunner().invoke(cli.main, ["knowledge", "docker", "status"])
    assert out.exit_code == 0, out.output
    assert "qdrant: unknown" in out.output and "qdrant: stopped" not in out.output


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="Linux descriptor-bound storage")
def test_stopped_descriptor_bound_container_is_recreated_on_current_storage(tmp_path, daemon):
    a = manager(tmp_path)
    a.ensure_qdrant()
    a.stop("qdrant")
    old = next(iter(daemon.containers.values()))
    old["Mounts"][0]["Source"] = "/proc/999999999/fd/17"  # the creating process has exited
    assert not old["State"]["Running"] and a.status()["qdrant"] is False

    url = manager(tmp_path).ensure_qdrant()
    assert url
    assert ["docker", "rm", old["Id"]] in daemon.commands
    assert not any(c[1] == "start" for c in daemon.commands)
    (new,) = daemon.containers.values()
    assert Path(new["Mounts"][0]["Source"]).samefile(tmp_path / "a" / ".argus" / "qdrant-data")
    assert a.status()["qdrant"] is True


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="Linux descriptor-bound storage")
def test_stopped_descriptor_bound_container_for_other_storage_is_never_removed(tmp_path, daemon):
    a = manager(tmp_path)
    a.ensure_qdrant()
    a.stop("qdrant")
    old = next(iter(daemon.containers.values()))
    old["Mounts"][0]["Source"] = "/proc/999999999/fd/17"
    old["Config"]["Labels"]["org.argus.qdrant.directory"] = "0:0"
    before = len(daemon.commands)
    with pytest.raises(DockerOwnershipError):
        manager(tmp_path).ensure_qdrant()
    assert not any(c[1] in {"rm", "start", "run"} for c in daemon.commands[before:])


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="Linux descriptor-bound storage")
def test_down_on_an_already_stopped_descriptor_container_succeeds(tmp_path, daemon):
    a = manager(tmp_path)
    a.ensure_qdrant()
    assert a.stop("qdrant")
    old = next(iter(daemon.containers.values()))
    old["Mounts"][0]["Source"] = "/proc/999999999/fd/17"  # the creating process has exited
    before = len(daemon.commands)
    assert manager(tmp_path).stop("qdrant")
    assert not any(c[1] in {"stop", "rm", "run"} for c in daemon.commands[before:])


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="Linux descriptor-bound storage")
def test_down_on_a_stopped_descriptor_container_for_other_storage_fails_closed(tmp_path, daemon):
    a = manager(tmp_path)
    a.ensure_qdrant()
    assert a.stop("qdrant")
    old = next(iter(daemon.containers.values()))
    old["Mounts"][0]["Source"] = "/proc/999999999/fd/17"
    old["Config"]["Labels"]["org.argus.qdrant.directory"] = "0:0"
    assert not manager(tmp_path).stop("qdrant")


def _remove_storage(tmp_path):
    storage = tmp_path / "a" / ".argus" / "qdrant-data"
    storage.rename(storage.with_name("qdrant-data-moved"))


def test_missing_storage_with_running_container_is_unverified_not_stopped(tmp_path, daemon):
    a = manager(tmp_path)
    a.ensure_qdrant()
    _remove_storage(tmp_path)
    before = len(daemon.commands)
    assert manager(tmp_path).status()["qdrant"] is None
    assert not manager(tmp_path).stop("qdrant")
    assert not any(c[1] == "stop" for c in daemon.commands[before:])  # unverified: left alone
    assert next(iter(daemon.containers.values()))["State"]["Running"]


def test_missing_storage_with_failed_lookup_is_unverified(tmp_path, daemon):
    manager(tmp_path).ensure_qdrant()
    _remove_storage(tmp_path)
    daemon.fail.add("ps")
    assert manager(tmp_path).status()["qdrant"] is None
    assert not manager(tmp_path).stop("qdrant")


@pytest.mark.parametrize("state", ["stopped", "absent"])
def test_missing_storage_without_a_running_container_is_stopped(tmp_path, daemon, state):
    if state == "stopped":
        a = manager(tmp_path)
        a.ensure_qdrant()
        assert a.stop("qdrant")
        _remove_storage(tmp_path)
    assert manager(tmp_path).status()["qdrant"] is False
    assert manager(tmp_path).stop("qdrant")


@pytest.mark.parametrize("command", ["down", "status"])
def test_cli_with_the_whole_argus_directory_gone_still_finds_running_qdrant(tmp_path, daemon, monkeypatch, command):
    from click.testing import CliRunner
    from argus.cli import main
    project = tmp_path / "a"
    manager(tmp_path).ensure_qdrant()
    (project / ".argus").rename(project / "argus-moved")
    monkeypatch.chdir(project)
    before = len(daemon.commands)
    result = CliRunner().invoke(main, ["knowledge", "docker", command])
    assert not any(c[1] == "stop" and c[2] != "argus-neo4j" for c in daemon.commands[before:])
    assert next(iter(daemon.containers.values()))["State"]["Running"]
    if command == "down":
        assert result.exit_code == 1 and "Could not confirm" in result.output
    else:
        assert "qdrant: unknown" in result.output
