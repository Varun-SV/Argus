"""Minimal authenticated Argus agent intended to run inside a Capsule guest.

The host never sends mouse/keyboard input to its own desktop. It sends Argus
adapter actions to this service; the service creates the normal platform adapter
*inside the guest* and therefore all physical/semantic input stays in the VM.

Golden images should start this module in the interactive test-user session.
For the MVP, prefer a one-time token file that exists in the golden image and is
consumed from the per-session differencing disk before the server starts::

    python -m argus.capsule.guest_agent --host 0.0.0.0 --port 8765 \
        --token-file C:\\ProgramData\\Argus\\guest-token.once

``--token-env`` remains available for process-scoped bootstrap environments, but
the variable is removed from ``os.environ`` immediately after startup. Do not
store the control-plane secret in the interactive test user's persistent
environment. A token is mandatory when binding to a non-loopback address.
"""

from __future__ import annotations

import argparse
import base64
import hmac
import json
import os
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from argus.adapters.base import Adapter, AdapterError, Observation, create_adapter as create_platform_adapter


def _consume_control_token(*, token_file: str = "", token_env: str = "") -> str:
    """Load the guest control-plane token and remove bootstrap material.

    The application under test is launched as a child of this process through a
    normal platform adapter. Any secret left in ``os.environ`` would therefore
    be inherited by that workload. Token bootstrap must be one-shot: a token
    file is deleted before the server starts, and environment-backed bootstrap
    values are popped immediately after reading.
    """
    token = ""
    path = Path(token_file).expanduser() if token_file else None
    if path is not None:
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise AdapterError(f"cannot read guest token file {path}: {exc}") from exc
        try:
            path.unlink()
        except OSError as exc:
            raise AdapterError(
                f"guest token file {path} could not be consumed/deleted safely: {exc}"
            ) from exc
    elif token_env:
        token = os.environ.pop(token_env, "").strip()

    # Always scrub the conventional name as well as the configured bootstrap
    # name. This is intentionally done even when token_file supplied the token.
    for name in {token_env, "ARGUS_CAPSULE_GUEST_TOKEN"}:
        if name:
            os.environ.pop(name, None)
    return token


def _observation_to_dict(obs: Observation) -> dict:
    return {
        "window_title": obs.window_title,
        "elements": [
            {
                "element_id": el.element_id,
                "control_type": el.control_type,
                "name": el.name,
                "rect": list(el.rect),
                "enabled": el.enabled,
                "depth": el.depth,
            }
            for el in obs.elements
        ],
        "screenshot_png_b64": (
            base64.b64encode(obs.screenshot_png).decode("ascii")
            if obs.screenshot_png
            else None
        ),
        "process_alive": obs.process_alive,
        "dialogs": list(obs.dialogs),
        "error": obs.error,
        "stdout": obs.stdout,
        "stderr": obs.stderr,
        "exit_code": obs.exit_code,
        "url": obs.url,
        "action_capabilities": obs.action_capabilities,
    }


class GuestAgentState:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.adapter: Optional[Adapter] = None

    def start(self, adapter_type: str, target: str, input_mode: str) -> dict:
        input_mode = (input_mode or "safe").lower().strip()
        if input_mode not in {"safe", "semantic", "physical", "legacy"}:
            raise AdapterError("invalid guest input_mode")
        with self._lock:
            self.close()
            previous = os.environ.get("ARGUS_INPUT_MODE")
            try:
                os.environ["ARGUS_INPUT_MODE"] = input_mode
                adapter = create_platform_adapter(adapter_type)
            finally:
                if previous is None:
                    os.environ.pop("ARGUS_INPUT_MODE", None)
                else:
                    os.environ["ARGUS_INPUT_MODE"] = previous
            try:
                adapter.launch(target)
            except Exception:
                try:
                    adapter.close()
                except Exception:
                    pass
                raise
            self.adapter = adapter
            return adapter.capabilities()

    def observe(self, include_screenshot: bool) -> Observation:
        with self._lock:
            if self.adapter is None:
                raise AdapterError("no guest target session is active")
            return self.adapter.observe(include_screenshot=include_screenshot)

    def act(self, action: dict) -> str:
        with self._lock:
            if self.adapter is None:
                raise AdapterError("no guest target session is active")
            # create_platform_adapter returns PolicyAdapter, so this traverses
            # the same PR1 safety boundary again inside the guest.
            return self.adapter.act(action)

    def close(self) -> None:
        with self._lock:
            adapter = self.adapter
            self.adapter = None
            if adapter is not None:
                adapter.close()


class GuestAgentServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, token: str, state: Optional[GuestAgentState] = None):
        self.token = token
        self.state = state or GuestAgentState()
        super().__init__(address, GuestAgentHandler)


class GuestAgentHandler(BaseHTTPRequestHandler):
    server: GuestAgentServer

    def log_message(self, format: str, *args) -> None:
        # Golden-image agents should be quiet by default; supervisors may still
        # capture stderr for unexpected crashes.
        return None

    def _authorized(self) -> bool:
        if not self.server.token:
            return True
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return False
        return hmac.compare_digest(header[len(prefix):], self.server.token)

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _payload(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        if length > 1024 * 1024:
            raise AdapterError("guest request body is too large")
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdapterError("request body must be valid JSON") from exc
        if not isinstance(value, dict):
            raise AdapterError("request body must be a JSON object")
        return value

    def _dispatch(self) -> None:
        if not self._authorized():
            self._send(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
            return
        parsed = urlparse(self.path)
        try:
            if self.command == "GET" and parsed.path == "/v1/health":
                self._send(HTTPStatus.OK, {"ok": True, "service": "argus-guest-agent"})
                return
            if self.command == "POST" and parsed.path == "/v1/session/start":
                body = self._payload()
                adapter_type = str(body.get("adapter_type") or "").strip()
                target = str(body.get("target") or "").strip()
                if not adapter_type or not target:
                    raise AdapterError("adapter_type and target are required")
                capabilities = self.server.state.start(
                    adapter_type,
                    target,
                    str(body.get("input_mode") or "safe"),
                )
                self._send(HTTPStatus.OK, {"ok": True, "capabilities": capabilities})
                return
            if self.command == "GET" and parsed.path == "/v1/observe":
                query = parse_qs(parsed.query)
                include = (query.get("include_screenshot") or ["1"])[0] not in {"0", "false"}
                obs = self.server.state.observe(include)
                self._send(
                    HTTPStatus.OK,
                    {"ok": True, "observation": _observation_to_dict(obs)},
                )
                return
            if self.command == "POST" and parsed.path == "/v1/act":
                body = self._payload()
                action = body.get("action")
                if not isinstance(action, dict):
                    raise AdapterError("action must be a JSON object")
                note = self.server.state.act(action)
                self._send(HTTPStatus.OK, {"ok": True, "note": note})
                return
            if self.command == "POST" and parsed.path == "/v1/session/close":
                self.server.state.close()
                self._send(HTTPStatus.OK, {"ok": True})
                return
            self._send(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
        except AdapterError as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
        except Exception as exc:
            self._send(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": f"guest operation failed: {exc}"},
            )

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._dispatch()

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._dispatch()


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Argus Capsule guest agent")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token-file", default="")
    parser.add_argument("--token-env", default="ARGUS_CAPSULE_GUEST_TOKEN")
    args = parser.parse_args(argv)

    try:
        token = _consume_control_token(
            token_file=args.token_file,
            token_env=args.token_env,
        )
    except AdapterError as exc:
        parser.error(str(exc))

    loopback_hosts = {"127.0.0.1", "localhost", "::1"}
    if args.host not in loopback_hosts and not token:
        parser.error("a guest token is required when binding outside loopback")
    if not (1 <= args.port <= 65535):
        parser.error("port must be between 1 and 65535")

    server = GuestAgentServer((args.host, args.port), token)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            server.state.close()
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
