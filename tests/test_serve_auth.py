"""SEC-1: authentication and hardening of ``argus serve``.

Covers the access token, cookie and Bearer authentication, the ``Origin`` check,
the ``Host`` (DNS-rebinding) check, the CLI loopback guard and ``--debug`` meaning.
"""

from __future__ import annotations

import re
import secrets
import socket
from types import SimpleNamespace
from typing import ClassVar

import pytest

flask = pytest.importorskip("flask")

from click.testing import CliRunner

import argus.cli as cli_module
import argus.serve.app as app_module
from argus.serve.app import create_app

PORT = 5000
BASE = f"http://127.0.0.1:{PORT}"
ORIGIN = BASE


class _FakeThread:
    started: ClassVar[list] = []

    def __init__(self, target=None, daemon=None, **_kw):
        self.target = target

    def start(self):
        _FakeThread.started.append(self.target)


@pytest.fixture
def fake_thread(monkeypatch):
    _FakeThread.started = []
    monkeypatch.setattr(app_module.threading, "Thread", _FakeThread)
    return _FakeThread


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / ".argus" / "roam").mkdir(parents=True)
    return SimpleNamespace(project_dir=tmp_path, argus_dir=tmp_path / ".argus")


@pytest.fixture
def token():
    return secrets.token_urlsafe(32)


def _app(cfg, token, **kw):
    kw.setdefault("host", "127.0.0.1")
    kw.setdefault("port", PORT)
    return create_app(cfg, token=token, **kw)


@pytest.fixture
def client(cfg, token):
    return _app(cfg, token).test_client()


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _login(client, token):
    return client.post("/login", base_url=BASE, data={"token": token},
                       headers={"Origin": ORIGIN})


def _set_cookies(resp):
    return resp.headers.getlist("Set-Cookie")


# ---------------------------------------------------------------- R1 ----


@pytest.mark.parametrize("path", [
    "/api/roam/status", "/api/runs", "/api/live", "/api/events", "/api/unknown",
])
def test_unauthenticated_api_get_is_401(client, path):
    resp = client.get(path, base_url=BASE)
    assert resp.status_code == 401
    assert resp.is_json
    assert resp.get_json()["ok"] is False


@pytest.mark.parametrize("path", ["/", "/roam"])
def test_unauthenticated_html_get_shows_login_not_dashboard(client, path):
    resp = client.get(path, base_url=BASE)
    assert resp.status_code in (200, 302, 303)
    body = resp.get_data(as_text=True)
    assert "Recent Runs" not in body
    assert "/api/roam/start" not in body
    if resp.status_code != 200:
        assert resp.headers["Location"].endswith("/login")
    else:
        assert 'name="token"' in body


def test_login_page_is_reachable_without_auth(client, token):
    resp = client.get("/login", base_url=BASE)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "<form" in body and 'name="token"' in body
    assert token not in body


def test_unauthenticated_start_never_starts_a_session(client, fake_thread):
    resp = client.post("/api/roam/start", base_url=BASE, json={"target": "notepad.exe"},
                       headers={"Origin": ORIGIN})
    assert resp.status_code == 401
    assert fake_thread.started == []


def test_unauthenticated_stop_does_not_touch_session(client, token):
    resp = client.post("/api/roam/stop", base_url=BASE, headers={"Origin": ORIGIN})
    assert resp.status_code == 401
    status = client.get("/api/roam/status", base_url=BASE, headers=_bearer(token))
    assert status.get_json()["stop"] is False


def test_unauthenticated_response_never_contains_token(client, token):
    for path in ("/", "/roam", "/login", "/api/roam/status"):
        assert token not in client.get(path, base_url=BASE).get_data(as_text=True)


# ---------------------------------------------------------------- R2 ----


def test_token_generated_per_app_when_omitted(cfg):
    a = create_app(cfg, host="127.0.0.1", port=PORT)
    b = create_app(cfg, host="127.0.0.1", port=PORT)
    ta = a.config["ARGUS_ACCESS_TOKEN"]
    tb = b.config["ARGUS_ACCESS_TOKEN"]
    assert len(ta) >= 43 and len(tb) >= 43
    assert ta != tb
    resp = a.test_client().get("/api/roam/status", base_url=BASE, headers=_bearer(ta))
    assert resp.status_code == 200


def test_create_app_rejects_weak_token(cfg):
    with pytest.raises(ValueError):
        create_app(cfg, token="short", host="127.0.0.1", port=PORT)


def test_token_comparison_uses_compare_digest(client, token, monkeypatch):
    calls = []
    real = app_module.hmac.compare_digest

    def spy(a, b):
        calls.append(1)
        return real(a, b)

    monkeypatch.setattr(app_module.hmac, "compare_digest", spy)
    resp = client.get("/api/roam/status", base_url=BASE, headers=_bearer(token))
    assert resp.status_code == 200
    assert calls


# ---------------------------------------------------------------- R3 ----


def test_login_sets_hardened_cookie_and_grants_access(client, token):
    resp = _login(client, token)
    assert resp.status_code in (302, 303)
    cookies = _set_cookies(resp)
    assert len(cookies) == 1
    cookie = cookies[0]
    lowered = cookie.lower()
    assert "httponly" in lowered
    assert "samesite=strict" in lowered
    assert "path=/" in lowered
    assert token not in cookie

    dash = client.get("/", base_url=BASE)
    assert dash.status_code == 200
    assert "Recent Runs" in dash.get_data(as_text=True)
    roam = client.get("/roam", base_url=BASE)
    assert roam.status_code == 200
    assert "/api/roam/start" in roam.get_data(as_text=True)
    status = client.get("/api/roam/status", base_url=BASE)
    assert status.status_code == 200


def test_login_accepts_json_body(client, token):
    resp = client.post("/login", base_url=BASE, json={"token": token},
                       headers={"Origin": ORIGIN})
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}
    assert _set_cookies(resp)
    assert client.get("/api/roam/status", base_url=BASE).status_code == 200


def test_login_with_wrong_token_is_rejected(client, token):
    resp = _login(client, token + "x")
    assert resp.status_code == 401
    assert _set_cookies(resp) == []
    assert client.get("/api/roam/status", base_url=BASE).status_code == 401


def test_login_never_reads_token_from_query_string(client, token):
    resp = client.post(f"/login?token={token}", base_url=BASE, headers={"Origin": ORIGIN})
    assert resp.status_code == 401
    assert _set_cookies(resp) == []
    assert client.get("/api/roam/status", base_url=BASE).status_code == 401


@pytest.mark.parametrize("param", ["token", "access_token", "auth", "bearer"])
def test_token_in_query_string_is_never_accepted(client, token, param):
    resp = client.get(f"/api/roam/status?{param}={token}", base_url=BASE)
    assert resp.status_code == 401
    resp = client.get(f"/?{param}={token}", base_url=BASE)
    assert "Recent Runs" not in resp.get_data(as_text=True)


def test_bearer_header_authenticates_scripts(client, token):
    resp = client.get("/api/roam/status", base_url=BASE, headers=_bearer(token))
    assert resp.status_code == 200
    assert set(resp.get_json()) >= {"running", "log", "report", "findings", "stop"}


@pytest.mark.parametrize("header", [
    "Bearer wrong", "Bearer", "Basic abc", "bearer ", "Token {t}", "{t}",
])
def test_bad_authorization_header_is_401(client, token, header):
    resp = client.get("/api/roam/status", base_url=BASE,
                      headers={"Authorization": header.format(t=token)})
    assert resp.status_code == 401


def test_forged_cookie_is_rejected(client, token):
    client.set_cookie(f"argus_session_{PORT}", token, domain="127.0.0.1")
    assert client.get("/api/roam/status", base_url=BASE).status_code == 401
    client.set_cookie(f"argus_session_{PORT}", "0" * 64, domain="127.0.0.1")
    assert client.get("/api/roam/status", base_url=BASE).status_code == 401


def test_cookie_from_a_previous_launch_is_rejected(cfg, token):
    first = _app(cfg, token).test_client()
    resp = _login(first, token)
    cookie_value = _set_cookies(resp)[0].split(";", 1)[0].split("=", 1)[1]
    second = _app(cfg, secrets.token_urlsafe(32)).test_client()
    second.set_cookie(f"argus_session_{PORT}", cookie_value, domain="127.0.0.1")
    assert second.get("/api/roam/status", base_url=BASE).status_code == 401


# ---------------------------------------------------------------- R4 ----


@pytest.mark.parametrize("origin", [
    "http://evil.example", "null", "http://127.0.0.1:5001", "https://127.0.0.1:5000",
    "http://localhost:5000",
])
def test_cookie_post_with_foreign_origin_is_403(client, token, fake_thread, origin):
    _login(client, token)
    resp = client.post("/api/roam/start", base_url=BASE, json={"target": "x"},
                       headers={"Origin": origin})
    assert resp.status_code == 403
    assert fake_thread.started == []


def test_bearer_post_with_foreign_origin_is_403(client, token, fake_thread):
    headers = {**_bearer(token), "Origin": "http://evil.example"}
    resp = client.post("/api/roam/start", base_url=BASE, json={"target": "x"},
                       headers=headers)
    assert resp.status_code == 403
    assert fake_thread.started == []


def test_cookie_post_without_origin_is_403(client, token, fake_thread):
    _login(client, token)
    resp = client.post("/api/roam/start", base_url=BASE, json={"target": "x"})
    assert resp.status_code == 403
    resp = client.post("/api/roam/stop", base_url=BASE)
    assert resp.status_code == 403
    assert fake_thread.started == []


def test_bearer_post_without_origin_is_accepted(client, token, fake_thread):
    resp = client.post("/api/roam/start", base_url=BASE, json={"target": "x"},
                       headers=_bearer(token))
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}
    assert len(fake_thread.started) == 1


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_other_state_changing_methods_need_origin(client, token, method):
    _login(client, token)
    resp = getattr(client, method)("/api/roam/stop", base_url=BASE,
                                   headers={"Origin": "http://evil.example"})
    assert resp.status_code == 403


@pytest.mark.parametrize("origin", [None, "http://evil.example", "null"])
def test_login_post_enforces_origin(client, token, origin):
    headers = {} if origin is None else {"Origin": origin}
    resp = client.post("/login", base_url=BASE, data={"token": token}, headers=headers)
    assert resp.status_code == 403
    assert _set_cookies(resp) == []


def test_same_origin_post_on_localhost_alias(client, token, fake_thread):
    base = f"http://localhost:{PORT}"
    resp = client.post("/login", base_url=base, data={"token": token},
                       headers={"Origin": base})
    assert resp.status_code in (302, 303)
    resp = client.post("/api/roam/start", base_url=base, json={"target": "x"},
                       headers={"Origin": base})
    assert resp.status_code == 200


# ---------------------------------------------------------------- R5 ----


@pytest.mark.parametrize("base", [
    "http://evil.example:5000", "http://127.0.0.1:5001", "http://evil.example",
    "http://localhost.evil.example:5000",
])
def test_foreign_host_header_is_403_before_anything(client, token, base):
    assert client.get("/api/roam/status", base_url=base,
                      headers=_bearer(token)).status_code == 403
    assert client.get("/login", base_url=base).status_code == 403
    resp = client.post("/login", base_url=base, data={"token": token},
                       headers={"Origin": base})
    assert resp.status_code == 403
    assert _set_cookies(resp) == []


@pytest.mark.parametrize("base", [
    "http://127.0.0.1:5000", "http://localhost:5000", "http://[::1]:5000",
    "http://LOCALHOST:5000",
])
def test_loopback_host_headers_are_allowed(client, token, base):
    resp = client.get("/api/roam/status", base_url=base, headers=_bearer(token))
    assert resp.status_code == 200


def test_exact_bound_host_is_allowed(cfg, token):
    app = _app(cfg, token, host="127.0.0.2")
    resp = app.test_client().get("/api/roam/status", base_url="http://127.0.0.2:5000",
                                 headers=_bearer(token))
    assert resp.status_code == 200


def test_allow_remote_relaxes_only_host_check(cfg, token, fake_thread):
    app = _app(cfg, token, host="0.0.0.0", allow_remote=True)
    c = app.test_client()
    base = "http://dash.example:5000"
    assert c.get("/api/roam/status", base_url=base).status_code == 401
    assert c.get("/api/roam/status", base_url=base,
                 headers=_bearer(token)).status_code == 200
    resp = c.post("/api/roam/start", base_url=base, json={"target": "x"},
                  headers={**_bearer(token), "Origin": "http://evil.example"})
    assert resp.status_code == 403
    assert c.get(f"/api/roam/status?token={token}", base_url=base).status_code == 401
    assert fake_thread.started == []


# ---------------------------------------------------------------- R7 ----


def test_app_is_never_in_flask_debug_mode(cfg, token):
    app = _app(cfg, token, verbose=True)
    assert app.debug is False
    assert app.config["DEBUG"] is False


# ---------------------------------------------------------------- R8 ----


def test_authenticated_roam_api_shapes_unchanged(client, token, fake_thread):
    _login(client, token)
    h = {"Origin": ORIGIN}
    assert client.post("/api/roam/start", base_url=BASE, json={"target": ""},
                       headers=h).get_json() == {"ok": False, "error": "target is required"}
    assert client.post("/api/roam/start", base_url=BASE, json={"target": "notepad.exe"},
                       headers=h).get_json() == {"ok": True}
    assert client.post("/api/roam/start", base_url=BASE, json={"target": "notepad.exe"},
                       headers=h).get_json() == {
        "ok": False, "error": "a session is already running"}
    assert client.post("/api/roam/stop", base_url=BASE, headers=h).get_json() == {"ok": True}
    status = client.get("/api/roam/status", base_url=BASE).get_json()
    assert status == {"running": True, "log": [], "report": None, "findings": 0,
                      "stop": True}
    assert len(fake_thread.started) == 1
    assert client.get("/api/runs", base_url=BASE).get_json() == []
    assert client.get("/api/live", base_url=BASE).status_code == 404


def test_roam_page_fetches_stay_same_origin(client, token):
    _login(client, token)
    body = client.get("/roam", base_url=BASE).get_data(as_text=True)
    urls = re.findall(r'fetch\("([^"]+)"', body)
    assert urls and all(u.startswith("/api/") for u in urls)
    assert token not in body


# ---------------------------------------------------------------- CLI ----


@pytest.fixture
def cli_env(monkeypatch, cfg):
    runs = []

    def fake_run(self, *args, **kwargs):
        runs.append({"app": self, "args": args, "kwargs": kwargs})

    monkeypatch.setattr(flask.Flask, "run", fake_run)
    monkeypatch.setattr(cli_module, "load_config", lambda *a, **k: cfg)
    return runs


def _serve(*args):
    return CliRunner().invoke(cli_module.main, ["serve", *args])


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "::", "example.com",
                                  "10.0.0.1"])
def test_cli_refuses_non_loopback_without_allow_remote(cli_env, host):
    result = _serve("--host", host)
    assert result.exit_code == 2
    assert "--allow-remote" in result.output
    assert cli_env == []


def test_cli_refuses_localhost_that_resolves_off_loopback(cli_env, monkeypatch):
    def fake_getaddrinfo(*_a, **_k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.9", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    result = _serve("--host", "localhost")
    assert result.exit_code == 2
    assert "--allow-remote" in result.output
    assert cli_env == []


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "localhost"])
def test_cli_accepts_loopback_hosts(cli_env, monkeypatch, host):
    def fake_getaddrinfo(*_a, **_k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0)),
                (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 0, 0, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    result = _serve("--host", host)
    assert result.exit_code == 0, result.output
    assert len(cli_env) == 1


def test_cli_allow_remote_permits_non_loopback(cli_env):
    result = _serve("--host", "0.0.0.0", "--allow-remote")
    assert result.exit_code == 0, result.output
    assert len(cli_env) == 1
    app = cli_env[0]["app"]
    tok = app.config["ARGUS_ACCESS_TOKEN"]
    resp = app.test_client().get("/api/roam/status", base_url="http://dash.example:5000",
                                 headers=_bearer(tok))
    assert resp.status_code == 200


def test_cli_prints_token_once_and_never_in_url(cli_env):
    result = _serve()
    assert result.exit_code == 0, result.output
    tok = cli_env[0]["app"].config["ARGUS_ACCESS_TOKEN"]
    assert result.output.count(tok) == 1
    for line in result.output.splitlines():
        if "http" in line:
            assert tok not in line
    assert "/login" in result.output


def test_cli_token_differs_per_launch(cli_env):
    assert _serve().exit_code == 0
    assert _serve().exit_code == 0
    tokens = [r["app"].config["ARGUS_ACCESS_TOKEN"] for r in cli_env]
    assert tokens[0] != tokens[1]


def test_cli_binds_requested_host_and_port(cli_env):
    assert _serve("--host", "127.0.0.1", "--port", "8123").exit_code == 0
    kw = cli_env[0]["kwargs"]
    assert kw.get("host") == "127.0.0.1"
    assert kw.get("port") == 8123


def test_cli_debug_never_enables_flask_debugger_or_reloader(cli_env, monkeypatch):
    monkeypatch.setenv("FLASK_DEBUG", "1")
    result = _serve("--debug")
    assert result.exit_code == 0, result.output
    kw = cli_env[0]["kwargs"]
    assert kw.get("debug") is False
    assert kw.get("use_reloader") is False
    assert kw.get("use_debugger") is False
    assert cli_env[0]["app"].debug is False
