"""Model clients: Claude through the Anthropic SDK, everything else through the
OpenAI-compatible chat API (OpenAI, Gemini, Ollama, LiteLLM, LM Studio …).

Every failure becomes an :class:`LLMError` whose message tells the user what
to do next, because these messages are shown as-is in the app.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests

Image = Tuple[bytes, str]  # (data, media type)


class LLMError(Exception):
    pass


@dataclass
class Reply:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0


class Usage:
    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.calls = 0

    def add(self, reply: Reply) -> None:
        self.input_tokens += reply.input_tokens
        self.output_tokens += reply.output_tokens
        self.calls += 1

    def as_dict(self) -> Dict[str, int]:
        return {"input": self.input_tokens, "output": self.output_tokens, "calls": self.calls}


class Client:
    """One provider + model. ``chat`` sends a system prompt and one user turn."""

    def __init__(self, info: Dict[str, Any], model: str, usage: Optional[Usage] = None) -> None:
        self.info = info
        self.model = model
        self.usage = usage or Usage()
        self.label = info["label"]

    # -- public ----------------------------------------------------------
    def chat(self, system: str, text: str, images: Optional[List[Image]] = None,
             max_tokens: int = 16000) -> Reply:
        if not self.model:
            raise LLMError("No model is selected. Open Settings and pick one.")
        if self.info["kind"] == "anthropic":
            reply = self._anthropic_chat(system, text, images or [], max_tokens)
        else:
            reply = self._openai_chat(system, text, images or [], max_tokens)
        self.usage.add(reply)
        return reply

    def list_models(self) -> List[str]:
        if self.info["kind"] == "anthropic":
            return self._anthropic_models()
        return self._openai_models()

    # -- Anthropic -------------------------------------------------------
    def _anthropic_client(self):
        try:
            import anthropic
        except ImportError:
            raise LLMError("The 'anthropic' package is not installed. Run: pip install anthropic") from None
        options: Dict[str, Any] = {"timeout": 180.0, "max_retries": 2}
        if self.info["api_key"]:
            options["api_key"] = self.info["api_key"]
        if self.info["base_url"]:
            options["base_url"] = self.info["base_url"]
        return anthropic, anthropic.Anthropic(**options)

    def _anthropic_call(self, func):
        anthropic, client = self._anthropic_client()
        try:
            return func(client)
        except anthropic.AuthenticationError:
            raise LLMError("Anthropic rejected the API key. Check it in Settings.") from None
        except anthropic.PermissionDeniedError as exc:
            raise LLMError(f"This Anthropic key is not allowed to do that: {_short(exc)}") from None
        except anthropic.NotFoundError:
            raise LLMError(f"Anthropic does not know the model '{self.model}'. "
                           "Pick another one in Settings.") from None
        except anthropic.RateLimitError:
            raise LLMError("Anthropic rate limit reached. Wait a moment and try again.") from None
        except anthropic.BadRequestError as exc:
            raise LLMError(f"Anthropic refused the request: {_short(exc)}") from None
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Anthropic returned an error ({exc.status_code}): {_short(exc)}") from None
        except anthropic.APIConnectionError:
            raise LLMError("Could not reach Anthropic. Check the internet connection"
                           + (f" and the base URL {self.info['base_url']}." if self.info["base_url"] else ".")
                           ) from None

    def _anthropic_chat(self, system: str, text: str, images: List[Image], max_tokens: int) -> Reply:
        content: List[Dict[str, Any]] = [
            {"type": "image", "source": {"type": "base64", "media_type": media,
                                         "data": base64.standard_b64encode(data).decode("ascii")}}
            for data, media in images
        ]
        content.append({"type": "text", "text": text})
        response = self._anthropic_call(lambda client: client.messages.create(
            model=self.model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": content}],
        ))
        if response.stop_reason == "refusal":
            raise LLMError("The model declined this request for safety reasons. "
                           "Try a different target or another model.")
        out = "".join(block.text for block in response.content if block.type == "text")
        return Reply(out, response.usage.input_tokens or 0, response.usage.output_tokens or 0)

    def _anthropic_models(self) -> List[str]:
        return self._anthropic_call(lambda client: [m.id for m in client.models.list(limit=1000)])

    # -- OpenAI-compatible -----------------------------------------------
    def _openai_request(self, method: str, path: str, body: Optional[dict] = None, timeout: int = 180) -> dict:
        base = self.info["base_url"]
        if not base:
            raise LLMError(f"{self.label} has no base URL. Set it in Settings.")
        headers = {"Content-Type": "application/json"}
        if self.info["api_key"]:
            headers["Authorization"] = f"Bearer {self.info['api_key']}"
        try:
            response = requests.request(method, base + path, headers=headers, json=body, timeout=timeout)
        except requests.exceptions.ConnectionError:
            hint = " Is Ollama running?" if self.info["provider"] == "ollama" else ""
            raise LLMError(f"Could not connect to {self.label} at {base}.{hint}") from None
        except requests.exceptions.Timeout:
            raise LLMError(f"{self.label} did not answer in time.") from None
        except requests.exceptions.RequestException as exc:
            raise LLMError(f"Request to {self.label} failed: {exc}") from None
        if response.status_code >= 400:
            detail = _error_detail(response)
            status = response.status_code
            if status in (401, 403):
                raise LLMError(f"{self.label} rejected the API key ({status}). Check it in Settings. {detail}".strip())
            if status == 404:
                raise LLMError(f"{self.label} returned 'not found'. Check the base URL and the model name. {detail}".strip())
            if status == 429:
                raise LLMError(f"{self.label} rate limit or quota reached. {detail}".strip())
            raise LLMError(f"{self.label} returned an error ({status}). {detail}".strip())
        try:
            data = response.json()
        except ValueError:
            raise LLMError(f"{self.label} did not return JSON. Check the base URL ({base}).") from None
        if not isinstance(data, dict):
            raise LLMError(f"{self.label} returned an unexpected response.")
        return data

    def _openai_chat(self, system: str, text: str, images: List[Image], max_tokens: int) -> Reply:
        content: Any = text
        if images:
            content = [{"type": "text", "text": text}] + [
                {"type": "image_url", "image_url": {
                    "url": f"data:{media};base64," + base64.standard_b64encode(data).decode("ascii")}}
                for data, media in images
            ]
        data = self._openai_request("POST", "/chat/completions", {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
        })
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise LLMError(f"{self.label} returned no answer: {json.dumps(data)[:300]}") from None
        out = message.get("content") or ""
        if isinstance(out, list):  # some servers return content parts
            out = "".join(part.get("text", "") for part in out if isinstance(part, dict))
        usage = data.get("usage") or {}
        return Reply(str(out), int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0))

    def _openai_models(self) -> List[str]:
        data = self._openai_request("GET", "/models", timeout=20)
        items = data.get("data")
        if not isinstance(items, list):
            raise LLMError(f"{self.label} did not return a model list. Check the base URL.")
        names = [str(item.get("id", "")) for item in items if isinstance(item, dict)]
        # Gemini prefixes ids with "models/".
        return sorted({name[7:] if name.startswith("models/") else name for name in names if name})


def _short(exc: Exception) -> str:
    message = getattr(exc, "message", None) or str(exc)
    return str(message)[:400]


def _error_detail(response: requests.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:300].strip()
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, dict):
        error = data.get("error", data)
        if isinstance(error, dict):
            return str(error.get("message") or error)[:400]
        return str(error)[:400]
    return str(data)[:300]


def extract_json(text: str) -> Optional[dict]:
    """First JSON object in a model reply (tolerates code fences and chatter)."""
    start = text.find("{")
    while start != -1:
        depth, in_string, escape = 0, False, False
        for index in range(start, len(text)):
            ch = text[index]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
            elif ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(text[start:index + 1])
                    except ValueError:
                        break
                    if isinstance(value, dict):
                        return value
                    break
        start = text.find("{", start + 1)
    return None
