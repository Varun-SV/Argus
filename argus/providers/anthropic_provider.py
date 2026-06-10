"""Anthropic provider (Claude). Uses the HTTP API directly via ``requests``
so Argus carries no SDK dependency.

Vision: all current Claude models accept images, so detection is a static
allowlist check rather than a network round-trip.
"""

from __future__ import annotations

import base64
from typing import List, Optional

import requests

from argus.providers.base import LLMProvider, LLMResponse, ProviderError
from argus.tokens import TokenTracker, Usage

_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"

# Claude model name fragments known to be text-only (none currently —
# every claude-3+ model is multimodal). Kept for future-proofing.
_TEXT_ONLY_FRAGMENTS: tuple = ()


class AnthropicProvider(LLMProvider):
    type_name = "anthropic"

    def __init__(
        self,
        model: str,
        api_key: str,
        tracker: Optional[TokenTracker] = None,
        max_tokens: int = 2048,
        timeout: float = 300.0,
    ) -> None:
        super().__init__(model, tracker)
        if not api_key:
            raise ProviderError(
                "Anthropic requires an API key (config api_key or ARGUS_API_KEY / ANTHROPIC_API_KEY)"
            )
        self.api_key = api_key
        self.max_tokens = max_tokens
        self.timeout = timeout

    def chat(
        self,
        system: str,
        user: str,
        images: Optional[List[bytes]] = None,
    ) -> LLMResponse:
        content: list = []
        for img in images or []:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.b64encode(img).decode("ascii"),
                    },
                }
            )
        content.append({"type": "text", "text": user})

        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": content}],
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": _API_VERSION,
            "content-type": "application/json",
        }
        try:
            resp = requests.post(_API_URL, json=payload, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ProviderError(f"Anthropic request failed: {exc}") from exc
        if resp.status_code != 200:
            raise ProviderError(f"Anthropic API error {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        usage_raw = data.get("usage") or {}
        usage = Usage(
            prompt_tokens=int(usage_raw.get("input_tokens", 0) or 0),
            completion_tokens=int(usage_raw.get("output_tokens", 0) or 0),
        )
        self._record(usage)
        text = "".join(
            block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
        )
        return LLMResponse(text=text, usage=usage)

    def _detect_vision(self) -> bool:
        return not any(frag in self.model for frag in _TEXT_ONLY_FRAGMENTS)

    def check_connection(self) -> dict:
        try:
            response = self.chat(system="Reply with the single word: ok", user="ping")
        except ProviderError as exc:
            return {"ok": False, "detail": str(exc)}
        return {
            "ok": True,
            "detail": f"connected · model '{self.model}' supports vision "
            f"(probe used {response.usage.total_tokens} tokens)",
            "vision": True,
        }
