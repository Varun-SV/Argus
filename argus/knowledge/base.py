"""Abstract KnowledgeStore interface and shared data classes."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from argus.adapters.base import Observation


@dataclass
class SimilarState:
    state_id: str
    similarity: float
    window_title: str
    actions_taken: List[str] = field(default_factory=list)
    bug_found: bool = False


@dataclass
class PastBug:
    title: str
    severity: str
    detail: str
    state_id: str


@dataclass
class KnowledgeContext:
    similar_states: List[SimilarState] = field(default_factory=list)
    past_bugs: List[PastBug] = field(default_factory=list)
    unexplored_hints: List[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.similar_states or self.past_bugs or self.unexplored_hints)

    def format(self) -> str:
        lines: List[str] = []
        if self.past_bugs:
            lines.append("Known bugs near this area:")
            for b in self.past_bugs[:3]:
                lines.append(f"  [{b.severity}] {b.title}")
        if self.similar_states:
            lines.append("Similar past states:")
            for s in self.similar_states[:3]:
                tag = " (BUG FOUND HERE)" if s.bug_found else ""
                lines.append(f"  {s.window_title!r}{tag}")
                if s.actions_taken:
                    lines.append(f"    tried: {', '.join(s.actions_taken[:4])}")
        if self.unexplored_hints:
            lines.append("Suggested unexplored paths:")
            for h in self.unexplored_hints[:3]:
                lines.append(f"  - {h}")
        return "\n".join(lines)


class KnowledgeStore(ABC):
    @abstractmethod
    def record_state(
        self, obs: Observation, target: str, session_id: str, action_index: int
    ) -> str:
        """Record an observed UI state; returns a stable state_id."""

    @abstractmethod
    def record_transition(
        self,
        from_id: str,
        action: Dict[str, Any],
        to_id: str,
        target: str,
        session_id: str,
        success: bool = True,
    ) -> None:
        """Record a state → action → state edge."""

    @abstractmethod
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
        """Record a bug finding; returns a bug_id."""

    @abstractmethod
    def record_assertion(
        self,
        assertion_type: str,
        expected: Any,
        state_id: str,
        passed: bool,
        target: str,
        session_id: str,
    ) -> None:
        """Record an assertion outcome (only failures are stored)."""

    @abstractmethod
    def retrieve(
        self, obs: Observation, target: str, top_k: int = 5
    ) -> KnowledgeContext:
        """Retrieve relevant past experiences for the current observation."""

    @abstractmethod
    def finalize_session(self, session_id: str, target: str) -> None:
        """Flush and persist data at session end."""

    @abstractmethod
    def get_stats(self, target: Optional[str] = None) -> Dict[str, Any]:
        """Return counts of states, transitions, and bug nodes."""

    @abstractmethod
    def clear_target(self, target: str) -> None:
        """Delete all stored knowledge for a given target."""

    @abstractmethod
    def close(self) -> None:
        """Flush and release all resources."""
