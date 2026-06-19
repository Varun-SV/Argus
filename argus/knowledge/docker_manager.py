"""Docker-managed backing services for the knowledge engine.

Argus can spin up Qdrant (vector search) and/or Neo4j (graph DB) in
lightweight Docker containers and reuse them across sessions.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional


class DockerManager:
    QDRANT_IMAGE = "qdrant/qdrant:latest"
    QDRANT_CONTAINER = "argus-qdrant"
    QDRANT_PORT = 6333

    NEO4J_IMAGE = "neo4j:latest"
    NEO4J_CONTAINER = "argus-neo4j"
    NEO4J_BOLT_PORT = 7687
    NEO4J_HTTP_PORT = 7474

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir

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

    def _wait_http(self, url: str, timeout: int = 30) -> bool:
        try:
            import requests
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    if requests.get(url, timeout=2).status_code < 500:
                        return True
                except Exception:
                    pass
                time.sleep(1)
            return False
        except ImportError:
            time.sleep(5)
            return True

    def ensure_qdrant(self) -> Optional[str]:
        """Start argus-qdrant container if not running; return its URL."""
        docker = shutil.which("docker")
        if not docker:
            return None
        if not self._container_running(self.QDRANT_CONTAINER):
            data_path = self._data_dir / "qdrant-data"
            data_path.mkdir(parents=True, exist_ok=True)
            if self._container_exists(self.QDRANT_CONTAINER):
                subprocess.run(
                    [docker, "start", self.QDRANT_CONTAINER],
                    capture_output=True, timeout=30,
                )
            else:
                subprocess.run(
                    [
                        docker, "run", "-d",
                        "--name", self.QDRANT_CONTAINER,
                        "-p", f"{self.QDRANT_PORT}:{self.QDRANT_PORT}",
                        "-v", f"{data_path}:/qdrant/storage",
                        self.QDRANT_IMAGE,
                    ],
                    capture_output=True, timeout=120,
                )
        url = f"http://localhost:{self.QDRANT_PORT}"
        return url if self._wait_http(url) else None

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

    def stop(self, service: str = "all") -> None:
        """Stop one or both backing containers."""
        docker = shutil.which("docker")
        if not docker:
            return
        containers = []
        if service in ("all", "qdrant"):
            containers.append(self.QDRANT_CONTAINER)
        if service in ("all", "neo4j"):
            containers.append(self.NEO4J_CONTAINER)
        for name in containers:
            try:
                subprocess.run([docker, "stop", name], capture_output=True, timeout=30)
            except Exception:
                pass

    def status(self) -> dict:
        """Return running state of both containers."""
        return {
            "qdrant": self._container_running(self.QDRANT_CONTAINER),
            "neo4j": self._container_running(self.NEO4J_CONTAINER),
        }
