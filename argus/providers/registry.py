"""Provider factory — turns config into a concrete :class:`LLMProvider`.

Azure, Gemini and LiteLLM ride on the OpenAI-compatible provider with a
custom ``base_url``; only Ollama and Anthropic have native wire formats.
"""

from __future__ import annotations

from typing import Optional

from argus.providers.base import LLMProvider, ProviderError
from argus.providers.ollama import OllamaProvider
from argus.providers.anthropic_provider import AnthropicProvider
from argus.providers.openai_provider import OpenAICompatProvider
from argus.tokens import TokenTracker

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"

PROVIDER_TYPES = ("ollama", "anthropic", "openai", "azure", "gemini", "litellm")


def create_provider(
    provider_type: str,
    model: str,
    api_key: str = "",
    base_url: Optional[str] = None,
    tracker: Optional[TokenTracker] = None,
) -> LLMProvider:
    provider_type = (provider_type or "").lower().strip()

    if provider_type == "ollama":
        return OllamaProvider(
            model=model,
            host=base_url or "http://localhost:11434",
            tracker=tracker,
        )
    if provider_type == "anthropic":
        return AnthropicProvider(model=model, api_key=api_key, tracker=tracker)
    if provider_type == "openai":
        return OpenAICompatProvider(
            model=model,
            api_key=api_key,
            base_url=base_url or "https://api.openai.com/v1",
            tracker=tracker,
        )
    if provider_type == "gemini":
        return OpenAICompatProvider(
            model=model,
            api_key=api_key,
            base_url=base_url or _GEMINI_BASE,
            tracker=tracker,
            type_name="gemini",
        )
    if provider_type in ("azure", "litellm"):
        if not base_url:
            raise ProviderError(
                f"provider '{provider_type}' needs a base_url in .argus/config.yaml "
                "(your Azure deployment / LiteLLM proxy endpoint)"
            )
        return OpenAICompatProvider(
            model=model,
            api_key=api_key,
            base_url=base_url,
            tracker=tracker,
            type_name=provider_type,
        )
    raise ProviderError(
        f"unknown provider '{provider_type}' — expected one of: {', '.join(PROVIDER_TYPES)}"
    )
