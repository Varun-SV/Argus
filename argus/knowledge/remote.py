"""RemoteKnowledgeStore: Qdrant (vector) + NetworkX (graph).

Used for Docker-managed or externally-hosted Qdrant instances.
Graph data is still stored locally as JSON (Neo4j support is planned).
"""

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


class RemoteKnowledgeStore(KnowledgeStore):
    """Qdrant vector store + local NetworkX graph (JSON-backed)."""

    def __init__(
        self,
        persist_dir: Path,
        vector_url: str,
        embedding_model: str = "all-MiniLM-L6-v2",
    ) -> None:
        self._dir = persist_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._vector_url = vector_url
        self._embedder = EmbeddingGenerator(embedding_model)
        self._qdrant = None
        self._graphs: Dict[str, Any] = {}

    def _client(self):
        if self._qdrant is None:
            try:
                from qdrant_client import QdrantClient
                self._qdrant = QdrantClient(url=self._vector_url)
            except Exception:
                pass
        return self._qdrant

    def _graph(self, target: str):
        key = target_key(target)
        if key not in self._graphs:
            try:
                import networkx as nx
                G = nx.DiGraph()
                path = self._dir / f"{key}.graph.json"
                if path.exists():
                    data = json.loads(path.read_text(encoding="utf-8"))
                    G = nx.node_link_graph(data)
                self._graphs[key] = G
            except ImportError:
                self._graphs[key] = None
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

    def _ensure_collection(self, name: str, vector_size: int = 384) -> bool:
        client = self._client()
        if client is None:
            return False
        try:
            from qdrant_client.models import Distance, VectorParams
            existing = [c.name for c in client.get_collections().collections]
            if name not in existing:
                client.create_collection(
                    collection_name=name,
                    vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
                )
            return True
        except Exception:
            return False

    @staticmethod
    def _str_to_uint(s: str) -> int:
        h = 0
        for c in s:
            h = (h * 31 + ord(c)) & 0xFFFFFFFF
        return h

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

        embed = self._embedder.embed(desc)
        if embed is not None:
            col_name = f"{target_key(target)}_states"
            if self._ensure_collection(col_name, len(embed)):
                try:
                    from qdrant_client.models import PointStruct
                    client = self._client()
                    if client:
                        point = PointStruct(
                            id=self._str_to_uint(state_id + session_id + str(action_index)),
                            vector=embed,
                            payload={
                                "state_id": state_id,
                                "window_title": obs.window_title,
                                "bug_found": False,
                                "session_id": session_id,
                                "timestamp": now,
                            },
                        )
                        client.upsert(collection_name=col_name, points=[point])
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
            G[from_id][to_id].setdefault("actions", []).append(label)
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

        embed = self._embedder.embed(text)
        if embed is not None:
            col_name = f"{target_key(target)}_bugs"
            if self._ensure_collection(col_name, len(embed)):
                try:
                    from qdrant_client.models import PointStruct
                    client = self._client()
                    if client:
                        point = PointStruct(
                            id=self._str_to_uint(bug_id + session_id),
                            vector=embed,
                            payload={
                                "bug_id": bug_id,
                                "title": title,
                                "severity": severity,
                                "state_id": state_id,
                                "session_id": session_id,
                                "timestamp": now,
                                "expected": expected,
                                "actual": actual,
                            },
                        )
                        client.upsert(collection_name=col_name, points=[point])
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
        embed = self._embedder.embed(desc)
        client = self._client()

        if embed is not None and client is not None:
            col_name = f"{target_key(target)}_states"
            try:
                results = client.search(
                    collection_name=col_name,
                    query_vector=embed,
                    limit=top_k,
                    with_payload=True,
                )
                G = self._graph(target)
                seen: set = set()
                for hit in results:
                    sid = hit.payload.get("state_id", "")
                    if sid in seen:
                        continue
                    seen.add(sid)
                    actions_taken: List[str] = []
                    if G is not None and sid in G.nodes:
                        for _, _, data in G.out_edges(sid, data=True):
                            actions_taken.extend(data.get("actions", [])[:2])
                    ctx.similar_states.append(SimilarState(
                        state_id=sid,
                        similarity=hit.score,
                        window_title=hit.payload.get("window_title", ""),
                        actions_taken=actions_taken[:6],
                        bug_found=bool(hit.payload.get("bug_found", False)),
                    ))
            except Exception:
                pass

            bug_col = f"{target_key(target)}_bugs"
            try:
                results = client.search(
                    collection_name=bug_col,
                    query_vector=embed,
                    limit=3,
                    with_payload=True,
                )
                for hit in results:
                    ctx.past_bugs.append(PastBug(
                        title=hit.payload.get("title", ""),
                        severity=hit.payload.get("severity", "medium"),
                        detail=hit.payload.get("actual", ""),
                        state_id=hit.payload.get("state_id", ""),
                    ))
            except Exception:
                pass

        # graph-based unexplored hints (works even without Qdrant)
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
        client = self._client()
        if client:
            for suffix in ("states", "bugs"):
                try:
                    client.delete_collection(f"{key}_{suffix}")
                except Exception:
                    pass

    def close(self) -> None:
        for tgt in list(self._graphs.keys()):
            self._save_graph(tgt)
