import time

from argus.tokens import Budget, TokenTracker, Usage


def test_tracker_accumulates():
    t = TokenTracker()
    t.add(Usage(prompt_tokens=100, completion_tokens=20))
    t.add(Usage(prompt_tokens=50, completion_tokens=10))
    snap = t.snapshot()
    assert snap == {
        "prompt_tokens": 150, "completion_tokens": 30, "total_tokens": 180, "calls": 2,
    }
    assert t.total_tokens == 180


def test_tracker_persist_merges(tmp_path):
    t1 = TokenTracker()
    t1.add(Usage(10, 5))
    t1.persist(tmp_path)
    t2 = TokenTracker()
    t2.add(Usage(20, 5))
    t2.persist(tmp_path)
    data = TokenTracker.load_persisted(tmp_path)
    assert data["total_tokens"] == 40
    assert data["calls"] == 2


def test_time_budget():
    b = Budget(max_seconds=0.05)
    assert b.exhausted() is None
    time.sleep(0.06)
    assert "time budget" in b.exhausted()


def test_token_budget():
    t = TokenTracker()
    b = Budget(max_tokens=100, tracker=t)
    assert b.exhausted() is None
    t.add(Usage(90, 20))
    assert "token budget" in b.exhausted()


def test_unbounded_budget():
    b = Budget()
    assert b.exhausted() is None
    assert b.describe() == "unbounded"


def test_describe():
    t = TokenTracker()
    assert Budget(max_seconds=600, max_tokens=5000, tracker=t).describe() == "600s + 5000 tokens"
