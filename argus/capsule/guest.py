"""Host-side client/proxy for the Argus guest agent."""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

from argus.adapters.base import Adapter, Observation, UIElement
from argus.capsule.base import CapsuleError


class CapsuleGuestError(CapsuleError):
    """Raised when the guest agent cannot satisfy a host request."""


def _observation_from_dict(data: dict) -> Observation:
    elements = [
        UIElement(
            element_id=int(item["element_id"]),
            control_type=str(item.get("control_type", "")),
            name=str(item.get("name", "")),
            rect=tuple(item.get("rect") or (0, 0, 0, 0)),
            enabled=bool(item.get("enabled", True)),
            depth=int(item.get("depth", 0)),
        )
        for item in (data.get("elements") or [])
    ]
    screenshot = data.get("screenshot_png_b64")
    return Observation(
        window_title=str(data.get("window_title", "")),
        elements=elements,
        screenshot_png=base64.b64decode(screenshot) if screenshot else None,
        process_alive=bool(data.get("process_alive", True)),
        dialogs=[str(x) for x in (data.get("dialogs") or [])],
        error=data.get("error"),
        stdout=data.get("stdout"),
        stderr=data.get("stderr"),
        exit_code=data.get("exit_code"),
        url=data.get("url"),
        action_capabilities=data.get("action_capabilities"),
    )


class GuestAgentClient:
    """Small stdlib HTTP client used by the host-side Capsule environment."""

    def __init__(
        self,
        endpoint: str,
        token: str,
        *,
        timeout_seconds: float = 15.0,
        opener: Optional[Callable] = None,
    ) -> None:
        parsed = urllib.parse.urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise CapsuleGuestError(f"invalid guest agent endpoint: {endpoint!r}")
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.timeout_seconds = float(timeout_seconds)
        self._opener = opener or urllib.request.urlopen

    def _request(self, method: str, path: str, payload: Optional[dict] = None) -> dict:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            self.endpoint + path,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:
                detail = ""
            raise CapsuleGuestError(
                f"guest agent HTTP {exc.code}: {detail[:300] or exc.reason}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CapsuleGuestError(f"guest agent request failed: {exc}") from exc

        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CapsuleGuestError("guest agent returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise CapsuleGuestError("guest agent response must be a JSON object")
        if data.get("ok") is False:
            raise CapsuleGuestError(str(data.get("error") or "guest agent operation failed"))
        return data

    def health(self) -> dict:
        return self._request("GET", "/v1/health")

    def wait_until_ready(self, timeout_seconds: float) -> None:
        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        last_error = "guest agent not ready"
        while time.monotonic() < deadline:
            try:
                data = self.health()
                if data.get("ok", True):
                    return
            except CapsuleGuestError as exc:
                last_error = str(exc)
            time.sleep(0.5)
        raise CapsuleGuestError(
            f"guest agent did not become ready within {timeout_seconds:.0f}s: {last_error}"
        )

    def launch(self, adapter_type: str, target: str, input_mode: str) -> dict:
        return self._request(
            "POST",
            "/v1/session/start",
            {
                "adapter_type": adapter_type,
                "target": target,
                "input_mode": input_mode,
            },
        )

    def observe(self, include_screenshot: bool = True) -> Observation:
        query = "1" if include_screenshot else "0"
        data = self._request("GET", f"/v1/observe?include_screenshot={query}")
        return _observation_from_dict(data["observation"])

    def act(self, action: dict) -> str:
        data = self._request("POST", "/v1/act", {"action": action})
        return str(data.get("note", ""))

    def close_session(self) -> None:
        self._request("POST", "/v1/session/close", {})


class GuestAdapterProxy(Adapter):
    """Adapter-compatible proxy whose real implementation lives in the guest."""

    def __init__(self, client: GuestAgentClient, adapter_type: str, input_mode: str) -> None:
        self.client = client
        self.type_name = adapter_type
        self.input_mode = input_mode
        self._capabilities: Optional[dict] = None

    def launch(self, target: str) -> None:
        data = self.client.launch(self.type_name, target, self.input_mode)
        capabilities = data.get("capabilities")
        if not isinstance(capabilities, dict):
            raise CapsuleGuestError("guest agent did not return adapter capabilities")
        self._capabilities = capabilities

    def observe(self, include_screenshot: bool = True) -> Observation:
        obs = self.client.observe(include_screenshot=include_screenshot)
        obs.action_capabilities = self.capabilities()
        return obs

    def capabilities(self) -> dict:
        if self._capabilities is None:
            return {
                "actions": {"wait": {}, "done": {}},
                "notes": ["Guest target has not been launched yet."],
            }
        return self._capabilities

    def validate_action(self, action: dict) -> None:
        # Host-side PolicyAdapter enforces generic/global policy. The guest's
        # actual platform adapter performs platform-specific validation again.
        return None

    def act(self, action: dict) -> str:
        return self.client.act(action)

    def close(self) -> None:
        self.client.close_session()
        self._capabilities = None
