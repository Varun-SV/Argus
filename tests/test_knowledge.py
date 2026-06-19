"""Tests for the knowledge engine module.

Uses only stdlib — no chromadb, networkx, or sentence-transformers required
(the LocalKnowledgeStore degrades gracefully when those are absent).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from argus.adapters.base import Observation, UIElement
from argus.knowledge.base import KnowledgeContext, KnowledgeStore
from argus.knowledge.fingerprint import fingerprint, semantic_description, summarize_action, target_key
from argus.knowledge.store import LocalKnowledgeStore


# ── helpers ──────────────────────────────────────────────────────────────────

def _obs(title: str, elements=None, error=None, alive=True) -> Observation:
    els = elements or [
        UIElement(element_id=1, control_type="Button", name="OK", rect=(0, 0, 50, 20)),
        UIElement(element_id=2, control_type="Edit", name="Input", rect=(0, 30, 200, 50)),
    ]
    return Observation(window_title=title, elements=els, process_alive=alive, error=error)


@pytest.fixture()
def store(tmp_path: Path) -> LocalKnowledgeStore:
    return LocalKnowledgeStore(persist_dir=tmp_path / "knowledge")


# ── fingerprinting ───────────────────────────────────────────────────────────

def test_fingerprint_same_obs_same_id():
    obs = _obs("Notepad")
    assert fingerprint(obs) == fingerprint(obs)


def test_fingerprint_different_title_different_id():
    obs1 = _obs("Notepad")
    obs2 = _obs("Calculator")
    assert fingerprint(obs1) != fingerprint(obs2)


def test_fingerprint_length():
    fp = fingerprint(_obs("Test"))
    assert len(fp) == 16


def test_semantic_description_includes_title():
    desc = semantic_description(_obs("My Window"))
    assert "My Window" in desc


def test_target_key_normalises():
    assert target_key("Notepad.exe") == "notepad-exe"
    assert target_key("http://localhost:3000") == "http-localhost-3000"


def test_summarize_action_click():
    assert "click" in summarize_action({"action": "click", "element_id": 5})


def test_summarize_action_type():
    summary = summarize_action({"action": "type", "text": "hello world"})
    assert "type" in summary and "hello" in summary


# ── LocalKnowledgeStore — record & retrieve ──────────────────────────────────

def test_record_state_returns_consistent_id(store):
    obs = _obs("Notepad")
    id1 = store.record_state(obs, "notepad", "session1", 0)
    id2 = store.record_state(obs, "notepad", "session1", 1)
    assert id1 == id2  # same observation → same fingerprint


def test_record_state_different_windows(store):
    id1 = store.record_state(_obs("Notepad"), "app", "s1", 0)
    id2 = store.record_state(_obs("Calculator"), "app", "s1", 1)
    assert id1 != id2


def test_record_transition_no_crash(store):
    obs_a = _obs("Notepad")
    obs_b = _obs("Notepad — File menu open")
    sid_a = store.record_state(obs_a, "notepad", "s1", 0)
    sid_b = store.record_state(obs_b, "notepad", "s1", 1)
    # Should not raise
    store.record_transition(sid_a, {"action": "click", "element_id": 2}, sid_b, "notepad", "s1")


def test_record_finding_returns_id(store):
    obs = _obs("Notepad")
    sid = store.record_state(obs, "notepad", "s1", 0)
    bug_id = store.record_finding(
        title="crash on save",
        severity="high",
        state_id=sid,
        action_sequence=[],
        target="notepad",
        session_id="s1",
        expected="file saved",
        actual="app crashed",
    )
    assert isinstance(bug_id, str) and len(bug_id) > 0


def test_record_assertion_failure_no_crash(store):
    obs = _obs("Notepad")
    sid = store.record_state(obs, "notepad", "s1", 0)
    # Must not raise
    store.record_assertion("text_visible", "hello", sid, passed=False, target="notepad", session_id="s1")


def test_record_assertion_pass_no_op(store):
    obs = _obs("Notepad")
    sid = store.record_state(obs, "notepad", "s1", 0)
    store.record_assertion("text_visible", "hello", sid, passed=True, target="notepad", session_id="s1")


def test_retrieve_returns_knowledge_context(store):
    for i in range(3):
        obs = _obs(f"Window {i}")
        store.record_state(obs, "app", "s1", i)
    ctx = store.retrieve(_obs("Window 1"), "app")
    assert isinstance(ctx, KnowledgeContext)


def test_knowledge_context_is_empty_initially():
    ctx = KnowledgeContext()
    assert ctx.is_empty()


def test_knowledge_context_format_non_empty():
    from argus.knowledge.base import PastBug, SimilarState
    ctx = KnowledgeContext(
        past_bugs=[PastBug(title="crash", severity="high", detail="boom", state_id="abc")],
    )
    fmt = ctx.format()
    assert "crash" in fmt
    assert not ctx.is_empty()


# ── graph serialisation ───────────────────────────────────────────────────────

def test_finalize_session_writes_graph(store):
    try:
        import networkx  # noqa: F401
    except ImportError:
        pytest.skip("networkx not installed")
    obs = _obs("Notepad")
    store.record_state(obs, "notepad", "s1", 0)
    store.finalize_session("s1", "notepad")
    graph_files = list(store._dir.glob("*.graph.json"))
    assert len(graph_files) == 1


def test_graph_round_trips(store):
    try:
        import networkx  # noqa: F401
    except ImportError:
        pytest.skip("networkx not installed")
    obs = _obs("Notepad")
    sid = store.record_state(obs, "notepad", "s1", 0)
    store.finalize_session("s1", "notepad")

    # New store reading the same dir should reload the graph
    store2 = LocalKnowledgeStore(persist_dir=store._dir)
    G = store2._graph("notepad")
    assert G is not None
    assert sid in G.nodes


def test_get_stats_returns_dict(store):
    obs = _obs("Notepad")
    store.record_state(obs, "notepad", "s1", 0)
    store.finalize_session("s1", "notepad")
    stats = store.get_stats("notepad")
    assert isinstance(stats, dict)


def test_clear_target_removes_data(store):
    obs = _obs("Notepad")
    store.record_state(obs, "notepad", "s1", 0)
    store.finalize_session("s1", "notepad")
    store.clear_target("notepad")
    stats = store.get_stats("notepad")
    total = sum(v.get("states", 0) for v in stats.values())
    assert total == 0


def test_close_no_crash(store):
    obs = _obs("Notepad")
    store.record_state(obs, "notepad", "s1", 0)
    store.close()  # must not raise


# ── factory ──────────────────────────────────────────────────────────────────

def test_create_knowledge_store_local(tmp_path):
    from argus.knowledge import create_knowledge_store
    ks = create_knowledge_store(enabled=True, store_type="local", persist_dir=tmp_path / "ks")
    # May be None if libraries missing, but must not raise
    if ks is not None:
        ks.close()


def test_create_knowledge_store_disabled():
    from argus.knowledge import create_knowledge_store
    ks = create_knowledge_store(enabled=False)
    assert ks is None


# ── integration: roam + runner callers never crash when ks=None ─────────────

def test_runner_accepts_none_knowledge_store(tmp_path):
    """run_test() must work normally when knowledge_store=None."""
    from tests.conftest import FakeAdapter, FakeProvider
    from argus.engine.runner import run_test
    from argus.engine.spec import TestSpec, NLStep

    provider = FakeProvider([
        '{"action":"done","success":true,"note":"done"}',
    ])
    adapter = FakeAdapter()
    spec = TestSpec(
        name="test",
        adapter="fake",
        launch="fake",
        steps=[NLStep(text="do something", kind="step")],
        retries=0,
        continue_on_failure=False,
    )
    result = run_test(spec, provider, adapter, knowledge_store=None)
    assert result.status in ("pass", "fail", "error")
