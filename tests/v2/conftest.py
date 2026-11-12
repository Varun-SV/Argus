"""Fixtures for Argus 2: a scripted model server, a small website, isolated settings."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from argus2.settings import Settings

SITE = {
    "/": """<!doctype html><title>Sign in</title><h1>Sign in</h1>
<form onsubmit="event.preventDefault();
  if (user.value==='ann' && pass.value==='secret') location.href='/welcome';
  else document.getElementById('msg').textContent='Invalid password';">
<label for=user>User name</label><input id=user name=user>
<label for=pass>Password</label><input id=pass name=pass type=password>
<button id=go>Sign in</button></form><p id=msg></p>
<button id=boom onclick="throw new Error('boom exploded')">Export</button>""",
    "/welcome": "<!doctype html><title>Home</title><h1>Welcome, ann</h1><a href='/'>Sign out</a>",
}


class _Quiet(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, body, kind="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def site():
    class Handler(_Quiet):
        def do_GET(self):
            page = SITE.get(self.path.split("?")[0])
            self.reply(200 if page else 404, (page or "not found").encode(), "text/html; charset=utf-8")

    server = _serve(Handler)
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


class FakeModel:
    """Speaks both the OpenAI-compatible and the Anthropic wire formats."""

    def __init__(self):
        self.replies = []
        self.requests = []
        self.key = "test-key"
        self.url = ""

    def script(self, *replies):
        self.replies.extend(r if isinstance(r, str) else json.dumps(r) for r in replies)

    def next(self):
        return self.replies.pop(0) if self.replies else json.dumps(
            {"action": "done", "success": False, "why": "the script ran out of replies"})


@pytest.fixture
def model():
    fake = FakeModel()

    class Handler(_Quiet):
        def authorized(self):
            return (self.headers.get("Authorization") == f"Bearer {fake.key}"
                    or self.headers.get("x-api-key") == fake.key)

        def do_GET(self):
            if not self.authorized():
                return self.reply(401, {"error": {"message": "bad key"}})
            if self.path.startswith("/v1/models"):
                self.reply(200, {"data": [{"id": "fake-1", "type": "model", "display_name": "Fake 1",
                                           "created_at": "2026-01-01T00:00:00Z"},
                                          {"id": "models/fake-2", "type": "model", "display_name": "Fake 2",
                                           "created_at": "2026-01-01T00:00:00Z"}],
                                 "has_more": False, "first_id": "fake-1", "last_id": "models/fake-2"})
            else:
                self.reply(404, {"error": {"message": "no such route"}})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if not self.authorized():
                return self.reply(401, {"type": "error", "error": {"type": "authentication_error", "message": "bad key"}})
            fake.requests.append({"path": self.path, "body": body})
            if self.path == "/v1/chat/completions":
                self.reply(200, {"choices": [{"message": {"role": "assistant", "content": fake.next()}}],
                                 "usage": {"prompt_tokens": 100, "completion_tokens": 10}})
            elif self.path == "/v1/messages":
                self.reply(200, {"id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
                                 "content": [{"type": "text", "text": fake.next()}], "stop_reason": "end_turn",
                                 "stop_sequence": None, "usage": {"input_tokens": 100, "output_tokens": 10}})
            else:
                self.reply(404, {"error": {"message": "no such route"}})

    server = _serve(Handler)
    fake.url = f"http://127.0.0.1:{server.server_address[1]}"
    yield fake
    server.shutdown()


@pytest.fixture
def settings(tmp_path, model, monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "ARGUS_LLM_API_KEY",
                 "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    value = Settings(tmp_path / "home" / "settings.json")
    value.set_base_url("custom", model.url + "/v1")
    value.set_key("custom", model.key)
    value.update({"provider": "custom", "model": "fake-1", "vision": False})
    return value


@pytest.fixture
def workspace(tmp_path):
    path = tmp_path / "ws"
    path.mkdir()
    return path


@pytest.fixture(scope="session")
def browser_ok():
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch().close()
    except Exception as exc:
        pytest.skip(f"no browser available: {exc}")
