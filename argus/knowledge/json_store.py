"""Zero-dependency JSON knowledge store.

Stores states and findings as NDJSON files. Retrieves similar states using
TF-IDF keyword overlap — no external libraries required.
"""

from __future__ import annotations

import json
import math
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from argus.adapters.base import Observation
from argus.knowledge.base import KnowledgeContext, KnowledgeStore, PastBug, SimilarState
from argus.knowledge.fingerprint import fingerprint, semantic_description, summarize_action, target_key


def _tokenize(text: str) -> List[str]:
    return [w.lower() for w in text.split() if len(w) > 1]


def _tf_idf_sim(query_tokens: List[str], doc_tokens: List[str]) -> float:
    if not query_tokens or not doc_tokens:
        return 0.0
    q = Counter(query_tokens)
    d = Counter(doc_tokens)
    common = set(q) & set(d)
    if not common:
        return 0.0
    dot = sum(q[t] * d[t] for t in common)
    mag_q = math.sqrt(sum(v * v for v in q.values()))
    mag_d = math.sqrt(sum(v * v for v in d.values()))
    return dot / (mag_q * mag_d) if mag_q and mag_d else 0.0


class JsonKnowledgeStore(KnowledgeStore):
    """Pure-Python knowledge store — works with no optional dependencies.

    Layout under persist_dir:
      <target_key>.states.ndjson   — one JSON object per line
      <target_key>.bugs.ndjson     — one JSON object per line
      <target_key>.graph.json      — {nodes: {state_id: {visit_count, ...}},
                                       edges: [[from, to, label], ...]}
    """

    def __init__(self, persist_dir: Path) -> None:
        self._dir = persist_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        # In-memory graph: {target_key: {"nodes": {sid: {...}}, "edges": [...]}}
        self._graphs: Dict[str, Dict[str, Any]] = {}

    # ── helpers ─────────────────────────────────────────────────────────────

    def _key(self, target: str) -> str:
        return target_key(target)

    def _graph(self, target: str) -> Dict[str, Any]:
        k = self._key(target)
        if k not in self._graphs:
            path = self._dir / f"{k}.graph.json"
            if path.exists():
                try:
                    self._graphs[k] = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    pass
            if k not in self._graphs:
                self._graphs[k] = {"nodes": {}, "edges": []}
        return self._graphs[k]

    def _save_graph(self, target: str) -> None:
        k = self._key(target)
        g = self._graphs.get(k)
        if g is None:
            return
        try:
            path = self._dir / f"{k}.graph.json"
            path.write_text(json.dumps(g), encoding="utf-8")
        except Exception:
            pass

    def _append_ndjson(self, path: Path, obj: Dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj) + "\n")

    def _read_ndjson(self, path: Path) -> List[Dict[str, Any]]:
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
        return rows

    # ── public interface ────────────────────────────────────────────────────

    def record_state(
        self, obs: Observation, target: str, session_id: str, action_index: int
    ) -> str:
        state_id = fingerprint(obs)
        desc = semantic_description(obs)
        now = time.time()

        g = self._graph(target)
        nodes = g["nodes"]
        if state_id not in nodes:
            nodes[state_id] = {
                "window_title": obs.window_title,
                "desc": desc,
                "visit_count": 1,
                "bug_found": False,
                "first_seen": now,
                "last_seen": now,
            }
        else:
            nodes[state_id]["visit_count"] = nodes[state_id].get("visit_count", 0) + 1
            nodes[state_id]["last_seen"] = now

        path = self._dir / f"{self._key(target)}.states.ndjson"
        self._append_ndjson(path, {
            "state_id": state_id,
            "window_title": obs.window_title,
            "desc": desc,
            "session_id": session_id,
            "action_index": action_index,
            "timestamp": now,
        })
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
        g = self._graph(target)
        g["edges"].append([from_id, to_id, summarize_action(action), success])

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
        now = time.time()

        g = self._graph(target)
        if state_id in g["nodes"]:
            g["nodes"][state_id]["bug_found"] = True

        path = self._dir / f"{self._key(target)}.bugs.ndjson"
        self._append_ndjson(path, {
            "bug_id": bug_id,
            "title": title,
            "severity": severity,
            "state_id": state_id,
            "session_id": session_id,
            "expected": expected,
            "actual": actual,
            "detail": detail,
            "timestamp": now,
        })
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
        query_tokens = _tokenize(semantic_description(obs))
        current_id = fingerprint(obs)

        # similar states via TF-IDF over state descriptions
        states = self._read_ndjson(self._dir / f"{self._key(target)}.states.ndjson")
        scored: List[tuple[float, Dict[str, Any]]] = []
        seen_ids: Set[str] = set()
        for row in states:
            sid = row.get("state_id", "")
            if sid == current_id or sid in seen_ids:
                continue
            seen_ids.add(sid)
            sim = _tf_idf_sim(query_tokens, _tokenize(row.get("desc", "")))
            if sim > 0:
                scored.append((sim, row))
        scored.sort(key=lambda x: -x[0])

        g = self._graph(target)
        nodes = g.get("nodes", {})
        for sim, row in scored[:top_k]:
            sid = row["state_id"]
            actions_taken: List[str] = [
                e[2] for e in g.get("edges", []) if e[0] == sid
            ][:6]
            ctx.similar_states.append(SimilarState(
                state_id=sid,
                similarity=sim,
                window_title=row.get("window_title", ""),
                actions_taken=actions_taken,
                bug_found=bool(nodes.get(sid, {}).get("bug_found", False)),
            ))

        # nearby bugs via title/detail similarity
        bugs = self._read_ndjson(self._dir / f"{self._key(target)}.bugs.ndjson")
        bug_scored: List[tuple[float, Dict[str, Any]]] = []
        for row in bugs:
            text = f"{row.get('title', '')} {row.get('detail', '')} {row.get('actual', '')}"
            sim = _tf_idf_sim(query_tokens, _tokenize(text))
            bug_scored.append((sim, row))
        bug_scored.sort(key=lambda x: -x[0])
        for _, row in bug_scored[:3]:
            ctx.past_bugs.append(PastBug(
                title=row.get("title", ""),
                severity=row.get("severity", "medium"),
                detail=row.get("actual", ""),
                state_id=row.get("state_id", ""),
            ))

        # unexplored hints from graph
        if current_id in nodes:
            tried: Set[str] = {
                e[2] for e in g.get("edges", []) if e[0] == current_id
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

    def confidence_for_state(self, state_id: str) -> int:
        for g in self._graphs.values():
            if state_id in g.get("nodes", {}):
                return int(g["nodes"][state_id].get("visit_count", 0))
        return 0

    def finalize_session(self, session_id: str, target: str) -> None:
        self._save_graph(target)

    def get_stats(self, target: Optional[str] = None) -> Dict[str, Any]:
        stats: Dict[str, Any] = {}

        def _stats_for(tgt: str) -> Dict[str, Any]:
            g = self._graph(tgt)
            nodes = g.get("nodes", {})
            bugs_path = self._dir / f"{self._key(tgt)}.bugs.ndjson"
            bugs = self._read_ndjson(bugs_path)
            sessions: set = set()
            for row in self._read_ndjson(self._dir / f"{self._key(tgt)}.states.ndjson"):
                sessions.add(row.get("session_id", ""))
            return {
                "states": len(nodes),
                "transitions": len(g.get("edges", [])),
                "bugs": len(bugs),
                "bug_nodes": sum(1 for n in nodes.values() if n.get("bug_found")),
                "sessions": len(sessions),
            }

        if target:
            stats[target] = _stats_for(target)
        else:
            for p in self._dir.glob("*.graph.json"):
                tgt = p.stem  # already a target_key
                stats[tgt] = _stats_for(tgt)
        return stats

    def clear_target(self, target: str) -> None:
        k = self._key(target)
        for suffix in ("states.ndjson", "bugs.ndjson", "graph.json"):
            p = self._dir / f"{k}.{suffix}"
            if p.exists():
                p.unlink()
        self._graphs.pop(k, None)

    def close(self) -> None:
        for k in list(self._graphs.keys()):
            # derive target name from key (best effort)
            self._save_graph(k)
