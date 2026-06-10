from unittest import mock

import pytest

from argus.providers import ProviderError, create_provider
from argus.providers.ollama import OllamaProvider
from argus.tokens import TokenTracker


def test_registry_ollama():
    p = create_provider("ollama", model="gemma3:9b")
    assert isinstance(p, OllamaProvider)
    assert p.host == "http://localhost:11434"


def test_registry_anthropic_needs_key():
    with pytest.raises(ProviderError, match="API key"):
        create_provider("anthropic", model="claude-sonnet-4-6", api_key="")


def test_registry_azure_needs_base_url():
    with pytest.raises(ProviderError, match="base_url"):
        create_provider("azure", model="gpt-4o", api_key="k")


def test_registry_unknown():
    with pytest.raises(ProviderError, match="unknown provider"):
        create_provider("clippy", model="x")


def _resp(payload, status=200):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = payload
    r.raise_for_status = mock.Mock()
    return r


def test_ollama_vision_detection_capabilities():
    p = OllamaProvider("gemma3:9b")
    with mock.patch("requests.post", return_value=_resp({"capabilities": ["completion", "vision"]})):
        assert p.supports_vision() is True


def test_ollama_vision_detection_text_only():
    p = OllamaProvider("llama3:8b")
    with mock.patch("requests.post", return_value=_resp({"capabilities": ["completion"]})):
        assert p.supports_vision() is False


def test_ollama_vision_detection_old_families():
    p = OllamaProvider("llava:13b")
    with mock.patch(
        "requests.post",
        return_value=_resp({"details": {"families": ["llama", "clip"]}}),
    ):
        assert p.supports_vision() is True


def test_ollama_vision_cached_single_probe():
    p = OllamaProvider("gemma3:9b")
    with mock.patch("requests.post", return_value=_resp({"capabilities": ["vision"]})) as m:
        p.supports_vision()
        p.supports_vision()
        p.supports_vision()
        assert m.call_count == 1  # asked Ollama exactly once


def test_ollama_chat_counts_tokens():
    tracker = TokenTracker()
    p = OllamaProvider("gemma3:9b", tracker=tracker)
    payload = {
        "message": {"content": '{"action":"done","success":true}'},
        "prompt_eval_count": 123,
        "eval_count": 45,
    }
    with mock.patch("requests.post", return_value=_resp(payload)):
        out = p.chat(system="s", user="u")
    assert out.text.startswith('{"action"')
    assert tracker.total_tokens == 168


def test_ollama_image_to_text_only_model_raises():
    p = OllamaProvider("llama3:8b")
    p._vision = False  # already probed
    with pytest.raises(ProviderError, match="not multimodal"):
        p.chat(system="s", user="u", images=[b"png"])


def test_ollama_check_connection_model_missing():
    p = OllamaProvider("gemma3:9b")
    tags = _resp({"models": [{"name": "llama3:8b"}]})
    with mock.patch("requests.get", return_value=tags):
        status = p.check_connection()
    assert status["ok"] is False
    assert "ollama pull gemma3:9b" in status["detail"]
