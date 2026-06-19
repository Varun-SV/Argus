"""LocalKnowledgeStore: ChromaDB (vector) + NetworkX (graph), no external services."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from argus.adapters.base import Observation
from argus.knowledge.base import KnowledgeContext, KnowledgeStore, PastBug, SimilarState
from argus.knowledge.embeddings import EmbeddingGenerator
from argus.knowledge.fingerprint import fingerprint, semantic_description, summarize_action, target_key


class LocalKnowledgeStore(KnowledgeStore):
    """ChromaDB + NetworkX local backend.

    Falls back gracefully when libraries are not installed:
      - No chromadb → vector retrieval disabled, graph still works.
      - No networkx  → graph disabled, vector retrieval still works.
      - Neither      → store records nothing but callers never crash.
    """

    def __init__(
        self,
        persist_dir: Path,
        embedding_model: str = "all-MiniLM-L6-v2",
    ) -> None:
        self._dir = persist_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._embedder = EmbeddingGenerator(embedding_model)
        self._chroma_client = None
        self._chroma_ok: Optional[bool] = None
        self._graphs: Dict[str, Any] = {}
        self._nx_ok: Optional[bool] = None

    # ── backend accessors ───────────────────────────────────────────────────

    def _chroma(self):
        if self._chroma_ok is None:
            try:
                import chromadb
                self._chroma_client = chromadb.PersistentClient(
                    path=str(self._dir / "chroma")
                )
                self._chroma_ok = True
            except Exception:
                self._chroma_ok = False
        return self._chroma_client if self._chroma_ok else None

    def _collection(self, target: str, suffix: str):
        client = self._chroma()
        if client is None:
            return None
        key = target_key(target)
        try:
            return client.get_or_create_collection(f"{key}_{suffix}")
        except Exception:
            return None

    def _graph(self, target: str):
        key = target_key(target)
        if key not in self._graphs:
            if self._nx_ok is None:
                try:
                    import networkx  # noqa: F401
                    self._nx_ok = True
                except ImportError:
                    self._nx_ok = False
            if not self._nx_ok:
                self._graphs[key] = None
                return None
            import networkx as nx
            G = nx.DiGraph()
            path = self._dir / f"{key}.graph.json"
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    G = nx.node_link_graph(data)
                except Exception:
                    pass
            self._graphs[key] = G
        return self._graphs.get(key)

    def _save_graph(self, target: str) -> None:
        key = target_key(target)
        G = self._graphs.get(key)
        if G is None:
            return
        try:
            import networkx as nx
            path = self._dir / f"{key}.graph.json"
            path.write_text(json.dumps(nx.node_link_data(G), indent=2), encoding="utf-8")
        except Exception:
            pass

    # ── public interface ────────────────────────────────────────────────────

    def record_state(
        self, obs: Observation, target: str, session_id: str, action_index: int
    ) -> str:
        state_id = fingerprint(obs)
        desc = semantic_description(obs)
        now = time.time()

        G = self._graph(target)
        if G is not None:
            if state_id not in G.nodes:
                G.add_node(
                    state_id,
                    window_title=obs.window_title,
                    visit_count=1,
                    bug_found=False,
                    first_seen=now,
                    last_seen=now,
                )
            else:
                G.nodes[state_id]["visit_count"] = G.nodes[state_id].get("visit_count", 0) + 1
                G.nodes[state_id]["last_seen"] = now

        col = self._collection(target, "states")
        if col is not None:
            embed = self._embedder.embed(desc)
            doc_id = f"{state_id}_{action_index}"
            meta: Dict[str, Any] = {
                "state_id": state_id,
                "window_title": obs.window_title,
                "bug_found": False,
                "session_id": session_id,
                "timestamp": now,
            }
            try:
                if embed is not None:
                    col.upsert(ids=[doc_id], documents=[desc], embeddings=[embed], metadatas=[meta])
                else:
                    col.upsert(ids=[doc_id], documents=[desc], metadatas=[meta])
            except Exception:
                pass

        return state_id

    def record_transition(
        self,
        from_id: str,
        action: Dict[str, Any],
        to_id: str,
        target: str,
        session_id: str,
        success: bool = True,
    ) -> None:
        G = self._graph(target)
        if G is None:
            return
        label = summarize_action(action)
        if G.has_edge(from_id, to_id):
            acts = G[from_id][to_id].get("actions", [])
            acts.append(label)
            G[from_id][to_id]["actions"] = acts
            G[from_id][to_id]["count"] = G[from_id][to_id].get("count", 0) + 1
        else:
            G.add_edge(from_id, to_id, actions=[label], count=1, success=success)

    def record_finding(
        self,
        title: str,
        severity: str,
        state_id: str,
        action_sequence: List[Dict[str, Any]],
        target: str,
        session_id: str,
        expected: str = "",
        actual: str = "",
        detail: str = "",
    ) -> str:
        bug_id = str(uuid.uuid4())[:8]
        text = f"Bug: {title}. Expected: {expected}. Actual: {actual}. {detail}"
        now = time.time()

        G = self._graph(target)
        if G is not None and state_id in G.nodes:
            G.nodes[state_id]["bug_found"] = True

        col = self._collection(target, "bugs")
        if col is not None:
            embed = self._embedder.embed(text)
            meta: Dict[str, Any] = {
                "bug_id": bug_id,
                "title": title,
                "severity": severity,
                "state_id": state_id,
                "session_id": session_id,
                "timestamp": now,
                "expected": expected,
                "actual": actual,
            }
            try:
                if embed is not None:
                    col.upsert(ids=[bug_id], documents=[text], embeddings=[embed], metadatas=[meta])
                else:
                    col.upsert(ids=[bug_id], documents=[text], metadatas=[meta])
            except Exception:
                pass

        return bug_id

    def record_assertion(
        self,
        assertion_type: str,
        expected: Any,
        state_id: str,
        passed: bool,
        target: str,
        session_id: str,
    ) -> None:
        if passed:
            return
        self.record_finding(
            title=f"assertion:{assertion_type}",
            severity="medium",
            state_id=state_id,
            action_sequence=[],
            target=target,
            session_id=session_id,
            expected=str(expected),
            actual="assertion_failed",
        )

    def retrieve(
        self, obs: Observation, target: str, top_k: int = 5
    ) -> KnowledgeContext:
        ctx = KnowledgeContext()
        desc = semantic_description(obs)

        # semantic similar-state search
        col = self._collection(target, "states")
        if col is not None:
            try:
                count = col.count()
                if count > 0:
                    embed = self._embedder.embed(desc)
                    n = min(top_k, count)
                    if embed is not None:
                        results = col.query(query_embeddings=[embed], n_results=n)
                    else:
                        results = col.query(query_texts=[desc], n_results=n)
                    metas = results.get("metadatas", [[]])[0]
                    dists = results.get("distances", [[]])[0]
                    G = self._graph(target)
                    seen: set = set()
                    for meta, dist in zip(metas, dists):
                        sid = meta.get("state_id", "")
                        if sid in seen:
                            continue
                        seen.add(sid)
                        actions_taken: List[str] = []
                        if G is not None and sid in G.nodes:
                            for _, _, data in G.out_edges(sid, data=True):
                                actions_taken.extend(data.get("actions", [])[:2])
                        ctx.similar_states.append(SimilarState(
                            state_id=sid,
                            similarity=max(0.0, 1.0 - dist),
                            window_title=meta.get("window_title", ""),
                            actions_taken=actions_taken[:6],
                            bug_found=bool(meta.get("bug_found", False)),
                        ))
            except Exception:
                pass

        # nearby bug search
        bug_col = self._collection(target, "bugs")
        if bug_col is not None:
            try:
                count = bug_col.count()
                if count > 0:
                    embed = self._embedder.embed(desc)
                    n = min(3, count)
                    if embed is not None:
                        results = bug_col.query(query_embeddings=[embed], n_results=n)
                    else:
                        results = bug_col.query(query_texts=[desc], n_results=n)
                    for meta in results.get("metadatas", [[]])[0]:
                        ctx.past_bugs.append(PastBug(
                            title=meta.get("title", ""),
                            severity=meta.get("severity", "medium"),
                            detail=meta.get("actual", ""),
                            state_id=meta.get("state_id", ""),
                        ))
            except Exception:
                pass

        # graph-based unexplored hints
        current_id = fingerprint(obs)
        G = self._graph(target)
        if G is not None and current_id in G.nodes:
            tried = {
                act
                for _, _, d in G.out_edges(current_id, data=True)
                for act in d.get("actions", [])
            }
            for el in obs.elements:
                if not el.name.strip():
                    continue
                hint = f"click '{el.name}' ({el.control_type})"
                if hint not in tried:
                    ctx.unexplored_hints.append(hint)
                    if len(ctx.unexplored_hints) >= 5:
                        break

        return ctx

    def finalize_session(self, session_id: str, target: str) -> None:
        self._save_graph(target)

    def get_stats(self, target: Optional[str] = None) -> Dict[str, Any]:
        stats: Dict[str, Any] = {}
        if target:
            keys = [target_key(target)]
            target_map = {target_key(target): target}
        else:
            graph_files = list(self._dir.glob("*.graph.json"))
            keys = [f.stem for f in graph_files]
            target_map = {k: k for k in keys}

        for key in keys:
            G = self._graph(target_map[key])
            stats[target_map[key]] = {
                "states": len(G.nodes) if G is not None else 0,
                "transitions": len(G.edges) if G is not None else 0,
                "bug_nodes": sum(
                    1 for _, d in (G.nodes(data=True) if G else []) if d.get("bug_found")
                ),
            }
        return stats

    def clear_target(self, target: str) -> None:
        key = target_key(target)
        path = self._dir / f"{key}.graph.json"
        if path.exists():
            path.unlink()
        self._graphs.pop(key, None)
        client = self._chroma()
        if client:
            for suffix in ("states", "bugs"):
                try:
                    client.delete_collection(f"{key}_{suffix}")
                except Exception:
                    pass

    def close(self) -> None:
        for tgt in list(self._graphs.keys()):
            self._save_graph(tgt)
