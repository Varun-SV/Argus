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
import hashlib
import hmac
import json
import os
import stat
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
    normalize_guest_relative_path,
    sha256_file,
    validate_session_id,
    workspace_path,
)
from argus.capsule.safe_open import open_workspace_regular_file, stabilize_snapshot_read


def _consume_control_token(*, token_file: str = "", token_env: str = "") -> str:
    """Load the guest control-plane token and remove bootstrap material."""
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
        self._collection_snapshots: dict[str, dict] = {}

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
            self._clear_collection_snapshots()
            return {"workspace": str(resolved)}

    def _workspace(self) -> Path:
        if self.workspace_root is None:
            raise AdapterError("guest file workspace is not initialized")
        return self.workspace_root

    def _discard_collection_snapshot(self, relative: str) -> None:
        self._collection_snapshots.pop(relative, None)

    def _clear_collection_snapshots(self) -> None:
        self._collection_snapshots.clear()

    def stage_begin(self, relative: str, size: int, sha256: str) -> None:
        relative = normalize_guest_relative_path(relative)
        size = int(size)
        digest = str(sha256 or "").strip().lower()
        if size < 0 or size > TRANSFER_MAX_FILE_BYTES:
            raise AdapterError(f"invalid staged file size for {relative}: {size}")
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise AdapterError(f"invalid staged file checksum for {relative}")
        with self._lock:
            if self.adapter is not None:
                raise AdapterError("files cannot be staged after target launch")
            if relative in self._uploads:
                raise AdapterError(f"upload already in progress for {relative}")
            pending_total = sum(int(upload["size"]) for upload in self._uploads.values())
            if self._staged_total + pending_total + size > TRANSFER_MAX_TOTAL_BYTES:
                raise AdapterError("staged files exceed the Capsule session byte limit")
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
        relative = normalize_guest_relative_path(relative)
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

    def _discard_upload(self, relative: str, upload: dict) -> None:
        try:
            upload["temp"].unlink()
        except OSError:
            pass
        self._uploads.pop(relative, None)

    def stage_commit(self, relative: str, size: int, sha256: str) -> dict:
        relative = normalize_guest_relative_path(relative)
        with self._lock:
            upload = self._uploads.get(relative)
            if upload is None:
                raise AdapterError(f"no upload is active for {relative}")
            expected_size = upload["size"]
            expected_hash = upload["sha256"]
            if int(size) != expected_size or str(sha256 or "").lower() != expected_hash:
                self._discard_upload(relative, upload)
                raise AdapterError(f"staging manifest changed during upload for {relative}")
            if upload["received"] != expected_size:
                raise AdapterError(
                    f"staged file is incomplete for {relative}: "
                    f"{upload['received']} of {expected_size} bytes"
                )
            actual = sha256_file(upload["temp"])
            if actual != expected_hash:
                self._discard_upload(relative, upload)
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

    @staticmethod
    def _read_exact_digest(source, size: int, *, capture: bool) -> tuple[bytearray | None, str]:
        digest = hashlib.sha256()
        snapshot = bytearray() if capture else None
        remaining = int(size)
        while remaining:
            chunk = source.read(min(1024 * 1024, remaining))
            if not chunk:
                raise AdapterError("artifact changed while being snapshotted")
            digest.update(chunk)
            if snapshot is not None:
                snapshot.extend(chunk)
            remaining -= len(chunk)
        if source.read(1):
            raise AdapterError("artifact changed while being snapshotted")
        return snapshot, digest.hexdigest()

    def collect_info(self, relative: str) -> dict:
        relative = normalize_guest_relative_path(relative)
        with self._lock:
            existing = self._collection_snapshots.get(relative)
            if existing is not None:
                return {"size": existing["size"], "sha256": existing["sha256"]}

            with open_workspace_regular_file(self._workspace(), relative) as source:
                with stabilize_snapshot_read(source, relative):
                    before = os.fstat(source.fileno())
                    if before.st_size < 0 or before.st_size > TRANSFER_MAX_FILE_BYTES:
                        raise AdapterError(
                            f"artifact exceeds {TRANSFER_MAX_FILE_BYTES} byte limit: {relative}"
                        )

                    snapshot_total = sum(
                        int(snapshot["size"])
                        for snapshot in self._collection_snapshots.values()
                    )
                    if snapshot_total + before.st_size > TRANSFER_MAX_TOTAL_BYTES:
                        raise AdapterError(
                            f"artifact snapshots exceed the {TRANSFER_MAX_TOTAL_BYTES} byte session limit"
                        )

                    source.seek(0)
                    try:
                        snapshot, first_digest = self._read_exact_digest(
                            source,
                            int(before.st_size),
                            capture=True,
                        )
                        source.seek(0)
                        _discarded, second_digest = self._read_exact_digest(
                            source,
                            int(before.st_size),
                            capture=False,
                        )
                    except AdapterError as exc:
                        raise AdapterError(
                            f"artifact changed while being snapshotted: {relative}"
                        ) from exc

                    after = os.fstat(source.fileno())
                    if (
                        first_digest != second_digest
                        or before.st_size != after.st_size
                        or before.st_mtime_ns != after.st_mtime_ns
                        or getattr(before, "st_ctime_ns", 0)
                        != getattr(after, "st_ctime_ns", 0)
                        or snapshot is None
                        or len(snapshot) != before.st_size
                    ):
                        raise AdapterError(
                            f"artifact changed while being snapshotted: {relative}"
                        )

            self._collection_snapshots[relative] = {
                "data": snapshot,
                "size": len(snapshot),
                "sha256": first_digest,
            }
            return {"size": len(snapshot), "sha256": first_digest}

    def collect_chunk(self, relative: str, offset: int, limit: int) -> bytes:
        relative = normalize_guest_relative_path(relative)
        offset = int(offset)
        limit = int(limit)
        if offset < 0 or limit <= 0 or limit > TRANSFER_CHUNK_BYTES:
            raise AdapterError("invalid artifact chunk range")
        with self._lock:
            snapshot = self._collection_snapshots.get(relative)
            if snapshot is None:
                raise AdapterError(f"artifact must be preflighted before collection: {relative}")
            size = int(snapshot["size"])
            if offset + limit > size:
                raise AdapterError(f"artifact chunk exceeds snapshot bounds: {relative}")
            data = snapshot["data"][offset:offset + limit]
            if len(data) != limit:
                raise AdapterError(f"artifact snapshot changed while being collected: {relative}")
            return bytes(data)

    def _authorize_literal_target(self, target: str) -> None:
        """Grant only owner-execute to an existing staged POSIX launch target.

        The transfer protocol intentionally does not copy arbitrary host mode
        bits into the guest. A literal launch is the authenticated authorization
        boundary for real ``stage://`` targets, so POSIX guests add only ``u+x``
        after proving an existing file is contained by the bound workspace.

        Literal launch is also a generic adapter API used by tests/integrations
        with platform-native target strings. If the target does not exist on this
        host, leave it untouched and let the selected adapter validate it. An
        existing file, however, must satisfy the workspace boundary before Argus
        changes any permission bit.
        """
        if os.name == "nt":
            return
        root = self._workspace().resolve(strict=True)
        candidate = Path(target)
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            return
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise AdapterError("literal staged launch target escapes the Capsule workspace") from exc
        if not resolved.is_file():
            raise AdapterError("literal staged launch target must be a regular file")
        try:
            mode = stat.S_IMODE(resolved.stat().st_mode)
            os.chmod(resolved, mode | stat.S_IXUSR)
            if not (stat.S_IMODE(resolved.stat().st_mode) & stat.S_IXUSR):
                raise AdapterError("could not authorize staged launch target for execution")
        except OSError as exc:
            raise AdapterError(
                f"could not authorize staged launch target for execution: {exc}"
            ) from exc

    def start(
        self,
        adapter_type: str,
        target: str,
        input_mode: str,
        *,
        literal_target: bool = False,
    ) -> dict:
        input_mode = (input_mode or "safe").lower().strip()
        if input_mode not in {"safe", "semantic", "physical", "legacy"}:
            raise AdapterError("invalid guest input_mode")
        with self._lock:
            self.close()
            if literal_target:
                self._authorize_literal_target(target)
            previous_input = os.environ.get("ARGUS_INPUT_MODE")
            previous_cwd = Path.cwd()
            try:
                os.environ["ARGUS_INPUT_MODE"] = input_mode
                adapter = create_platform_adapter(adapter_type)
                if self.workspace_root is not None:
                    setter = getattr(adapter, "set_working_directory", None)
                    if callable(setter):
                        setter(str(self.workspace_root))
                    os.chdir(self.workspace_root)
                if literal_target:
                    launch_literal = getattr(adapter, "launch_literal", None)
                    if not callable(launch_literal):
                        raise AdapterError(
                            f"adapter '{adapter_type}' does not support literal staged launches"
                        )
                    launch_literal(target)
                else:
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
            try:
                if adapter is not None:
                    adapter.close()
            finally:
                self._clear_collection_snapshots()


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
                literal_target = body.get("literal_target", False)
                if not isinstance(literal_target, bool):
                    raise AdapterError("literal_target must be a JSON boolean")
                if not adapter_type or not target:
                    raise AdapterError("adapter_type and target are required")
                capabilities = self.server.state.start(
                    adapter_type,
                    target,
                    str(body.get("input_mode") or "safe"),
                    literal_target=literal_target,
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

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
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
