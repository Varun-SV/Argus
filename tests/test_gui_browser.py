"""Chat interaction regressions against the real API and a local CLI target.

CI installs Chromium in its quality job. Other platforms can run their unit
suite without the optional browser dependency or a browser installation.
"""

import json
import re
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml

from argus.config import ArgusConfig
from argus.gui.app import ArgusAPI, WEB_DIR
from tests.conftest import FakeProvider
from tests.test_gui_api import CLI_SPEC


@pytest.fixture
def desktop_browser(tmp_path, monkeypatch):
    playwright = pytest.importorskip("playwright.sync_api")
    monkeypatch.setenv("ARGUS_GUI_STATE_DIR", str(tmp_path / "user-state"))
    for name in ("ARGUS_PROVIDER", "ARGUS_MODEL", "ARGUS_EXECUTION_ENVIRONMENT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ArgusConfig, "make_provider",
                        lambda self, tracker=None: FakeProvider([CLI_SPEC]))
    api = ArgusAPI(tmp_path)
    api.init_project()

    config = tmp_path / ".argus" / "config.yaml"
    raw = yaml.safe_load(config.read_text())
    raw.setdefault("knowledge", {})["enabled"] = False
    config.write_text(yaml.safe_dump(raw))

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(WEB_DIR), **kwargs)

        def log_message(self, *args):
            pass

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            method = data["method"]
            try:
                if method.startswith("_") or not callable(getattr(api, method, None)):
                    raise ValueError("Unknown API method")
                result = {"result": getattr(api, method)(*data["args"])}
            except Exception as exc:
                result = {"error": str(exc)}
            body = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    bridge = """window.pywebview = {api: new Proxy({}, {get: (_, method) => async (...args) => {
      const r = await fetch('/api', {method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({method,args})});
      const d = await r.json(); if(d.error) throw new Error(d.error); return d.result;
    }})};"""
    errors = []
    try:
        with playwright.sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except playwright.Error as exc:
                if "Executable doesn't exist" in str(exc):
                    pytest.skip("Run playwright install chromium to enable desktop browser checks")
                raise
            try:
                page = browser.new_page(viewport={"width": 1024, "height": 680})
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                page.add_init_script(bridge)
                page.goto(f"http://127.0.0.1:{server.server_port}")
                page.wait_for_function("state.info && state.tests.length > 0")
                yield page, api, tmp_path
                assert errors == []
            finally:
                browser.close()
    finally:
        api.stop()
        api.watch_stop()
        server.shutdown()
        server.server_close()


def test_chat_run_draft_evidence_watch_and_keyboard(desktop_browser):
    page, api, project = desktop_browser
    page.locator("#tools-btn").click()
    assert page.locator("#tools-menu button").first.evaluate("e=>e===document.activeElement")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Escape")
    assert page.locator("#tools-btn").evaluate("e=>e===document.activeElement")
    page.locator("#input").fill("/help")
    page.locator("#input").press("Enter")
    page.wait_for_function("state.conv.msgs.some(m=>m.kind==='help')")
    page.evaluate("runChip('Run smoke', I('run', {tests:['smoke.test.yaml']}))")
    page.wait_for_function("state.conv.msgs.some(m=>m.kind==='run' && m.snap.status==='pass')")
    page.locator(".card").get_by_role("button", name="Evidence", exact=True).click()
    page.wait_for_function("state.conv.msgs.some(m=>m.kind==='evidence' && m.e.verified)")
    run = page.evaluate("state.conv.msgs.find(m=>m.kind==='run').snap")
    assert api.evidence(run["key"])["state"] == "bound_verified"
    assert run["result"]["tokens"]["calls"] == 0
    page.evaluate("runChip('Draft a test', I('write_test', {description:'Script says OK'}))")
    page.wait_for_function("state.conv.msgs.some(m=>m.kind==='spec')")
    draft = page.evaluate("state.conv.msgs.find(m=>m.kind==='spec').draft")
    assert not (project / ".argus" / draft["file"]).exists()
    page.evaluate("execute(I('dry_run', {tests:'draft',draft:state.conv.msgs.find(m=>m.kind==='spec').draft.id}))")
    page.wait_for_function("state.conv.msgs.some(m=>m.kind==='dry')")
    page.locator(".card").get_by_role("button", name="Save to .argus", exact=True).click()
    page.wait_for_function("state.conv.msgs.find(m=>m.kind==='spec').draft.saved")
    assert (project / ".argus" / draft["file"]).is_file()
    page.evaluate("runChip('Watch', I('watch', {action:'start'}))")
    page.wait_for_function("state.watchOn")
    file = project / ".argus" / "smoke.test.yaml"
    file.write_text(file.read_text() + "\n# watch regression\n")
    page.wait_for_function("state.conv.msgs.some(m=>m.kind==='watch' && m.watch.events.some(e=>e.status==='pass'))", timeout=30000)
    page.evaluate("execute(I('watch',{action:'stop'}))")
    page.wait_for_function("!state.watchOn")
    assert page.locator(".box-row").evaluate("e=>e.scrollWidth<=e.clientWidth")


def test_reload_restores_live_watch_and_handles_missing_jobs_and_corrupt_cards(desktop_browser):
    page, api, project = desktop_browser
    page.evaluate("runChip('Watch', I('watch', {action:'start'}))")
    api.save_conversations(page.evaluate("allConversations()"))
    page.reload()
    page.wait_for_function("state.watchOn && state.conversations.length > 0")
    page.locator("#stop-btn").wait_for(state="visible")
    api.watch_stop()
    api.save_conversations([{"id": "restored", "title": "Interrupted test", "seq": 3,
        "msgs": [{"id": "restored:1", "role": "argus", "kind": "run"},
                 {"id": "restored:2", "role": "argus", "kind": "run", "idx": 0,
                  "job": "missing", "meta": {"running": True},
                  "snap": {"status": "running", "steps": [], "planned": []}}]}])
    page.reload()
    page.wait_for_function("state.conversations.some(c=>c.id==='restored')")
    page.get_by_role("button", name="Interrupted test", exact=True).click()
    page.get_by_text("This saved result could not be restored.", exact=False).wait_for()
    page.get_by_text("Outcome unknown", exact=True).wait_for()
    page.wait_for_function("document.getElementById('stop-btn').hidden")


def test_reload_reattaches_same_active_job_without_starting_another(desktop_browser):
    page, api, project = desktop_browser
    slow = project / ".argus" / "slow.test.yaml"
    slow.write_text(CLI_SPEC.replace("print(123)", "import time; time.sleep(4); print(123)"))
    page.evaluate("runChip('Run slow', I('run',{tests:['slow.test.yaml']}))")
    page.wait_for_function("state.conv.msgs.some(m=>m.kind==='run' && m.meta.running)")
    job_id = page.evaluate("state.conv.msgs.find(m=>m.kind==='run').job")
    api.save_conversations(page.evaluate("allConversations()"))
    page.reload()
    page.wait_for_function("state.conversations.some(c=>c.msgs.some(m=>m.kind==='run' && m.meta.running))")
    page.locator("#stop-btn").wait_for(state="visible")
    page.wait_for_function("state.conversations.some(c=>c.msgs.some(m=>m.kind==='run' && m.snap.status==='pass'))")
    assert api._active_job == job_id
    assert len(api._jobs) == 1


def test_provider_environment_and_unavailable_knowledge_are_visible(desktop_browser):
    page, api, project = desktop_browser
    config = project / ".argus" / "config.yaml"
    original = config.read_bytes()
    page.locator("#model-btn").click()
    page.locator("#model-menu").get_by_role("menuitemradio", name=re.compile("^openai")).click()
    page.wait_for_function("state.info.provider==='openai'")
    page.evaluate("execute(I('providers'))")
    page.wait_for_function("state.conv.msgs.some(m=>m.kind==='providers' && m.status.ok)")
    page.locator("#env-btn").click()
    page.get_by_role("menuitemradio", name=re.compile("^Capsule · auto")).click()
    page.wait_for_function("state.info.environment==='capsule'")
    page.evaluate("execute(I('environment'))")
    page.wait_for_function("state.conv.msgs.some(m=>m.kind==='env' && m.env.rows.some(r=>r.v==='not ready'))")
    page.get_by_text("No VM was created.", exact=False).wait_for()
    page.evaluate("execute(I('environment',{environment:'local'}))")
    page.evaluate("execute(I('tokens'))")
    page.wait_for_function("state.conv.msgs.some(m=>m.kind==='tokens')")
    page.evaluate("execute(I('knowledge',{action:'show'}))")
    page.get_by_text("The knowledge store is disabled", exact=False).wait_for()
    assert config.read_bytes() == original
    assert api._jobs == {}
    # An API error after opening the model menu must be rendered, not become
    # an unhandled promise rejection containing a credential-bearing YAML line.
    page.locator("#model-btn").click()
    config.write_text("providers: [\napi_key: sentinel-secret\n")
    page.locator("#model-menu").get_by_role("menuitemradio", name=re.compile("^anthropic")).click()
    page.get_by_text("Could not load .argus/config.yaml.", exact=False).wait_for()
    assert "sentinel-secret" not in page.locator("body").inner_text()
