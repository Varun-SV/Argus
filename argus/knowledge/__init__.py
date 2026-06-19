"""Argus Knowledge Engine — persistent graph + vector learning store.

Usage::

    from argus.knowledge import create_knowledge_store
    ks = create_knowledge_store(cfg.knowledge, cfg.argus_dir)
    # ks is None when knowledge is disabled or all backends are unavailable
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from argus.knowledge.base import KnowledgeContext, KnowledgeStore

log = logging.getLogger(__name__)


def create_knowledge_store(
    enabled: bool = True,
    store_type: str = "local",
    vector_backend: str = "chroma",
    vector_url: Optional[str] = None,
    persist_dir: Optional[Path] = None,
    embedding_model: str = "all-MiniLM-L6-v2",
    data_dir: Optional[Path] = None,
) -> Optional[KnowledgeStore]:
    """Build a KnowledgeStore from config values, or return None if disabled.

    Args:
        enabled:        Master switch — False returns None immediately.
        store_type:     "local" (ChromaDB + NetworkX) | "docker" (Qdrant via
                        Docker-managed container) | "external" (Qdrant at a
                        user-supplied URL).
        vector_backend: "chroma" or "qdrant" (only relevant for docker/external).
        vector_url:     Qdrant URL for store_type="external".
        persist_dir:    Where to write graph JSON + ChromaDB files.
        embedding_model: Sentence-transformers model name.
        data_dir:       Parent of persist_dir and Docker data volumes.
    """
    if not enabled:
        return None

    resolved_dir = persist_dir or (
        (data_dir / "knowledge") if data_dir else Path(".argus/knowledge")
    )

    if store_type == "local":
        return _make_local(resolved_dir, embedding_model)

    if store_type == "docker":
        resolved_url = _start_docker_qdrant(data_dir or resolved_dir.parent)
        if resolved_url:
            return _make_remote(resolved_dir, resolved_url, embedding_model)
        log.warning("Docker Qdrant unavailable — falling back to local knowledge store.")
        return _make_local(resolved_dir, embedding_model)

    if store_type == "external":
        if not vector_url:
            log.warning("knowledge.type=external but no vector_url set — falling back to local.")
            return _make_local(resolved_dir, embedding_model)
        return _make_remote(resolved_dir, vector_url, embedding_model)

    log.warning("Unknown knowledge store type %r — disabled.", store_type)
    return None


def _make_local(persist_dir: Path, embedding_model: str) -> Optional[KnowledgeStore]:
    try:
        from argus.knowledge.store import LocalKnowledgeStore
        return LocalKnowledgeStore(persist_dir=persist_dir, embedding_model=embedding_model)
    except Exception as exc:
        log.warning("LocalKnowledgeStore init failed: %s", exc)
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
        log.warning("RemoteKnowledgeStore init failed: %s — falling back to local.", exc)
        return _make_local(persist_dir, embedding_model)


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
