"""`argus serve` access control (spec §8.1, SEC-01…05)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import argus.cli as cli
import argus.serve.app as serve_app

TOKEN = "test-access-token-0123456789abcdef"
ORIGIN = "http://localhost"
COOKIE = "argus_access_default"


@pytest.fixture
def app(tmp_path: Path):
    pytest.importorskip("flask")
    project = tmp_path / "secret-project-dir"
    (project / ".argus").mkdir(parents=True)
    cfg = SimpleNamespace(project_dir=project, argus_dir=project / ".argus")
    return serve_app.create_app(cfg, access_token=TOKEN)


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def authed(client):
    resp = client.get(f"/?token={TOKEN}")
    assert resp.status_code == 303
    return client


@pytest.fixture
def no_launch(monkeypatch):
    """Fail loudly if a roam worker would launch anything."""
    import argus.adapters as adapters

    def _boom(*_a, **_k):
        raise AssertionError("roam worker must not start")

    monkeypatch.setattr(adapters, "create_adapter", _boom)


# ── SEC-01: access token ──────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["/", "/roam", "/api/runs", "/api/roam/status", "/api/live",
                                  "/api/events"])
def test_missing_token_is_401(client, path):
    resp = client.get(path)
    assert resp.status_code == 401


@pytest.mark.parametrize("path", ["/", "/api/runs", "/api/roam/status"])
def test_wrong_cookie_is_403(client, path):
    client.set_cookie(COOKIE, "not-the-token")
    assert client.get(path).status_code == 403


def test_wrong_token_link_is_403_and_sets_no_cookie(client):
    resp = client.get("/?token=nope")
    assert resp.status_code == 403
    assert "Set-Cookie" not in resp.headers
    assert client.get("/").status_code == 401


def test_roam_start_without_token_launches_nothing(client, no_launch):
    resp = client.post("/api/roam/start", json={"target": "calc.exe"},
                       headers={"Origin": ORIGIN})
    assert resp.status_code == 401
    client.set_cookie(COOKIE, "not-the-token")
    resp = client.post("/api/roam/start", json={"target": "calc.exe"},
                       headers={"Origin": ORIGIN})
    assert resp.status_code == 403


def test_token_link_sets_strict_httponly_cookie_and_strips_token(client):
    resp = client.get(f"/roam?token={TOKEN}&x=1")
    assert resp.status_code == 303
    assert resp.headers["Location"] == "/roam?x=1"
    cookie = resp.headers["Set-Cookie"]
    assert cookie.startswith(f"{COOKIE}={TOKEN};")
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie
    assert "Domain" not in cookie
    assert client.get("/roam").status_code == 200
    assert client.get("/api/roam/status").status_code == 200


def test_token_link_cannot_redirect_off_site(client):
    resp = client.get("/", query_string={"token": TOKEN},
                      environ_overrides={"PATH_INFO": "//evil.example/"})
    assert resp.status_code == 303
    assert resp.headers["Location"] == "/evil.example/"


def test_token_is_compared_in_constant_time(client, monkeypatch):
    calls = []
    real = serve_app.hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(serve_app.hmac, "compare_digest", spy)
    client.set_cookie(COOKIE, TOKEN)
    assert client.get("/api/runs").status_code == 200
    assert calls


def test_create_app_requires_a_token(tmp_path):
    pytest.importorskip("flask")
    cfg = SimpleNamespace(project_dir=tmp_path, argus_dir=tmp_path / ".argus")
    with pytest.raises(ValueError):
        serve_app.create_app(cfg, access_token="")


def test_new_access_tokens_are_random():
    assert serve_app.new_access_token() != serve_app.new_access_token()
    assert len(serve_app.new_access_token()) >= 32


# ── SEC-02: Origin check ──────────────────────────────────────────────────


@pytest.mark.parametrize("origin", [None, "http://evil.example", "null", "http://localhost:9999",
                                    "https://localhost"])
def test_post_with_bad_origin_is_rejected(authed, no_launch, origin):
    headers = {"Origin": origin} if origin else {}
    resp = authed.post("/api/roam/start", json={"target": "calc.exe"}, headers=headers)
    assert resp.status_code == 403
    assert authed.get("/api/roam/status").get_json()["running"] is False


def test_post_with_own_origin_is_accepted(authed):
    resp = authed.post("/api/roam/stop", headers={"Origin": ORIGIN})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


# ── SEC-04: no debugger ───────────────────────────────────────────────────


def test_app_debug_is_off_even_with_flask_debug_env(tmp_path, monkeypatch):
    pytest.importorskip("flask")
    monkeypatch.setenv("FLASK_DEBUG", "1")
    cfg = SimpleNamespace(project_dir=tmp_path, argus_dir=tmp_path / ".argus")
    assert serve_app.create_app(cfg, access_token=TOKEN).debug is False


# ── SEC-05: project path ──────────────────────────────────────────────────


def test_project_path_hidden_from_unauthenticated(client, app):
    for path in ("/", "/roam", "/api/runs", "/?token=wrong"):
        assert b"secret-project-dir" not in client.get(path).data


def test_project_path_shown_after_auth(authed):
    assert b"secret-project-dir" in authed.get("/").data


# ── CLI: SEC-03 binding and SEC-04 run options ────────────────────────────


class _FakeApp:
    def __init__(self):
        self.run_kwargs = None

    def run(self, **kwargs):
        self.run_kwargs = kwargs


@pytest.fixture
def fake_serve(monkeypatch):
    fake = _FakeApp()
    seen = {}

    def _create_app(cfg, *, access_token):
        seen["token"] = access_token
        return fake

    monkeypatch.setattr(serve_app, "create_app", _create_app)
    monkeypatch.setattr(cli, "load_config", lambda: SimpleNamespace())
    return fake, seen


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "example.com"])
def test_non_loopback_host_refused_without_flag(fake_serve, host):
    fake, _ = fake_serve
    result = CliRunner().invoke(cli.main, ["serve", "--host", host])
    assert result.exit_code == 2
    assert "--allow-remote" in result.output
    assert fake.run_kwargs is None


def test_non_loopback_host_allowed_with_flag_and_warns(fake_serve):
    fake, _ = fake_serve
    result = CliRunner().invoke(cli.main, ["serve", "--host", "0.0.0.0", "--allow-remote"])
    assert result.exit_code == 0, result.output
    assert "exposes the dashboard to the network" in result.output
    assert fake.run_kwargs["host"] == "0.0.0.0"


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.2"])
def test_loopback_hosts_need_no_flag(fake_serve, host):
    result = CliRunner().invoke(cli.main, ["serve", "--host", host])
    assert result.exit_code == 0, result.output
    assert "network" not in result.output


@pytest.mark.parametrize("args", [[], ["--debug"]])
def test_debug_never_reaches_app_run(fake_serve, args):
    fake, _ = fake_serve
    result = CliRunner().invoke(cli.main, ["serve", *args])
    assert result.exit_code == 0, result.output
    assert "debug" not in fake.run_kwargs
    assert fake.run_kwargs["use_debugger"] is False
    assert fake.run_kwargs["use_reloader"] is False
    assert fake.run_kwargs["load_dotenv"] is False


def test_startup_prints_token_link_once(fake_serve):
    _, seen = fake_serve
    result = CliRunner().invoke(cli.main, ["serve"])
    assert result.exit_code == 0, result.output
    assert result.output.count(seen["token"]) == 1
    assert f"http://127.0.0.1:5000/?token={seen['token']}" in result.output
