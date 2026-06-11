"""Token usage tracking and run budgets.

Every provider call reports its usage here. Totals are queryable at any
point in time (``argus tokens``, the GUI status bar, roam reports) and are
persisted per project so usage survives across invocations.

Budgets:
  * Ollama (local) — no token budget; runs are bounded by *time*.
  * Paid providers — the user picks a token budget, a time budget, or both.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

# Per-model pricing in USD per 1 000 tokens (prompt, completion).
# Ollama models are free (local).
_MODEL_PRICING: Dict[str, tuple] = {
    "claude-sonnet-4-6":       (0.003, 0.015),
    "claude-opus-4-8":         (0.015, 0.075),
    "claude-haiku-4-5":        (0.00025, 0.00125),
    "claude-3-5-sonnet-20241022": (0.003, 0.015),
    "claude-3-haiku-20240307": (0.00025, 0.00125),
    "gpt-4o":                  (0.005, 0.015),
    "gpt-4o-mini":             (0.00015, 0.0006),
    "gpt-4-turbo":             (0.01,   0.03),
    "gemini-2.0-flash":        (0.00015, 0.0006),
    "gemini-1.5-pro":          (0.00125, 0.005),
}


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Return estimated cost in USD, or 0.0 for local/unknown models."""
    key = model.lower()
    if key not in _MODEL_PRICING:
        # fuzzy match — strip date suffixes and try again
        for k in _MODEL_PRICING:
            if key.startswith(k) or k.startswith(key):
                key = k
                break
        else:
            return 0.0
    p_rate, c_rate = _MODEL_PRICING[key]
    return round(prompt_tokens * p_rate / 1000 + completion_tokens * c_rate / 1000, 6)


@dataclass
class Usage:
    """Token usage for a single LLM call."""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class TokenTracker:
    """Accumulates token usage across calls. Thread-safe.

    One tracker is shared per session (run / roam / GUI); totals can be
    read at any moment while work is still in flight.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, usage: Usage) -> None:
        with self._lock:
            self.prompt_tokens += usage.prompt_tokens
            self.completion_tokens += usage.completion_tokens
            self.calls += 1

    @property
    def total_tokens(self) -> int:
        with self._lock:
            return self.prompt_tokens + self.completion_tokens

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.prompt_tokens + self.completion_tokens,
                "calls": self.calls,
            }

    # ---- persistence ----------------------------------------------------

    def persist(self, project_dir: Path) -> None:
        """Merge this tracker's totals into ``.argus/usage.json``."""
        path = project_dir / ".argus" / "usage.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}
        if path.exists():
            try:
                data.update(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                pass
        snap = self.snapshot()
        data["prompt_tokens"] += snap["prompt_tokens"]
        data["completion_tokens"] += snap["completion_tokens"]
        data["calls"] += snap["calls"]
        data["total_tokens"] = data["prompt_tokens"] + data["completion_tokens"]
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @staticmethod
    def load_persisted(project_dir: Path) -> dict:
        path = project_dir / ".argus" / "usage.json"
        if not path.exists():
            return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}


class Budget:
    """A time and/or token budget for a run or roam session.

    ``exhausted()`` is checked between agent steps. A budget with neither
    limit never exhausts (not recommended; the CLI always sets a time
    limit by default).
    """

    def __init__(
        self,
        max_seconds: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tracker: Optional[TokenTracker] = None,
    ) -> None:
        self.max_seconds = max_seconds
        self.max_tokens = max_tokens
        self.tracker = tracker
        self._started_at = time.monotonic()

    def restart(self) -> None:
        self._started_at = time.monotonic()

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._started_at

    @property
    def remaining_seconds(self) -> Optional[float]:
        if self.max_seconds is None:
            return None
        return max(0.0, self.max_seconds - self.elapsed_seconds)

    def exhausted(self) -> Optional[str]:
        """Return a human-readable reason if the budget is spent, else None."""
        if self.max_seconds is not None and self.elapsed_seconds >= self.max_seconds:
            return f"time budget reached ({self.max_seconds:.0f}s)"
        if (
            self.max_tokens is not None
            and self.tracker is not None
            and self.tracker.total_tokens >= self.max_tokens
        ):
            return f"token budget reached ({self.tracker.total_tokens}/{self.max_tokens} tokens)"
        return None

    def describe(self) -> str:
        parts = []
        if self.max_seconds is not None:
            parts.append(f"{self.max_seconds:.0f}s")
        if self.max_tokens is not None:
            parts.append(f"{self.max_tokens} tokens")
        return " + ".join(parts) if parts else "unbounded"
