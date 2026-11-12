import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from argus2.server import App, make_server
from argus2.settings import Settings
from argus2.targets import TargetError, WebTarget

from tests.v2.test_core import run


# ---- web target -----------------------------------------------------------
def test_web_exact_test(browser_ok, tmp_path, workspace, site):
    result, _ = run(Settings(tmp_path / "blank.json"), workspace, f"""
name: wrong password is rejected
target: {{type: web, url: "{site}/"}}
steps:
  - fill: {{selector: "#user", text: ann}}
  - fill: {{selector: "#pass", text: wrong}}
  - click: "#go"
  - check: {{text_visible: Invalid password, url_contains: "{site}/", element_visible: "#go"}}
  - fill: {{selector: "#pass", text: secret}}
  - press: Enter
  - check: {{text_visible: "Welcome, ann", title_contains: Home, element_absent: "#go"}}
""")
    assert result["status"] == "passed", result["summary"]
    shots = sorted(p.name for p in (workspace / ".argus" / "results" / result["id"]).glob("*.jpg"))
    assert len(shots) >= 7 and result["steps"][-1]["shot"] in shots


def test_web_model_drives_the_page_by_element_number(browser_ok, settings, workspace, site, model):
    model.script({"action": "type", "ref": 1, "text": "ann", "why": "user name"},
                 {"action": "type", "ref": 2, "text": "secret", "submit": True, "why": "password"},
                 {"action": "done", "success": True, "why": "the page greets ann"},
                 {"pass": True, "why": "it says Welcome, ann"})
    result, _ = run(settings, workspace, f"""
name: sign in
target: {{type: web, url: "{site}/"}}
steps:
  - do: Sign in as ann with the password secret
  - verify: The page welcomes ann
  - check: {{text_visible: "Welcome, ann"}}
""")
    assert result["status"] == "passed", result["summary"]
    seen = model.requests[0]["body"]["messages"][1]["content"]
    assert '[1] input(text) "User name" value=""' in seen and '[3] button "Sign in"' in seen
    assert 'value="ann"' in model.requests[1]["body"]["messages"][1]["content"]
    assert result["steps"][0]["actions"][0]["label"] == "Type 'ann' into \"User name\" [1]"


def test_web_actions_report_real_effects(browser_ok, site):
    target = WebTarget({"url": site + "/"})
    target.start()
    try:
        target.observe()
        with pytest.raises(TargetError, match=r"no element \[99\]"):
            target.act({"action": "click", "ref": 99})
        with pytest.raises(TargetError, match="failed"):  # a button cannot be typed into
            target.act({"action": "type", "ref": 3, "text": "x"})
        target.act({"action": "click", "ref": 4})
        assert any("boom exploded" in p for p in target.observe().problems)
        assert target.check("text_absent", "Welcome")[0]
    finally:
        target.close()
    again = WebTarget({"url": site + "/"})  # a second launch on the same thread must work
    again.start()
    again.close()


def test_web_unreachable_site_is_a_setup_error(browser_ok, tmp_path, workspace):
    result, _ = run(Settings(tmp_path / "blank.json"), workspace,
                    'name: x\ntarget: {type: web, url: "http://127.0.0.1:9/"}\nsteps: [{check: {text_visible: a}}]')
    assert result["status"] == "error" and "Could not open" in result["summary"]


# ---- server ---------------------------------------------------------------
class Http:
    def __init__(self, base, cookie=""):
        self.base, self.cookie = base, cookie

    def call(self, method, path, body=None, headers=None):
        request = urllib.request.Request(self.base + path, method=method,
                                         data=json.dumps(body).encode() if body is not None else None)
        if self.cookie:
            request.add_header("Cookie", self.cookie)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=40) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                return error.code, json.loads(raw)
            except ValueError:
                return error.code, {"raw": raw.decode()}


@pytest.fixture
def served(settings, workspace):
    app = App(settings, workspace)
    server = make_server(app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield app, Http(base, f"argus2_token={app.token}"), base
    server.shutdown()


def wait_done(http):
    since = 0
    events = []
    for _ in range(100):
        _, data = http.call("GET", f"/api/events?since={since}")
        assert data["job"], "no job was started"
        events += data["events"]
        since = data["next"]
        if data["job"] and not data["job"]["running"]:
            return events, data["job"]
    raise AssertionError("job did not finish")


def test_server_requires_the_token_and_the_right_origin(served):
    app, http, base = served
    assert Http(base).call("GET", "/api/state")[0] == 401
    assert Http(base, "argus2_token=wrong").call("GET", "/api/state")[0] == 401
    assert http.call("GET", "/api/state")[0] == 200
    assert http.call("POST", "/api/stop", {}, {"Origin": "http://evil.example"})[0] == 403
    assert http.call("GET", "/api/state", headers={"Host": "evil.example"})[0] == 403
    assert Http(base).call("GET", "/?t=wrong")[0] == 403
    assert http.call("GET", "/../settings.py")[0] == 404


def test_settings_api_never_returns_keys_and_tests_the_connection(served, model):
    app, http, _ = served
    status, state = http.call("GET", "/api/state")
    assert model.key not in json.dumps(state) and state["settings"]["model_missing"] == ""
    _, out = http.call("POST", "/api/provider/test", {"provider": "custom", "model": "fake-1"})
    assert out["ok"] is False or out["ok"] is True
    model.script("ready")
    _, out = http.call("POST", "/api/provider/test", {"provider": "custom", "model": "fake-1"})
    assert out["ok"] and out["reply"] == "ready" and out["models"] == ["fake-1", "fake-2"]
    assert out["settings"]["providers"][-1]["models"] == ["fake-1", "fake-2"]
    _, out = http.call("POST", "/api/provider/save", {"provider": "custom", "api_key": "nope"})
    _, out = http.call("POST", "/api/provider/test", {"provider": "custom"})
    assert not out["ok"] and "rejected the API key" in out["error"]
    _, out = http.call("POST", "/api/provider/test", {"provider": "openai"})
    assert not out["ok"] and "no API key" in out["error"]
    assert http.call("POST", "/api/settings", {"max_actions": 1000})[0] == 400


def test_run_save_and_results_over_http(served, model):
    app, http, _ = served
    model.script({"action": "run", "command": "echo hi", "why": "greet"},
                 {"action": "done", "success": True, "why": "printed hi"})
    status, job = http.call("POST", "/api/run", {"target": {"type": "cli"}, "behaviour": "Print hi"})
    assert status == 200
    events, job = wait_done(http)
    kinds = [e["type"] for e in events]
    assert kinds[0] == "job_start" and kinds[-1] == "job_end" and "action" in kinds
    _, result = http.call("GET", f"/api/results/{job['results'][0]}")
    assert result["status"] == "passed" and result["source"].startswith("name: Print hi")
    _, saved = http.call("POST", "/api/tests", {"content": result["source"]})
    assert saved["file"] == "print-hi.yaml" and saved["tests"][0]["name"] == "Print hi"
    assert http.call("POST", "/api/tests", {"content": "name: x"})[0] == 400
    _, check = http.call("POST", "/api/tests/validate", {"content": "name: ["})
    assert not check["ok"] and "YAML" in check["error"]
    _, listing = http.call("GET", "/api/results")
    assert listing[0]["passed_steps"] == 1 and listing[0]["total_steps"] == 1
    assert http.call("DELETE", "/api/tests/print-hi.yaml")[1] == []


def test_model_steps_are_refused_until_a_model_is_set_up(tmp_path, workspace):
    app = App(Settings(tmp_path / "blank.json"), workspace)
    server = make_server(app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    http = Http(f"http://127.0.0.1:{server.server_address[1]}", f"argus2_token={app.token}")
    try:
        status, out = http.call("POST", "/api/run", {"target": {"type": "cli"}, "behaviour": "Print hi"})
        assert status == 412 and "Settings" in out["error"]
        assert http.call("POST", "/api/explore", {"target": {"type": "cli"}})[0] == 412
        status, _ = http.call("POST", "/api/run", {"content": "name: x\ntarget: {type: cli}\nsteps: [{run: echo hi}]"})
        assert status == 200
        wait_done(http)
    finally:
        server.shutdown()


def test_stop_ends_a_running_job(served, model):
    app, http, _ = served
    model.script(*[{"action": "wait", "seconds": 0.3, "why": "idle"}] * 50)
    http.call("POST", "/api/explore", {"target": {"type": "cli"}, "budget": 50})
    time.sleep(0.8)
    assert http.call("POST", "/api/run", {"target": {"type": "cli"}, "behaviour": "x"})[0] == 409
    http.call("POST", "/api/stop", {})
    _, job = wait_done(http)
    _, result = http.call("GET", f"/api/results/{job['results'][0]}")
    assert result["status"] == "stopped" and len(result["actions"]) < 50
