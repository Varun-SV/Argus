"""The local app server: a JSON API plus the web UI, bound to this computer only.

Access needs the per-launch token (handed over once in the opening link and
kept as a cookie), the ``Host`` header must be this server, and state-changing
requests from another origin are refused.
"""

from __future__ import annotations

import json
import mimetypes
import secrets
import threading
import time
import urllib.parse
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from argus2 import __version__
from argus2.engine import Engine, quick_spec
from argus2.llm import Client, LLMError
from argus2.settings import PROVIDERS, Settings, SettingsError
from argus2.spec import REFERENCE, SpecError, parse_spec, parse_target, placeholders
from argus2.store import StoreError, Workspace, browse, default_workspace
from argus2.targets import browser_status, install_browser

WEB_DIR = Path(__file__).parent / "web"
COOKIE = "argus2_token"


_SENT = object()  # the handler already wrote its own response


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class Job:
    """One piece of background work with an append-only event log."""

    def __init__(self, kind: str, title: str) -> None:
        self.id = secrets.token_hex(6)
        self.kind = kind
        self.title = title
        self.running = True
        self.events: List[Dict[str, Any]] = []
        self.stop = threading.Event()
        self.results: List[str] = []
        self._changed = threading.Condition()

    def emit(self, kind: str, /, **data: Any) -> None:
        with self._changed:
            self.events.append({"type": kind, "at": time.time(), **data})
            self._changed.notify_all()

    def wait(self, since: int, timeout: float) -> List[Dict[str, Any]]:
        with self._changed:
            if len(self.events) <= since and self.running:
                self._changed.wait(timeout)
            return self.events[since:]

    def view(self) -> Dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "title": self.title, "running": self.running,
                "results": list(self.results), "events": len(self.events)}


class App:
    def __init__(self, settings: Optional[Settings] = None, workspace: Optional[Path] = None,
                 client_factory: Optional[Callable[[Settings], Client]] = None) -> None:
        self.settings = settings or Settings()
        self.token = secrets.token_urlsafe(24)
        self.client_factory = client_factory  # tests inject a scripted model here
        self._lock = threading.Lock()
        self.job: Optional[Job] = None
        chosen = workspace or (Path(self.settings.data["workspace"]) if self.settings.data["workspace"] else None)
        try:
            self.workspace = Workspace(chosen) if chosen else Workspace(default_workspace())
        except StoreError:
            self.workspace = Workspace(default_workspace())
        if str(self.workspace.root) != self.settings.data["workspace"]:
            self.settings.set_workspace(self.workspace.root)

    # -- helpers ---------------------------------------------------------
    def _engine(self, job: Optional[Job] = None) -> Engine:
        client = self.client_factory(self.settings) if self.client_factory else None
        return Engine(self.settings, self.workspace.root, job.emit if job else None,
                      job.stop if job else None, client)

    def _start(self, kind: str, title: str, work: Callable[[Job], None]) -> Dict[str, Any]:
        with self._lock:
            if self.job and self.job.running:
                raise ApiError("Something is already running. Stop it or wait for it to finish.", 409)
            job = self.job = Job(kind, title)
        job.emit("job_start", kind=kind, title=title)

        def runner() -> None:
            try:
                work(job)
            except Exception as exc:
                job.emit("log", text=f"Unexpected problem: {exc}")
            finally:
                with job._changed:
                    job.running = False
                    job.events.append({"type": "job_end", "at": time.time(), "results": list(job.results)})
                    job._changed.notify_all()

        threading.Thread(target=runner, daemon=True, name=f"argus-job-{job.id}").start()
        return job.view()

    # -- API -------------------------------------------------------------
    def state(self) -> Dict[str, Any]:
        return {"version": __version__, "workspace": str(self.workspace.root),
                "settings": self.settings.public(), "job": self.job.view() if self.job else None,
                "reference": REFERENCE}

    def save_settings(self, body: dict) -> Dict[str, Any]:
        self.settings.update(body)
        return self.settings.public()

    def save_provider(self, body: dict) -> Dict[str, Any]:
        provider = str(body.get("provider") or "")
        if "api_key" in body:
            self.settings.set_key(provider, str(body["api_key"] or ""))
        if "base_url" in body:
            self.settings.set_base_url(provider, str(body["base_url"] or ""))
        return self.settings.public()

    def test_provider(self, body: dict) -> Dict[str, Any]:
        """Really call the provider: list its models and, if asked, send one message."""
        provider = str(body.get("provider") or "")
        if provider not in PROVIDERS:
            raise ApiError("Unknown provider.")
        missing = self.settings.provider_ready(provider)
        if missing:
            return {"ok": False, "error": missing, "models": []}
        info = self.settings.resolve(provider)
        model = str(body.get("model") or "")
        try:
            client = Client(info, model)
            models = client.list_models()
            self.settings.remember_models(provider, models)
            reply = ""
            if model:
                reply = client.chat("You are a connection test.", "Reply with the single word: ready",
                                    max_tokens=1000).text.strip()[:80]
        except LLMError as exc:
            return {"ok": False, "error": str(exc), "models": []}
        return {"ok": True, "models": models, "reply": reply, "settings": self.settings.public()}

    def set_workspace(self, body: dict) -> Dict[str, Any]:
        if self.job and self.job.running:
            raise ApiError("Wait for the current run to finish before changing folders.", 409)
        path = str(body.get("path") or "").strip()
        if not path:
            raise ApiError("Choose a folder.")
        self.workspace = Workspace(Path(path))
        self.settings.set_workspace(self.workspace.root)
        return self.state()

    def validate(self, body: dict) -> Dict[str, Any]:
        try:
            spec = parse_spec(str(body.get("content") or ""))
        except SpecError as exc:
            return {"ok": False, "error": str(exc)}
        unfinished = placeholders(spec)
        return {"ok": not unfinished, "name": spec.name, "steps": [s.text for s in spec.steps],
                "needs_model": spec.needs_model,
                "error": ("Unfinished placeholders: " + ", ".join(unfinished)) if unfinished else ""}

    def save_test(self, body: dict) -> Dict[str, Any]:
        content = str(body.get("content") or "")
        spec = parse_spec(content)
        file = str(body.get("file") or "") or self.workspace.free_name(spec.name)
        self.workspace.write_test(file, content, overwrite=bool(body.get("overwrite", True)))
        return {"file": file, "tests": self.workspace.list_tests()}

    def draft(self, body: dict) -> Dict[str, Any]:
        description = str(body.get("description") or "").strip()
        if not description:
            raise ApiError("Describe the behaviour you want to test.")
        target = parse_target(body["target"]) if body.get("target") else None
        return self._engine().draft(description, target)

    def run(self, body: dict) -> Dict[str, Any]:
        """Run saved tests (``files``), or an unsaved one (``content`` or ``target`` + ``behaviour``)."""
        items: List[tuple] = []
        if body.get("files"):
            for file in body["files"]:
                items.append((file, self.workspace.read_test(file)))
        elif body.get("content"):
            items.append(("", str(body["content"])))
        elif body.get("target"):
            items.append(("", quick_spec(parse_target(body["target"]), str(body.get("behaviour") or ""))))
        else:
            raise ApiError("Nothing to run.")
        specs = [(file, parse_spec(content), content) for file, content in items]
        if any(spec.needs_model for _, spec, _ in specs) and not self.client_factory:
            missing = self.settings.model_ready()
            if missing:
                raise ApiError(missing, 412)
        title = specs[0][1].name if len(specs) == 1 else f"{len(specs)} tests"

        def work(job: Job) -> None:
            for file, spec, content in specs:
                if job.stop.is_set():
                    break
                out_dir = self.workspace.new_result_dir()
                result = self._engine(job).run_test(spec, out_dir, file=file, source=content)
                self.workspace.save_result(out_dir, result)
                job.results.append(out_dir.name)

        return self._start("test", title, work)

    def explore(self, body: dict) -> Dict[str, Any]:
        target = parse_target(body.get("target"))
        try:
            budget = int(body.get("budget", 25))
        except (TypeError, ValueError):
            raise ApiError("The number of actions must be a number.") from None
        if not 1 <= budget <= 300:
            raise ApiError("The number of actions must be between 1 and 300.")
        if not self.client_factory:
            missing = self.settings.model_ready()
            if missing:
                raise ApiError(missing, 412)
        goal = str(body.get("goal") or "")

        def work(job: Job) -> None:
            out_dir = self.workspace.new_result_dir()
            result = self._engine(job).explore(target, goal, budget, out_dir)
            self.workspace.save_result(out_dir, result)
            job.results.append(out_dir.name)

        return self._start("explore", f"Explore {target.get('url') or target.get('command') or 'command line'}", work)

    def stop(self) -> Dict[str, Any]:
        if self.job and self.job.running:
            self.job.stop.set()
            self.job.emit("log", text="Stopping after the current action…")
        return {"ok": True}

    def events(self, query: dict) -> Dict[str, Any]:
        job = self.job
        if job is None or (query.get("job") and query["job"] != job.id):
            return {"job": None, "events": [], "next": 0}
        try:
            since = max(0, int(query.get("since", 0)))
        except ValueError:
            since = 0
        events = job.wait(since, 20.0)
        return {"job": job.view(), "events": events, "next": since + len(events)}


# ---------------------------------------------------------------------------

def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "Argus"

        def log_message(self, *args) -> None:  # keep the terminal quiet
            pass

        # -- plumbing ------------------------------------------------------
        def _send(self, status: int, body: bytes, content_type: str, headers: Optional[dict] = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data: Any, status: int = 200) -> None:
            self._send(status, json.dumps(data).encode("utf-8"), "application/json")

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").lower()
            port = self.server.server_address[1]
            return host in (f"127.0.0.1:{port}", f"localhost:{port}")

        def _authorized(self) -> bool:
            jar = cookies.SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie") or "")
            except cookies.CookieError:
                return False
            value = jar.get(COOKIE)
            return bool(value) and secrets.compare_digest(value.value, app.token)

        def _origin_ok(self) -> bool:
            origin = self.headers.get("Origin")
            if not origin:
                return True
            port = self.server.server_address[1]
            return origin.lower() in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")

        def _body(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length > 5_000_000:
                raise ApiError("Request too large.", 413)
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            try:
                data = json.loads(raw)
            except ValueError:
                raise ApiError("The request was not valid JSON.") from None
            if not isinstance(data, dict):
                raise ApiError("The request was not valid JSON.")
            return data

        # -- routing -------------------------------------------------------
        def do_GET(self) -> None:
            self._route("GET")

        def do_POST(self) -> None:
            self._route("POST")

        def do_DELETE(self) -> None:
            self._route("DELETE")

        def _route(self, method: str) -> None:
            url = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(url.path)
            query = dict(urllib.parse.parse_qsl(url.query))
            try:
                if not self._host_ok():
                    raise ApiError("Bad host.", 403)
                if method == "GET" and path == "/" and query.get("t"):
                    if not secrets.compare_digest(query["t"], app.token):
                        raise ApiError("This link has expired. Start Argus again.", 403)
                    self._send(303, b"", "text/plain", {
                        "Location": "/", "Set-Cookie": f"{COOKIE}={app.token}; HttpOnly; SameSite=Strict; Path=/"})
                    return
                if not self._authorized():
                    if path.startswith("/api/"):
                        raise ApiError("Not signed in to this Argus session.", 401)
                    self._send(401, b"<h1>Argus</h1><p>Open Argus using the link it printed when it started.</p>",
                               "text/html; charset=utf-8")
                    return
                if method != "GET" and not self._origin_ok():
                    raise ApiError("Request from another site refused.", 403)
                if path.startswith("/api/"):
                    answer = self._api(method, path[5:], query)
                    if answer is not _SENT:
                        self._json(answer)
                elif method == "GET":
                    self._static(path)
                else:
                    raise ApiError("Not found.", 404)
            except ApiError as exc:
                self._json({"error": str(exc)}, exc.status)
            except (SettingsError, SpecError, StoreError, LLMError) as exc:
                self._json({"error": str(exc)}, 400)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:
                self._json({"error": f"Unexpected problem: {exc}"}, 500)

        def _static(self, path: str) -> None:
            name = "index.html" if path == "/" else path.lstrip("/")
            file = (WEB_DIR / name).resolve()
            if WEB_DIR.resolve() not in file.parents or not file.is_file():
                raise ApiError("Not found.", 404)
            kind = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
            if kind.startswith("text/") or kind == "application/javascript":
                kind += "; charset=utf-8"
            self._send(200, file.read_bytes(), kind)

        def _api(self, method: str, path: str, query: dict) -> Any:
            parts = path.strip("/").split("/")
            head = parts[0]
            get, post, delete = method == "GET", method == "POST", method == "DELETE"
            ws = app.workspace
            if head == "state" and get:
                return app.state()
            if head == "events" and get:
                return app.events(query)
            if head == "settings" and post:
                return app.save_settings(self._body())
            if head == "provider" and post and parts[1:] == ["save"]:
                return app.save_provider(self._body())
            if head == "provider" and post and parts[1:] == ["test"]:
                return app.test_provider(self._body())
            if head == "browser" and get:
                return browser_status()
            if head == "browser" and post and parts[1:] == ["install"]:
                ok, detail = install_browser()
                return {"ok": ok, "detail": detail, "status": browser_status()}
            if head == "workspace" and post:
                return app.set_workspace(self._body())
            if head == "browse" and get:
                return browse(query.get("path"))
            if head == "tests":
                if get and len(parts) == 1:
                    return ws.list_tests()
                if post and parts[1:] == ["validate"]:
                    return app.validate(self._body())
                if post and parts[1:] == ["quick"]:
                    body = self._body()
                    return {"content": quick_spec(parse_target(body.get("target")), str(body.get("behaviour") or ""))}
                if post and parts[1:] == ["samples"]:
                    ws.add_samples()
                    return ws.list_tests()
                if post and len(parts) == 1:
                    return app.save_test(self._body())
                if len(parts) == 2 and get:
                    return {"file": parts[1], "content": ws.read_test(parts[1])}
                if len(parts) == 2 and delete:
                    ws.delete_test(parts[1])
                    return ws.list_tests()
            if head == "draft" and post:
                return app.draft(self._body())
            if head == "run" and post:
                return app.run(self._body())
            if head == "explore" and post:
                return app.explore(self._body())
            if head == "stop" and post:
                return app.stop()
            if head == "results":
                if get and len(parts) == 1:
                    return ws.list_results()
                if get and len(parts) == 2:
                    return ws.get_result(parts[1])
                if delete and len(parts) == 2:
                    ws.delete_result(parts[1])
                    return ws.list_results()
                if get and len(parts) == 4 and parts[2] == "file":
                    file = ws.artifact(parts[1], parts[3])
                    kind = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
                    if kind.startswith("text/"):
                        kind = "text/plain; charset=utf-8"
                    self._send(200, file.read_bytes(), kind)
                    return _SENT
            raise ApiError("Not found.", 404)

    return Handler


def make_server(app: App, port: int = 0) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    server.daemon_threads = True
    return server


def app_url(app: App, server: ThreadingHTTPServer) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}/?t={app.token}"
