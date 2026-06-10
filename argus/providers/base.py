"""Provider-agnostic LLM interface.

Every provider (Ollama, Anthropic, OpenAI, and OpenAI-compatible endpoints
such as Azure, Gemini, LiteLLM) implements the same small surface:

  * ``chat(system, user, images)`` — one completion, optionally multimodal.
  * ``supports_vision()`` — probed once and cached; if the model is not
    multimodal Argus warns the user and degrades to accessibility-tree-only
    observation (no screenshots are sent).
  * ``check_connection()`` — cheap health check used by ``argus providers``
    and the GUI.

All calls report token usage to the shared :class:`~argus.tokens.TokenTracker`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional

from argus.tokens import TokenTracker, Usage


class ProviderError(RuntimeError):
    """Raised when a provider call fails (connection, auth, bad model)."""


@dataclass
class LLMResponse:
    text: str
    usage: Usage


class LLMProvider(ABC):
    """Abstract base for all LLM providers."""

    #: machine id, e.g. "ollama", "anthropic", "openai"
    type_name: str = "base"

    def __init__(self, model: str, tracker: Optional[TokenTracker] = None) -> None:
        self.model = model
        self.tracker = tracker or TokenTracker()
        self._vision: Optional[bool] = None

    # ---- required interface ---------------------------------------------

    @abstractmethod
    def chat(
        self,
        system: str,
        user: str,
        images: Optional[List[bytes]] = None,
    ) -> LLMResponse:
        """Run one completion. ``images`` are raw PNG bytes (multimodal only)."""

    @abstractmethod
    def _detect_vision(self) -> bool:
        """Ask the backend (once) whether ``self.model`` is multimodal."""

    @abstractmethod
    def check_connection(self) -> dict:
        """Cheap health check. Returns {ok, detail} and raises nothing."""

    # ---- shared behaviour -------------------------------------------------

    def supports_vision(self) -> bool:
        """Whether the model can accept images. Probed once, then cached."""
        if self._vision is None:
            self._vision = self._detect_vision()
        return self._vision

    def describe(self) -> str:
        return f"{self.type_name}:{self.model}"

    def _record(self, usage: Usage) -> None:
        self.tracker.add(usage)
