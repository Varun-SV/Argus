"""Ollama provider (local models, default ``localhost:11434``).

Vision detection: Argus asks Ollama **once** per session whether the model
is multimodal, via ``POST /api/show``. Newer Ollama versions return a
``capabilities`` list (containing ``"vision"``); older versions are covered
by checking the model family metadata for known vision projectors. If the
model cannot see, Argus tells the user that no vision-related testing is
possible and falls back to accessibility-tree-only observation.

Ollama is local and free, so it carries no token budget — sessions are
bounded by time instead. Token usage is still counted (Ollama reports
``prompt_eval_count`` / ``eval_count``) so totals stay queryable.
"""

from __future__ import annotations

import base64
import json
from typing import List, Optional

import requests

from argus.providers.base import LLMProvider, LLMResponse, ProviderError
from argus.tokens import TokenTracker, Usage

# Model families that imply a vision projector in older Ollama metadata.
_VISION_FAMILIES = {"clip", "mllama", "llava", "qwen2vl", "gemma3", "minicpmv", "moondream"}


class OllamaProvider(LLMProvider):
    type_name = "ollama"

    def __init__(
        self,
        model: str,
        host: str = "http://localhost:11434",
        tracker: Optional[TokenTracker] = None,
        timeout: float = 300.0,
    ) -> None:
        super().__init__(model, tracker)
        self.host = host.rstrip("/")
        self.timeout = timeout

    # ---- chat -------------------------------------------------------------

    def chat(
        self,
        system: str,
        user: str,
        images: Optional[List[bytes]] = None,
    ) -> LLMResponse:
        message: dict = {"role": "user", "content": user}
        if images:
            if not self.supports_vision():
                raise ProviderError(
                    f"model '{self.model}' is not multimodal — cannot send images"
                )
            message["images"] = [base64.b64encode(img).decode("ascii") for img in images]

        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, message],
            "stream": False,
        }
        try:
            resp = requests.post(
                f"{self.host}/api/chat", json=payload, timeout=self.timeout
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise ProviderError(f"Ollama request failed: {exc}") from exc

        data = resp.json()
        usage = Usage(
            prompt_tokens=int(data.get("prompt_eval_count", 0) or 0),
            completion_tokens=int(data.get("eval_count", 0) or 0),
        )
        self._record(usage)
        text = (data.get("message") or {}).get("content", "")
        return LLMResponse(text=text, usage=usage)

    # ---- capability detection ----------------------------------------------

    def _detect_vision(self) -> bool:
        """Ask Ollama once whether the model is multimodal."""
        try:
            resp = requests.post(
                f"{self.host}/api/show", json={"model": self.model}, timeout=30
            )
            resp.raise_for_status()
            info = resp.json()
        except requests.RequestException as exc:
            raise ProviderError(
                f"could not query Ollama for model capabilities: {exc}"
            ) from exc

        # Modern Ollama: explicit capabilities list.
        capabilities = info.get("capabilities") or []
        if "vision" in capabilities:
            return True
        if capabilities:  # list present but no "vision"
            return False

        # Older Ollama: infer from family metadata / projector details.
        details = info.get("details") or {}
        families = {f.lower() for f in (details.get("families") or [])}
        if families & _VISION_FAMILIES:
            return True
        model_info = info.get("model_info") or {}
        if any("vision" in key.lower() for key in model_info):
            return True
        if "projector_info" in info:
            return True
        return False

    # ---- health -------------------------------------------------------------

    def check_connection(self) -> dict:
        try:
            resp = requests.get(f"{self.host}/api/tags", timeout=10)
            resp.raise_for_status()
            models = [m.get("name", "") for m in resp.json().get("models", [])]
        except requests.RequestException as exc:
            return {"ok": False, "detail": f"cannot reach Ollama at {self.host}: {exc}"}

        # Tag-insensitive match: "gemma3:9b" should match installed "gemma3:9b",
        # and bare "gemma3" should match any "gemma3:*".
        wanted = self.model
        found = any(m == wanted or m.split(":")[0] == wanted for m in models)
        if not found:
            return {
                "ok": False,
                "detail": (
                    f"model '{wanted}' is not pulled "
                    f"(available: {', '.join(models) or 'none'}) — run: ollama pull {wanted}"
                ),
            }
        try:
            vision = self.supports_vision()
        except ProviderError as exc:
            return {"ok": False, "detail": str(exc)}
        return {
            "ok": True,
            "detail": f"connected · model '{wanted}' "
            + ("supports vision" if vision else "is TEXT-ONLY (no vision testing)"),
            "vision": vision,
        }
