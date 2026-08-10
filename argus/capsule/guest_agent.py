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
import tempfile
import threading
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from argus.adapters.base import Adapter, AdapterError, Observation, create_adapter as create_platform_adapter
from argus.capsule.base import CapsuleError
from argus.capsule.files import (
    TRANSFER_CHUNK_BYTES,
    TRANSFER_MAX_FILE_BYTES,
    TRANSFER_MAX_TOTAL_BYTES,
    normalize_relative_path,
    sha256_file,
    validate_session_id,
    workspace_path,
)


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
        self.workspace_root: Optional[Path] = None
        self.workspace_session_id: str = ""
        self._uploads: dict[str, dict] = {}
        self._staged_total = 0

    # ---- per-session workspace -----------------------------------------

    def begin_files(self, session_id: str) -> dict:
        session_id = validate_session_id(session_id)
        with self._lock:
            if self.adapter is not None:
                raise AdapterError("file workspace must be initialized before target launch")
            if self.workspace_root is not None:
                if session_id != self.workspace_session_id:
                    raise AdapterError("guest file workspace is already bound to another session")
                return {"workspace": str(self.workspace_root)}

            base = Path(tempfile.gettempdir()) / "argus-capsule-workspaces"
            base.mkdir(parents=True, exist_ok=True)
            base_resolved = base.resolve(strict=True)
            root = base / session_id
            if root.is_symlink():
                raise AdapterError("guest workspace root cannot be a symlink")
            root.mkdir(parents=True, exist_ok=True)
            resolved = root.resolve(strict=True)
            try:
                resolved.relative_to(base_resolved)
            except ValueError as exc:
                raise AdapterError("guest workspace escaped the Argus workspace base") from exc
            self.workspace_root = resolved
            self.workspace_session_id = session_id
            self._uploads.clear()
            self._staged_total = 0
            return {"workspace": str(resolved)}

    def _workspace(self) -> Path:
        if self.workspace_root is None:
            raise AdapterError("guest file workspace is not initialized")
        return self.workspace_root

    def stage_begin(self, relative: str, size: int, sha256: str) -> None:
        relative = normalize_relative_path(relative)
        size = int(size)
        digest = str(sha256 or "").strip().lower()
        if size < 0 or size > TRANSFER_MAX_FILE_BYTES:
            raise AdapterError(f"invalid staged file size for {relative}: {size}")
        if self._staged_total + size > TRANSFER_MAX_TOTAL_BYTES:
            raise AdapterError("staged files exceed the Capsule session byte limit")
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise AdapterError(f"invalid staged file checksum for {relative}")
        with self._lock:
            if self.adapter is not None:
                raise AdapterError("files cannot be staged after target launch")
            if relative in self._uploads:
                raise AdapterError(f"upload already in progress for {relative}")
            root = self._workspace()
            destination = workspace_path(root, relative, must_exist=False)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination = workspace_path(root, relative, must_exist=False)
            if destination.exists():
                raise AdapterError(f"staging destination already exists: {relative}")
            temp = destination.with_name(
                f".{destination.name}.argus-{uuid.uuid4().hex}.part"
            )
            temp.touch(exist_ok=False)
            self._uploads[relative] = {
                "destination": destination,
                "temp": temp,
                "size": size,
                "sha256": digest,
                "received": 0,
            }

    def stage_chunk(self, relative: str, offset: int, data_b64: str) -> None:
        relative = normalize_relative_path(relative)
        try:
            chunk = base64.b64decode(str(data_b64 or ""), validate=True)
        except Exception as exc:
            raise AdapterError(f"invalid base64 upload chunk for {relative}") from exc
        if not chunk or len(chunk) > TRANSFER_CHUNK_BYTES:
            raise AdapterError(f"invalid upload chunk size for {relative}")
        with self._lock:
            upload = self._uploads.get(relative)
            if upload is None:
                raise AdapterError(f"no upload is active for {relative}")
            offset = int(offset)
            if offset != upload["received"]:
                raise AdapterError(
                    f"non-sequential upload offset for {relative}: "
                    f"expected {upload['received']}, got {offset}"
                )
            if offset + len(chunk) > upload["size"]:
                raise AdapterError(f"upload exceeds declared size for {relative}")
            with upload["temp"].open("ab") as handle:
                handle.write(chunk)
            upload["received"] += len(chunk)

    def stage_commit(self, relative: str, size: int, sha256: str) -> dict:
        relative = normalize_relative_path(relative)
        with self._lock:
            upload = self._uploads.get(relative)
            if upload is None:
                raise AdapterError(f"no upload is active for {relative}")
            expected_size = upload["size"]
            expected_hash = upload["sha256"]
            if int(size) != expected_size or str(sha256 or "").lower() != expected_hash:
                raise AdapterError(f"staging manifest changed during upload for {relative}")
            if upload["received"] != expected_size:
                raise AdapterError(
                    f"staged file is incomplete for {relative}: "
                    f"{upload['received']} of {expected_size} bytes"
                )
            actual = sha256_file(upload["temp"])
            if actual != expected_hash:
                try:
                    upload["temp"].unlink()
                except OSError:
                    pass
                self._uploads.pop(relative, None)
                raise AdapterError(
                    f"staged file checksum mismatch for {relative}: "
                    f"expected {expected_hash}, got {actual}"
                )
            os.replace(upload["temp"], upload["destination"])
            self._uploads.pop(relative, None)
            self._staged_total += expected_size
            return {
                "guest_path": str(upload["destination"]),
                "size": expected_size,
                "sha256": expected_hash,
            }

    def collect_info(self, relative: str) -> dict:
        relative = normalize_relative_path(relative)
        with self._lock:
            path = workspace_path(self._workspace(), relative, must_exist=True)
            if not path.is_file():
                raise AdapterError(f"requested artifact is not a regular file: {relative}")
            size = path.stat().st_size
            if size > TRANSFER_MAX_FILE_BYTES:
                raise AdapterError(
                    f"artifact exceeds {TRANSFER_MAX_FILE_BYTES} byte limit: {relative}"
                )
            return {"size": size, "sha256": sha256_file(path)}

    def collect_chunk(self, relative: str, offset: int, limit: int) -> bytes:
        relative = normalize_relative_path(relative)
        offset = int(offset)
        limit = int(limit)
        if offset < 0 or limit <= 0 or limit > TRANSFER_CHUNK_BYTES:
            raise AdapterError("invalid artifact chunk range")
        with self._lock:
            path = workspace_path(self._workspace(), relative, must_exist=True)
            if not path.is_file():
                raise AdapterError(f"requested artifact is not a regular file: {relative}")
            size = path.stat().st_size
            if size > TRANSFER_MAX_FILE_BYTES:
                raise AdapterError(f"artifact exceeds the per-file transfer limit: {relative}")
            if offset + limit > size:
                raise AdapterError(f"artifact chunk exceeds file bounds: {relative}")
            with path.open("rb") as handle:
                handle.seek(offset)
                data = handle.read(limit)
            if len(data) != limit:
                raise AdapterError(f"artifact changed while being collected: {relative}")
            return data

    # ---- target lifecycle ----------------------------------------------

    def start(self, adapter_type: str, target: str, input_mode: str) -> dict:
        input_mode = (input_mode or "safe").lower().strip()
        if input_mode not in {"safe", "semantic", "physical", "legacy"}:
            raise AdapterError("invalid guest input_mode")
        with self._lock:
            self.close()
            previous_input = os.environ.get("ARGUS_INPUT_MODE")
            previous_cwd = Path.cwd()
            try:
                os.environ["ARGUS_INPUT_MODE"] = input_mode
                adapter = create_platform_adapter(adapter_type)
                # A prepared workspace becomes the target's inherited working
                # directory. This lets declared relative outputs remain inside
                # the only guest tree that collection is allowed to read.
                if self.workspace_root is not None:
                    os.chdir(self.workspace_root)
                adapter.launch(target)
            except Exception:
                try:
                    if "adapter" in locals():
                        adapter.close()
                except Exception:
                    pass
                raise
            finally:
                os.chdir(previous_cwd)
                if previous_input is None:
                    os.environ.pop("ARGUS_INPUT_MODE", None)
                else:
                    os.environ["ARGUS_INPUT_MODE"] = previous_input
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

    @staticmethod
    def _query_int(query: dict, name: str) -> int:
        raw = (query.get(name) or [None])[0]
        if raw is None:
            raise AdapterError(f"missing query parameter: {name}")
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise AdapterError(f"invalid integer query parameter: {name}") from exc

    def _dispatch(self) -> None:
        if not self._authorized():
            self._send(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
            return
        parsed = urlparse(self.path)
        try:
            if self.command == "GET" and parsed.path == "/v1/health":
                self._send(HTTPStatus.OK, {"ok": True, "service": "argus-guest-agent"})
                return
            if self.command == "POST" and parsed.path == "/v1/files/begin":
                body = self._payload()
                data = self.server.state.begin_files(str(body.get("session_id") or ""))
                self._send(HTTPStatus.OK, {"ok": True, **data})
                return
            if self.command == "POST" and parsed.path == "/v1/files/stage/begin":
                body = self._payload()
                self.server.state.stage_begin(
                    str(body.get("path") or ""),
                    int(body.get("size", -1)),
                    str(body.get("sha256") or ""),
                )
                self._send(HTTPStatus.OK, {"ok": True})
                return
            if self.command == "POST" and parsed.path == "/v1/files/stage/chunk":
                body = self._payload()
                self.server.state.stage_chunk(
                    str(body.get("path") or ""),
                    int(body.get("offset", -1)),
                    str(body.get("data_b64") or ""),
                )
                self._send(HTTPStatus.OK, {"ok": True})
                return
            if self.command == "POST" and parsed.path == "/v1/files/stage/commit":
                body = self._payload()
                data = self.server.state.stage_commit(
                    str(body.get("path") or ""),
                    int(body.get("size", -1)),
                    str(body.get("sha256") or ""),
                )
                self._send(HTTPStatus.OK, {"ok": True, **data})
                return
            if self.command == "GET" and parsed.path == "/v1/files/collect/info":
                query = parse_qs(parsed.query)
                relative = (query.get("path") or [""])[0]
                data = self.server.state.collect_info(relative)
                self._send(HTTPStatus.OK, {"ok": True, **data})
                return
            if self.command == "GET" and parsed.path == "/v1/files/collect/chunk":
                query = parse_qs(parsed.query)
                relative = (query.get("path") or [""])[0]
                data = self.server.state.collect_chunk(
                    relative,
                    self._query_int(query, "offset"),
                    self._query_int(query, "limit"),
                )
                self._send(
                    HTTPStatus.OK,
                    {"ok": True, "data_b64": base64.b64encode(data).decode("ascii")},
                )
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
        except (AdapterError, CapsuleError, ValueError) as exc:
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
