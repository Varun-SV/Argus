"""OpenAI-compatible provider.

Covers OpenAI itself **and** every service exposing the same chat-completions
API: Azure OpenAI, Google Gemini (OpenAI-compat endpoint), LiteLLM proxies,
vLLM, LM Studio, … — just point ``base_url`` at the service.

Vision detection: a one-time probe. We send a 1×1 PNG with a trivial prompt;
if the endpoint rejects image content the model is treated as text-only and
Argus degrades to accessibility-tree observation.
"""

from __future__ import annotations

import base64
from typing import List, Optional

import requests

from argus.providers.base import LLMProvider, LLMResponse, ProviderError
from argus.tokens import TokenTracker, Usage

# A 1x1 transparent PNG used for the one-time vision capability probe.
_PROBE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQAB"
    "h6FO1AAAAABJRU5ErkJggg=="
)


class OpenAICompatProvider(LLMProvider):
    type_name = "openai"

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        tracker: Optional[TokenTracker] = None,
        max_tokens: int = 2048,
        timeout: float = 300.0,
        type_name: Optional[str] = None,
    ) -> None:
        super().__init__(model, tracker)
        if not api_key:
            raise ProviderError(
                "this provider requires an API key (config api_key or ARGUS_API_KEY)"
            )
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.max_tokens = max_tokens
        self.timeout = timeout
        if type_name:  # e.g. "azure", "gemini", "litellm" for display purposes
            self.type_name = type_name

    # ---- chat ---------------------------------------------------------------

    def _post_chat(self, messages: list) -> dict:
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=headers,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"{self.type_name} request failed: {exc}") from exc
        if resp.status_code != 200:
            raise ProviderError(
                f"{self.type_name} API error {resp.status_code}: {resp.text[:300]}"
            )
        return resp.json()

    def chat(
        self,
        system: str,
        user: str,
        images: Optional[List[bytes]] = None,
    ) -> LLMResponse:
        if images:
            content: list = [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64,"
                        + base64.b64encode(img).decode("ascii")
                    },
                }
                for img in images
            ]
            content.append({"type": "text", "text": user})
        else:
            content = user  # plain string for text-only

        data = self._post_chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ]
        )
        usage_raw = data.get("usage") or {}
        usage = Usage(
            prompt_tokens=int(usage_raw.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage_raw.get("completion_tokens", 0) or 0),
        )
        self._record(usage)
        choices = data.get("choices") or []
        text = (choices[0].get("message") or {}).get("content", "") if choices else ""
        return LLMResponse(text=text, usage=usage)

    # ---- capability detection --------------------------------------------------

    def _detect_vision(self) -> bool:
        """One-time probe: send a 1×1 PNG; rejection means text-only."""
        try:
            self.chat(
                system="Reply with the single word: ok",
                user="Can you see the attached image?",
                images=None,  # avoid recursion through the vision gate below
            )
        except ProviderError as exc:
            raise ProviderError(f"cannot probe model capabilities: {exc}") from exc

        # Now the actual image probe, bypassing supports_vision() gating.
        probe = [
            {"role": "system", "content": "Reply with the single word: ok"},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,"
                            + base64.b64encode(_PROBE_PNG).decode("ascii")
                        },
                    },
                    {"type": "text", "text": "ok?"},
                ],
            },
        ]
        try:
            data = self._post_chat(probe)
        except ProviderError:
            return False
        usage_raw = data.get("usage") or {}
        self._record(
            Usage(
                prompt_tokens=int(usage_raw.get("prompt_tokens", 0) or 0),
                completion_tokens=int(usage_raw.get("completion_tokens", 0) or 0),
            )
        )
        return True

    # ---- health ------------------------------------------------------------------

    def check_connection(self) -> dict:
        try:
            response = self.chat(system="Reply with the single word: ok", user="ping")
        except ProviderError as exc:
            return {"ok": False, "detail": str(exc)}
        try:
            vision = self.supports_vision()
        except ProviderError:
            vision = False
        return {
            "ok": True,
            "detail": f"connected · model '{self.model}' "
            + ("supports vision" if vision else "is TEXT-ONLY (no vision testing)")
            + f" (probe used {response.usage.total_tokens} tokens)",
            "vision": vision,
        }
