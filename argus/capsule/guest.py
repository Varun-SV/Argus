"""Host-side client/proxy for the Argus guest agent."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Callable, Optional

from argus.adapters.base import Adapter, Observation, UIElement
from argus.capsule.base import CapsuleError
from argus.capsule.files import (
    TRANSFER_CHUNK_BYTES,
    TRANSFER_MAX_FILE_BYTES,
    normalize_relative_path,
    sha256_file,
    workspace_path,
)


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

    # ---- deterministic workspace file transfer -------------------------

    def begin_files(self, session_id: str) -> dict:
        return self._request("POST", "/v1/files/begin", {"session_id": session_id})

    def stage_file(
        self,
        source: Path,
        destination: str,
        *,
        expected_sha256: str = "",
    ) -> dict:
        destination = normalize_relative_path(destination)
        size = source.stat().st_size
        if size > TRANSFER_MAX_FILE_BYTES:
            raise CapsuleGuestError(
                f"staged file exceeds {TRANSFER_MAX_FILE_BYTES} byte limit: {destination}"
            )
        digest = sha256_file(source)
        expected = str(expected_sha256 or "").strip().lower()
        if expected and expected != digest:
            raise CapsuleGuestError(
                f"staging checksum mismatch before upload for {destination}: "
                f"expected {expected}, got {digest}"
            )
        self._request(
            "POST",
            "/v1/files/stage/begin",
            {"path": destination, "size": size, "sha256": digest},
        )
        offset = 0
        with source.open("rb") as handle:
            while True:
                chunk = handle.read(TRANSFER_CHUNK_BYTES)
                if not chunk:
                    break
                self._request(
                    "POST",
                    "/v1/files/stage/chunk",
                    {
                        "path": destination,
                        "offset": offset,
                        "data_b64": base64.b64encode(chunk).decode("ascii"),
                    },
                )
                offset += len(chunk)
        data = self._request(
            "POST",
            "/v1/files/stage/commit",
            {"path": destination, "size": size, "sha256": digest},
        )
        guest_path = str(data.get("guest_path") or "").strip()
        if not guest_path:
            raise CapsuleGuestError("guest did not return the committed staged path")
        return {
            "source": str(source),
            "destination": destination,
            "size": size,
            "sha256": digest,
            "guest_path": guest_path,
        }

    def collect_info(self, relative: str) -> dict:
        relative = normalize_relative_path(relative)
        query = urllib.parse.urlencode({"path": relative})
        data = self._request("GET", f"/v1/files/collect/info?{query}")
        size = int(data.get("size", -1))
        digest = str(data.get("sha256") or "").strip().lower()
        if size < 0 or size > TRANSFER_MAX_FILE_BYTES:
            raise CapsuleGuestError(f"guest artifact size is invalid for {relative}: {size}")
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise CapsuleGuestError(f"guest artifact checksum is invalid for {relative}")
        return {"path": relative, "size": size, "sha256": digest}

    def collect_file(self, relative: str, output_root: Path, *, info: Optional[dict] = None) -> dict:
        relative = normalize_relative_path(relative)
        metadata = dict(info or self.collect_info(relative))
        size = int(metadata["size"])
        expected = str(metadata["sha256"]).lower()

        output_root.mkdir(parents=True, exist_ok=True)
        destination = workspace_path(output_root, relative, must_exist=False)
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Resolve again after creating parents so a pre-existing symlink/junction
        # cannot become a host-write escape between validation and the write.
        destination = workspace_path(output_root, relative, must_exist=False)
        temp = destination.with_name(f".{destination.name}.argus-{uuid.uuid4().hex}.part")
        digest = hashlib.sha256()
        offset = 0
        try:
            with temp.open("xb") as handle:
                while offset < size:
                    limit = min(TRANSFER_CHUNK_BYTES, size - offset)
                    query = urllib.parse.urlencode(
                        {"path": relative, "offset": offset, "limit": limit}
                    )
                    data = self._request("GET", f"/v1/files/collect/chunk?{query}")
                    try:
                        chunk = base64.b64decode(str(data.get("data_b64") or ""), validate=True)
                    except Exception as exc:
                        raise CapsuleGuestError(
                            f"guest returned invalid artifact data for {relative}"
                        ) from exc
                    if len(chunk) != limit:
                        raise CapsuleGuestError(
                            f"guest artifact chunk length mismatch for {relative}: "
                            f"expected {limit}, got {len(chunk)}"
                        )
                    handle.write(chunk)
                    digest.update(chunk)
                    offset += len(chunk)
            actual = digest.hexdigest()
            if actual != expected:
                raise CapsuleGuestError(
                    f"artifact checksum mismatch for {relative}: expected {expected}, got {actual}"
                )
            os.replace(temp, destination)
        except Exception:
            try:
                temp.unlink()
            except OSError:
                pass
            raise
        return {
            "path": relative,
            "size": size,
            "sha256": expected,
            "host_path": str(destination),
        }

    # ---- target session -------------------------------------------------

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
