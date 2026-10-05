"""Docker-managed backing services for the knowledge engine.

Argus can spin up Qdrant (vector search) and/or Neo4j (graph DB) in
lightweight Docker containers and reuse them across sessions.
"""

from __future__ import annotations

import shutil
import subprocess
import time
import hashlib
import json
import logging
import os
import re
import secrets
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


class DockerOwnershipError(ValueError):
    """Managed storage cannot safely be associated with a Docker resource."""


class ManagedQdrantURL(str):
    """Public URL string with private, container-bound client authority."""
    def __new__(cls, url, api_key, verify):
        endpoint = super().__new__(cls, url)
        endpoint._api_key = api_key
        endpoint.verify = verify
        return endpoint

    def client_options(self):
        return {"api_key": self._api_key}


class DockerManager:
    QDRANT_IMAGE = "qdrant/qdrant:latest"
    QDRANT_CONTAINER = "argus-qdrant"
    QDRANT_PORT = 6333

    NEO4J_IMAGE = "neo4j:latest"
    NEO4J_CONTAINER = "argus-neo4j"
    NEO4J_BOLT_PORT = 7687
    NEO4J_HTTP_PORT = 7474

    def __init__(self, data_dir: Path, qdrant_data_dir: Optional[Path] = None) -> None:
        self._data_dir = data_dir
        self._qdrant_data_dir = qdrant_data_dir

    def _qdrant_scope(self, create: bool = False):
        source = self._qdrant_data_dir or self._data_dir / "qdrant-data"
        if create:
            source.mkdir(parents=True, exist_ok=True)
        storage = source.resolve(strict=True)
        info = storage.stat()
        project = os.path.normcase(str(self._data_dir.resolve()))
        scope = hashlib.sha256(project.encode("utf-8")).hexdigest()
        labels = {
            "org.argus.qdrant.project": scope,
            "org.argus.qdrant.storage": hashlib.sha256(
                os.path.normcase(str(storage)).encode("utf-8")).hexdigest(),
            "org.argus.qdrant.directory": f"{info.st_dev}:{info.st_ino}",
            "org.argus.qdrant.schema": "1",
        }
        return f"{self.QDRANT_CONTAINER}-{scope[:32]}", labels, storage, source

    @staticmethod
    def _docker_command(docker: str, *args, timeout: int = 10, env=None):
        options = {"env": env} if env is not None else {}
        try:
            return subprocess.run([docker, *args], capture_output=True, text=True, timeout=timeout, **options)
        except (OSError, subprocess.SubprocessError):
            raise DockerOwnershipError("Docker operation could not be confirmed. Inspect Docker resources before retrying.") from None

    def _inspect_qdrant(self, docker: str, name: str):
        # A successful empty listing proves absence. Denied/failed inspection is
        # uncertainty, never permission to allocate or adopt another resource.
        found = self._docker_command(docker, "ps", "-a", "--filter", f"name=^/{name}$",
                                     "--format", "{{.ID}}")
        if found.returncode != 0:
            raise DockerOwnershipError("Could not inspect managed Qdrant. Check Docker access and retry.")
        ids = found.stdout.split()
        if not ids:
            return None
        if len(ids) != 1 or not re.fullmatch(r"[0-9a-f]{12,64}", ids[0]):
            raise DockerOwnershipError("Managed Qdrant identity is uncertain. Inspect Docker resources before retrying.")
        result = self._docker_command(docker, "inspect", "--type", "container", ids[0])
        try:
            parsed = json.loads(result.stdout)
            info = parsed[0]
            if (result.returncode != 0 or len(parsed) != 1 or not isinstance(info, dict)
                    or not re.fullmatch(r"[0-9a-f]{64}", info.get("Id", ""))
                    or not info["Id"].startswith(ids[0]) or info.get("Name") != "/" + name):
                raise ValueError
            for field in ("Config", "State", "HostConfig", "NetworkSettings"):
                if not isinstance(info.get(field), dict):
                    raise ValueError
            if not isinstance(info["State"].get("Running"), bool):
                raise ValueError
            if (not isinstance(info.get("Mounts"), list)
                    or any(not isinstance(m, dict) for m in info["Mounts"])
                    or not isinstance(info["Config"].get("Labels"), (dict, type(None)))):
                raise ValueError
            for owner, field in (("HostConfig", "PortBindings"), ("NetworkSettings", "Ports")):
                bindings = info[owner].get(field) or {}
                if not isinstance(bindings, dict) or any(
                    value is not None and (not isinstance(value, list)
                                           or any(not isinstance(entry, dict) for entry in value))
                    for value in bindings.values()
                ):
                    raise ValueError
        except (ValueError, TypeError, KeyError, IndexError):
            raise DockerOwnershipError("Managed Qdrant inspection is incomplete. Inspect Docker resources before retrying.") from None
        return info

    @staticmethod
    def _qdrant_key(info: dict) -> str:
        environment = (info.get("Config") or {}).get("Env") or []
        keys = [entry.partition("=")[2] for entry in environment
                if isinstance(entry, str) and entry.startswith("QDRANT__SERVICE__API_KEY=")]
        if len(keys) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]{43}", keys[0]):
            raise DockerOwnershipError("Managed Qdrant authentication is missing. Recreate only an operator-verified container, preserving its data.")
        return keys[0]

    @staticmethod
    def _mount_path(source: str) -> Path:
        # Docker Desktop may represent a Windows host bind in its Linux VM.
        if os.name == "nt":
            source = re.sub(r"^/(?:run/desktop/mnt/host|host_mnt)/([a-zA-Z])/(?=.)",
                            lambda m: m[1] + ":/", source)
        return Path(source)

    def _mount_matches(self, mount: dict, storage: Path, info: dict) -> bool:
        source = mount.get("Source")
        if not isinstance(source, str) or not source:
            return False
        if os.name != "nt" and re.fullmatch(r"/proc/[0-9]+/fd/[0-9]+", source) and info["State"]["Running"]:
            # Inspect reports the original descriptor spelling, not the live
            # mounted object. Attest that object even after the creator exits or
            # its descriptor is recycled. Never substitute an ownership label.
            result = self._docker_command(shutil.which("docker"), "exec", info["Id"],
                                          "stat", "-Lc", "%d:%i", "/qdrant/storage")
            expected = storage.stat()
            return result.returncode == 0 and result.stdout.strip() == f"{expected.st_dev}:{expected.st_ino}"
        try:
            # Do not trust a stale or recycled /proc/PID/fd/FD spelling. Both its
            # live directory identity and the immutable creation labels must match.
            return self._mount_path(source).samefile(storage)
        except (OSError, ValueError):
            return False

    @staticmethod
    def _descriptor_bound(info: dict) -> bool:
        """True when the storage bind source is a creator-process /proc/<pid>/fd spelling."""
        return os.name != "nt" and any(
            m.get("Destination") == "/qdrant/storage" and isinstance(m.get("Source"), str)
            and re.fullmatch(r"/proc/[0-9]+/fd/[0-9]+", m["Source"])
            for m in info.get("Mounts") or [])

    def _verify_qdrant(self, info: dict, labels: dict, storage: Path, attest_mount: bool = True) -> None:
        config = info.get("Config") or {}
        mounts = info.get("Mounts") or []
        bindings = (info.get("HostConfig") or {}).get("PortBindings") or {}
        expected = bindings.get("6333/tcp")
        writable = [m for m in mounts if m.get("Destination") == "/qdrant/storage"]
        conflicts = [m for m in mounts if m.get("Destination", "") != "/qdrant/storage"
                     and (m.get("Destination", "").startswith("/qdrant/storage/")
                          or m.get("Destination") in ("/", "/qdrant"))]
        owned = config.get("Labels") or {}
        if (any(owned.get(k) != v for k, v in labels.items())
                or config.get("Image") != self.QDRANT_IMAGE
                or len(writable) != 1 or conflicts
                or writable[0].get("Type") != "bind" or writable[0].get("RW") is not True
                or (attest_mount and not self._mount_matches(writable[0], storage, info))
                or (info.get("HostConfig") or {}).get("NetworkMode") not in ("default", "bridge")
                or set(bindings) != {"6333/tcp"} or not isinstance(expected, list)
                or len(expected) != 1 or expected[0].get("HostIp") != "127.0.0.1"):
            raise DockerOwnershipError(
                "Qdrant storage ownership cannot be verified. Leave the container and data intact; "
                "inspect its storage mount, then stop/remove only an operator-verified stale container and retry. "
                "Use knowledge.type=json while resolving this, or configure an approved external service.")

    @staticmethod
    def _qdrant_url(info: dict) -> str:
        ports = (info.get("NetworkSettings") or {}).get("Ports") or {}
        binding = ports.get("6333/tcp")
        if (not (info.get("State") or {}).get("Running")
                or any(value for key, value in ports.items() if key != "6333/tcp")
                or not isinstance(binding, list) or len(binding) != 1
                or binding[0].get("HostIp") != "127.0.0.1"):
            raise DockerOwnershipError("Qdrant has no verified localhost port. Inspect its Docker configuration and retry.")
        port = binding[0].get("HostPort", "")
        if not isinstance(port, str) or not port.isascii() or not port.isdecimal() or not 0 < int(port) < 65536:
            raise DockerOwnershipError("Qdrant has no verified localhost port. Inspect its Docker configuration and retry.")
        return f"http://127.0.0.1:{int(port)}"

    def _check_legacy_writer(self, docker: str, storage: Path) -> None:
        old = self._inspect_qdrant(docker, self.QDRANT_CONTAINER)
        if old is None or not (old.get("State") or {}).get("Running"):
            return
        for mount in old.get("Mounts") or []:
            if mount.get("Destination") == "/qdrant/storage":
                try:
                    source = self._mount_path(mount["Source"])
                    same = source.samefile(storage)
                except (OSError, ValueError, KeyError, TypeError):
                    same = True  # Cannot exclude a competing writer.
                if same:
                    raise DockerOwnershipError(
                        "Legacy argus-qdrant may still use this project's storage. Keep its data; "
                        "inspect and stop that container yourself before starting project-owned Qdrant. "
                        "Argus will not adopt or stop it automatically.")

    def available(self) -> bool:
        """Return True if Docker is installed and the daemon is running."""
        docker = shutil.which("docker")
        if not docker:
            return False
        try:
            result = subprocess.run(
                [docker, "info"], capture_output=True, timeout=10
            )
            return result.returncode == 0
        except Exception:
            return False

    def _container_running(self, name: str) -> bool:
        docker = shutil.which("docker")
        if not docker:
            return False
        try:
            result = subprocess.run(
                [docker, "ps", "--filter", f"name=^/{name}$", "--format", "{{.Names}}"],
                capture_output=True, text=True, timeout=10,
            )
            return name in result.stdout
        except Exception:
            return False

    def _container_exists(self, name: str) -> bool:
        docker = shutil.which("docker")
        if not docker:
            return False
        try:
            result = subprocess.run(
                [docker, "ps", "-a", "--filter", f"name=^/{name}$", "--format", "{{.Names}}"],
                capture_output=True, text=True, timeout=10,
            )
            return name in result.stdout
        except Exception:
            return False

    def _wait_http(self, url: str, timeout: int = 30, api_key: Optional[str] = None) -> bool:
        try:
            import requests
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    headers = {"api-key": api_key} if api_key else {}
                    if requests.get(url, timeout=2, allow_redirects=False, headers=headers).status_code == 200:
                        return True
                except Exception:
                    pass
                time.sleep(1)
            return False
        except ImportError:
            return False

    def ensure_qdrant(self) -> Optional[str]:
        """Start/reuse this project's verified Qdrant; return its inspected URL."""
        docker = shutil.which("docker")
        if not docker:
            return None
        name, labels, storage, source = self._qdrant_scope(create=True)
        self._check_legacy_writer(docker, storage)
        info = self._inspect_qdrant(docker, name)
        if (info is not None and not (info.get("State") or {}).get("Running")
                and self._descriptor_bound(info)):
            # The creating process's /proc/<pid>/fd bind source is stale once it exits and
            # can't be attested, and `docker start` would re-resolve it. The labels bind the
            # container to this exact storage directory and the data lives there, so replace
            # the stopped container with one bound to the current pinned storage.
            self._verify_qdrant(info, labels, storage, attest_mount=False)
            self._qdrant_key(info)
            if self._docker_command(docker, "rm", info["Id"], timeout=30).returncode != 0:
                return None
            info = None
        if info is not None:
            self._verify_qdrant(info, labels, storage)
            self._qdrant_key(info)
            if not (info.get("State") or {}).get("Running"):
                result = self._docker_command(docker, "start", info["Id"], timeout=30)
                if result.returncode != 0:
                    return None
        else:
            args = ["run", "-d", "--name", name, "-p", "127.0.0.1::6333",
                    "--env", "QDRANT__SERVICE__API_KEY",
                    "-v", f"{source}:/qdrant/storage"]
            for key, value in labels.items():
                args.extend(["--label", f"{key}={value}"])
            environment = dict(os.environ, QDRANT__SERVICE__API_KEY=secrets.token_urlsafe(32))
            result = self._docker_command(docker, *args, self.QDRANT_IMAGE, timeout=120, env=environment)
            if result.returncode != 0:
                return None
        verified = self._inspect_qdrant(docker, name)
        if verified is None or (info is not None and info["Id"] != verified["Id"]):
            raise DockerOwnershipError("Qdrant identity changed during startup. Inspect Docker resources and retry.")
        self._verify_qdrant(verified, labels, storage)
        url = self._qdrant_url(verified)
        key = self._qdrant_key(verified)
        if not self._wait_http(url + "/collections", api_key=key):
            return None
        final = self._inspect_qdrant(docker, name)
        if final is None or final["Id"] != verified["Id"]:
            raise DockerOwnershipError("Qdrant identity changed during readiness. Inspect Docker resources and retry.")
        self._verify_qdrant(final, labels, storage)
        if self._qdrant_url(final) != url or self._qdrant_key(final) != key:
            raise DockerOwnershipError("Qdrant port changed during readiness. Inspect Docker resources and retry.")
        def verify():
            current = self._inspect_qdrant(docker, name)
            if current is None or current["Id"] != final["Id"]:
                raise DockerOwnershipError("Managed Qdrant changed. Reopen the knowledge store after checking Docker resources.")
            self._verify_qdrant(current, labels, storage)
            if self._qdrant_url(current) != url or self._qdrant_key(current) != key:
                raise DockerOwnershipError("Managed Qdrant session changed. Reopen the knowledge store after checking Docker resources.")
        return ManagedQdrantURL(url, key, verify)

    def ensure_neo4j(self, password: str = "argus-neo4j") -> Optional[str]:
        """Start argus-neo4j container if not running; return bolt URI."""
        docker = shutil.which("docker")
        if not docker:
            return None
        if not self._container_running(self.NEO4J_CONTAINER):
            data_path = self._data_dir / "neo4j-data"
            data_path.mkdir(parents=True, exist_ok=True)
            if self._container_exists(self.NEO4J_CONTAINER):
                subprocess.run(
                    [docker, "start", self.NEO4J_CONTAINER],
                    capture_output=True, timeout=30,
                )
            else:
                subprocess.run(
                    [
                        docker, "run", "-d",
                        "--name", self.NEO4J_CONTAINER,
                        "-p", f"{self.NEO4J_HTTP_PORT}:{self.NEO4J_HTTP_PORT}",
                        "-p", f"{self.NEO4J_BOLT_PORT}:{self.NEO4J_BOLT_PORT}",
                        "-v", f"{data_path}:/data",
                        "-e", f"NEO4J_AUTH=neo4j/{password}",
                        self.NEO4J_IMAGE,
                    ],
                    capture_output=True, timeout=120,
                )
        http_url = f"http://localhost:{self.NEO4J_HTTP_PORT}"
        bolt_url = f"bolt://localhost:{self.NEO4J_BOLT_PORT}"
        return bolt_url if self._wait_http(http_url, timeout=60) else None

    def stop(self, service: str = "all") -> bool:
        """Stop one or both backing containers."""
        docker = shutil.which("docker")
        if not docker:
            return False
        containers = []
        ownership_confirmed = True
        if service in ("all", "qdrant"):
            try:
                name, labels, storage, _ = self._qdrant_scope()
                info = self._inspect_qdrant(docker, name)
                if info is not None:
                    self._verify_qdrant(info, labels, storage)
                    containers.append(info["Id"])
            except FileNotFoundError:
                pass
            except (OSError, ValueError, subprocess.SubprocessError):
                log.warning("Qdrant stop could not verify project ownership. Inspect Docker resources before retrying.")
                ownership_confirmed = False
        if service in ("all", "neo4j"):
            containers.append(self.NEO4J_CONTAINER)
        for name in containers:
            try:
                result = subprocess.run([docker, "stop", name], capture_output=True, timeout=30)
                if result.returncode != 0 and name != self.NEO4J_CONTAINER:
                    return False
            except Exception:
                if name != self.NEO4J_CONTAINER:
                    return False
        return ownership_confirmed

    def status(self) -> dict:
        """Return running state of both containers.

        ``qdrant`` is None when its state can't be verified (Docker inspection failed or
        the container failed the ownership checks): it may still be running, so callers
        must not report it as stopped.
        """
        qdrant: Optional[bool] = False
        try:
            docker = shutil.which("docker")
            name, labels, storage, _ = self._qdrant_scope()
            info = self._inspect_qdrant(docker, name) if docker else None
            # A stopped container writes nothing, so "stopped" needs no ownership attestation
            # (and a stopped descriptor-bound mount can't be attested anyway).
            if info is not None and (info.get("State") or {}).get("Running"):
                self._verify_qdrant(info, labels, storage)
                qdrant = True
        except FileNotFoundError:
            pass
        except (OSError, ValueError, subprocess.SubprocessError):
            log.warning("Qdrant status could not verify project ownership. Inspect Docker resources before retrying.")
            qdrant = None
        return {
            "qdrant": qdrant,
            "neo4j": self._container_running(self.NEO4J_CONTAINER),
        }
