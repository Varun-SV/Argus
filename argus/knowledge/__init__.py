"""Argus Knowledge Engine — persistent graph + vector learning store.

Usage::

    from argus.knowledge import create_knowledge_store
    ks = create_knowledge_store(enabled=True, store_type="auto", ...)
    # ks is None only when knowledge is explicitly disabled
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Optional

from argus.knowledge.base import KnowledgeContext, KnowledgeStore

log = logging.getLogger(__name__)


def create_knowledge_store(
    enabled: bool = True,
    store_type: str = "auto",
    vector_backend: str = "chroma",
    vector_url: Optional[str] = None,
    persist_dir: Optional[Path] = None,
    embedding_model: str = "all-MiniLM-L6-v2",
    data_dir: Optional[Path] = None,
    interactive: bool = True,
) -> Optional[KnowledgeStore]:
    """Build and return a KnowledgeStore.

    Resolution order when store_type == "auto" (default):
      1. Docker available → start argus-qdrant container → RemoteKnowledgeStore
      2. Docker unavailable, interactive → prompt user to pick json or ml-stack
      3. Non-interactive fallback → JsonKnowledgeStore (zero deps)

    Args:
        enabled:      Master switch. False returns None immediately.
        store_type:   "auto" | "docker" | "local" (ChromaDB) | "json" (zero-dep) | "external"
        interactive:  Allow a one-time CLI prompt when Docker is absent.
        data_dir:     Parent directory for .argus data (persist_dir defaults here).
    """
    if not enabled:
        return None

    resolved_dir = persist_dir or (
        (data_dir / "knowledge") if data_dir else Path(".argus/knowledge")
    )

    if store_type == "auto":
        store_type = _resolve_auto(resolved_dir.parent, interactive)

    if store_type == "json":
        return _make_json(resolved_dir)

    if store_type == "local":
        ks = _make_local(resolved_dir, embedding_model)
        if ks is None:
            log.warning(
                "argus[knowledge] extras not installed — falling back to json store. "
                "Run: pip install 'argus-app-testing[knowledge]'"
            )
            return _make_json(resolved_dir)
        return ks

    if store_type in ("docker", "qdrant"):
        resolved_url = _start_docker_qdrant(data_dir or resolved_dir.parent)
        if resolved_url:
            ks = _make_remote(resolved_dir, resolved_url, embedding_model)
            if ks is not None:
                return ks
        log.warning("Docker Qdrant unavailable — falling back to json knowledge store.")
        return _make_json(resolved_dir)

    if store_type == "external":
        if not vector_url:
            log.warning("knowledge.type=external but no vector_url — falling back to json.")
            return _make_json(resolved_dir)
        ks = _make_remote(resolved_dir, vector_url, embedding_model)
        return ks if ks is not None else _make_json(resolved_dir)

    log.warning("Unknown knowledge store type %r — using json store.", store_type)
    return _make_json(resolved_dir)


def _resolve_auto(data_dir: Path, interactive: bool) -> str:
    """Determine which backend to use when store_type is 'auto'."""
    # Try Docker first — if available, use Qdrant container
    if _docker_available():
        log.info("Docker detected — Argus will use Qdrant container for knowledge storage.")
        return "docker"

    # No Docker — check config for a persisted choice
    choice_file = data_dir / ".knowledge_backend"
    if choice_file.exists():
        choice = choice_file.read_text().strip()
        if choice in ("json", "local"):
            return choice

    # Interactive prompt (only on first run without Docker)
    if interactive and sys.stdin.isatty() and sys.stdout.isatty():
        print(
            "\nArgus Knowledge Engine — choose a backend:\n"
            "  [1] json    — built-in, zero dependencies, works now (recommended)\n"
            "  [2] ml-stack — pip install argus[knowledge], ~1GB download, best accuracy\n",
            flush=True,
        )
        try:
            raw = input("Enter 1 or 2 [default: 1]: ").strip()
        except (EOFError, KeyboardInterrupt):
            raw = "1"
        choice = "local" if raw == "2" else "json"
        choice_file.parent.mkdir(parents=True, exist_ok=True)
        choice_file.write_text(choice)
        return choice

    return "json"


def _docker_available() -> bool:
    docker = shutil.which("docker")
    if not docker:
        return False
    try:
        import subprocess
        result = subprocess.run([docker, "info"], capture_output=True, timeout=8)
        return result.returncode == 0
    except Exception:
        return False


def _make_json(persist_dir: Path) -> KnowledgeStore:
    from argus.knowledge.json_store import JsonKnowledgeStore
    return JsonKnowledgeStore(persist_dir=persist_dir)


def _make_local(persist_dir: Path, embedding_model: str) -> Optional[KnowledgeStore]:
    try:
        from argus.knowledge.store import LocalKnowledgeStore
        return LocalKnowledgeStore(persist_dir=persist_dir, embedding_model=embedding_model)
    except Exception as exc:
        log.debug("LocalKnowledgeStore init failed: %s", exc)
        return None


def _make_remote(
    persist_dir: Path, vector_url: str, embedding_model: str
) -> Optional[KnowledgeStore]:
    try:
        from argus.knowledge.remote import RemoteKnowledgeStore
        return RemoteKnowledgeStore(
            persist_dir=persist_dir,
            vector_url=vector_url,
            embedding_model=embedding_model,
        )
    except Exception as exc:
        log.warning("RemoteKnowledgeStore init failed: %s — falling back to json.", exc)
        return None


def _start_docker_qdrant(data_dir: Path) -> Optional[str]:
    try:
        from argus.knowledge.docker_manager import DockerManager
        mgr = DockerManager(data_dir)
        if not mgr.available():
            return None
        return mgr.ensure_qdrant()
    except Exception as exc:
        log.warning("Docker Qdrant startup failed: %s", exc)
        return None


__all__ = ["KnowledgeStore", "KnowledgeContext", "create_knowledge_store"]
