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


@pytest.mark.parametrize("text", ["/watch stpo", "/watch stop please", "don't watch tests", "never stop watch"])
def test_invalid_or_negated_watch_chat_never_calls_watch_api(desktop_browser, monkeypatch, text):
    page, api, project = desktop_browser
    calls = []
    monkeypatch.setattr(ArgusConfig, "make_provider", lambda self, tracker=None:
                        FakeProvider([json.dumps({"intent": "watch", "args": {"action": "start"}})]))
    def unexpected_watch():
        calls.append(True)
        return {"ok": False, "error": "Unexpected watch operation"}
    monkeypatch.setattr(api, "watch_start", unexpected_watch)
    monkeypatch.setattr(api, "watch_stop", unexpected_watch)
    page.evaluate("text => sendText(text)", text)
    assert not calls
    assert not api.watch_status()["running"] and api._jobs == {}
    assert not page.evaluate("state.conv.msgs.some(m=>m.kind==='watch')")
    assert page.evaluate("state.conv.msgs.some(m=>m.role==='argus' && m.kind==='text')")


def test_late_stop_failure_card_and_followups_remain_failed(desktop_browser, monkeypatch):
    from argus.engine.results import RunResult
    page, api, project = desktop_browser
    spec = yaml.safe_load(CLI_SPEC)
    spec["steps"][1]["assert"]["stdout_contains"] = "not printed"
    (project / ".argus" / "late-stop.test.yaml").write_text(yaml.safe_dump(spec))
    original = RunResult.save
    def stop_during_save(result, project_dir):
        path = original(result, project_dir)
        api.stop()
        return path
    monkeypatch.setattr(RunResult, "save", stop_during_save)
    page.evaluate("runChip('Run failing test', I('run',{tests:['late-stop.test.yaml']}))")
    page.wait_for_function("state.conv.msgs.some(m=>m.kind==='run' && m.snap.status==='fail')")
    page.wait_for_function("state.conv.followups.some(f=>f.intent && f.intent.intent==='explain')")
    assert page.evaluate("state.conv.msgs.find(m=>m.kind==='run').snap.result.status") == "fail"
    assert page.locator(".badge.fail").count() > 0
    page.evaluate("flushConversations()")
    saved = next(m for c in api.load_conversations() for m in c["msgs"] if m["kind"] == "run")
    assert saved["snap"]["status"] == saved["snap"]["result"]["status"] == "fail"


@pytest.mark.parametrize("status", ["pass", "fail"])
def test_reload_finishes_terminal_restored_job_in_its_owning_conversation(desktop_browser, monkeypatch, status):
    from tests.test_gui_api import _wait

    page, api, project = desktop_browser
    spec = yaml.safe_load(CLI_SPEC)
    if status == "fail":
        spec["steps"][1]["assert"]["stdout_contains"] = "not printed"
    (project / ".argus" / "reload.test.yaml").write_text(yaml.safe_dump(spec))
    job = _wait(api, api.run_tests(["reload.test.yaml"])["job"]["id"])
    stale = dict(job["runs"][0], status="running", result=None, key=None, steps=[])
    api.save_conversations([{"id": "owner", "title": "Reload owner", "seq": 2,
        "msgs": [{"id": "owner:1", "kind": "run", "role": "argus", "job": job["id"], "idx": 0,
                  "snap": stale, "meta": {"running": True, "env_label": job["env_label"], "provider": job["provider"]}}]}])
    list_tests = api.list_tests
    calls = []
    def first_snapshot_is_stale():
        rows = list_tests()
        calls.append(True)
        if len(calls) == 1:
            return [dict(row, last=None) for row in rows]
        return rows
    monkeypatch.setattr(api, "list_tests", first_snapshot_is_stale)
    page.reload()
    page.wait_for_function("status => state.tests.some(t=>t.file==='reload.test.yaml' && t.last===status)", arg=status)
    page.wait_for_function("state.conversations.some(c=>c.id==='owner' && c.followups.length > 0)")
    assert len(calls) >= 2
    assert page.evaluate("state.conv.id") != "owner"
    followups = page.evaluate("state.conversations.find(c=>c.id==='owner').followups")
    if status == "fail":
        assert any(f.get("intent", {}).get("intent") == "explain" for f in followups)
    else:
        assert any(f.get("intent", {}).get("intent") == "evidence" for f in followups)
    assert not page.evaluate("activeJobIds().size")


def test_failed_knowledge_inspection_cannot_confirm_or_call_reset(desktop_browser, monkeypatch):
    page, api, project = desktop_browser
    monkeypatch.setattr(api, "knowledge", lambda target="": {"ok": False, "error": "No knowledge for typo"})
    monkeypatch.setattr(api, "knowledge_reset", lambda target: pytest.fail("Failed inspection must not reset"))
    page.evaluate("() => { window.confirm = () => { throw new Error('Must not confirm failed inspection'); }; }")
    page.evaluate("execute(I('knowledge', {action:'reset', target:'typo'}))")
    page.get_by_text("No knowledge for typo", exact=False).wait_for()


def test_known_knowledge_reset_confirms_and_deletes_real_graph(desktop_browser, monkeypatch):
    from argus.config import KnowledgeConfig
    page, api, project = desktop_browser
    cfg = api._config()
    cfg.knowledge = KnowledgeConfig(type="json")
    monkeypatch.setattr(api, "_config", lambda provider=None: cfg)
    directory = project / ".argus" / "knowledge"
    directory.mkdir()
    graph = directory / "app.graph.json"
    graph.write_text(json.dumps({"nodes": {}, "edges": []}))
    page.evaluate("() => { window.confirm = message => { window.resetConfirmation = message; return true; }; }")
    page.evaluate("execute(I('knowledge', {action:'reset', target:'app'}))")
    page.get_by_text("Knowledge for app is reset:", exact=False).wait_for()
    assert "app" in page.evaluate("window.resetConfirmation")
    assert not graph.exists()


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
                # Boot still restores conversations/jobs after loading the sidebar.
                # Wait for its final focus step before replacing polling or API hooks.
                page.wait_for_function("document.activeElement === document.getElementById('input')")
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


def test_watch_updates_hidden_conversation_and_persists_final_stop_snapshot(desktop_browser):
    page, api, project = desktop_browser
    page.evaluate("runChip('Watch these tests', I('watch',{action:'start'}))")
    owner = page.evaluate("state.conv.id")
    page.evaluate("newChat()")
    # A settled rerun belongs to the watch ID in the older conversation.
    api._watch["events"].append({"at": "12:00:00", "file": "smoke.test.yaml",
                                 "status": "pass", "summary": "2/2 steps passed"})
    page.wait_for_function("allConversations().some(c=>c.msgs.some(m=>m.kind==='watch' && m.watch.events.length===1))")
    page.evaluate("execute(I('watch',{action:'stop'}))")
    page.wait_for_function("allConversations().some(c=>c.msgs.some(m=>m.kind==='watch' && !m.watch.running && m.watch.events.length===1))")
    assert page.evaluate("state.conv.msgs.some(m=>m.kind==='watch')") is False
    page.evaluate("flushConversations()")
    saved = next(c for c in api.load_conversations() if c["id"] == owner)
    watch = next(m["watch"] for m in saved["msgs"] if m["kind"] == "watch")
    assert not watch["running"] and watch["events"][0]["status"] == "pass"
    page.reload()
    page.wait_for_function("state.info && state.conversations.length > 0")
    page.evaluate("openConversation(" + json.dumps(owner) + ")")
    assert page.evaluate("state.conv.msgs.find(m=>m.kind==='watch').watch.running") is False


def test_close_flush_cancels_debounce_and_saves_latest_after_older_inflight_save(desktop_browser):
    page, api, project = desktop_browser
    page.evaluate("""() => {
      const real = window.pywebview.api;
      window.saveCalls = [];
      window.releaseFirstSave = null;
      window.pywebview.api = new Proxy(real, {get: (target, key) => key === 'save_conversations' ? async (chats) => {
        window.saveCalls.push(chats);
        if (window.saveCalls.length === 1) await new Promise(resolve => window.releaseFirstSave = resolve);
        return target.save_conversations(chats);
      } : target[key]});
      sayIn(state.conv, 'First snapshot');
      flushConversations();
    }""")
    page.wait_for_function("window.releaseFirstSave !== null")
    page.evaluate("""() => {
      sayIn(state.conv, 'Latest response just before close');
      scheduleSave();
      window.closeResult = null;
      flushConversationsForClose().then(r => window.closeResult = r);
    }""")
    assert page.evaluate("window.closeResult") is None
    page.evaluate("window.releaseFirstSave()")
    page.wait_for_function("window.closeResult && window.closeResult.ok")
    assert len(page.evaluate("window.saveCalls")) == 2
    saved = api.load_conversations()
    assert saved[0]["msgs"][-1]["text"] == "Latest response just before close"
    assert page.evaluate("document.getElementById('input').disabled")
    page.reload()
    page.wait_for_function("state.conversations.some(c=>c.msgs.some(m=>m.text==='Latest response just before close'))")


def test_failed_close_save_returns_failure_and_can_retry(desktop_browser):
    page, api, project = desktop_browser
    page.evaluate("""() => {
      window.realBridge = window.pywebview.api;
      window.pywebview.api = new Proxy(window.realBridge, {get: (target,key) =>
        key === 'save_conversations' ? async () => ({ok:false}) : target[key]});
      sayIn(state.conv, 'Keep this response');
      scheduleSave();
    }""")
    assert page.evaluate("flushConversationsForClose()") == {"ok": False}
    # Native close rejection tells the webview to resume; the failure is visible.
    page.evaluate("window.dispatchEvent(new Event('argusclosesavefailed'))")
    page.get_by_text("This window stayed open because", exact=False).wait_for()
    assert not page.evaluate("state.closing")
    assert not page.locator("#input").is_disabled()
    page.evaluate("() => { window.pywebview.api = window.realBridge; }")
    assert page.evaluate("flushConversationsForClose()") == {"ok": True}
    assert any(m.get("text") == "Keep this response" for c in api.load_conversations() for m in c["msgs"])


def test_invalid_forced_environment_is_visible_and_cannot_run_local(desktop_browser, monkeypatch):
    page, api, project = desktop_browser
    monkeypatch.setenv("ARGUS_EXECUTION_ENVIRONMENT", "capusle")
    page.reload()
    page.wait_for_function("state.info && !state.info.ok")
    assert page.locator("#env-btn").inner_text() == "Check environment"
    page.get_by_text("ARGUS_EXECUTION_ENVIRONMENT must be local or capsule", exact=False).wait_for()
    page.evaluate("runChip('Run smoke', I('run',{tests:['smoke.test.yaml']}))")
    page.wait_for_function("state.conv.msgs.some(m=>m.error && m.text.includes('ARGUS_EXECUTION_ENVIRONMENT'))")
    assert api._jobs == {}


@pytest.mark.parametrize("action,method", [("knowledge", "knowledge"), ("switch_provider", "set_provider")])
def test_close_waits_for_pending_provider_or_knowledge_response(desktop_browser, action, method):
    page, api, project = desktop_browser
    page.evaluate("""({action,method}) => {
      const real = window.pywebview.api;
      window.releaseResponse = null;
      window.pywebview.api = new Proxy(real, {get:(target,key) => key===method ? async (...args) => {
        await new Promise(resolve=>window.releaseResponse=resolve);
        return target[key](...args);
      } : target[key]});
      runChip('Pending response', I(action, action==='knowledge' ? {action:'show'} : {provider:'anthropic'}));
    }""", {"action": action, "method": method})
    page.wait_for_function("window.releaseResponse !== null")
    assert page.evaluate("state.pendingActions") == 1
    assert page.evaluate("flushConversationsForClose()") == {"ok": False}
    page.evaluate("window.releaseResponse()")
    page.wait_for_function("state.pendingActions === 0")
    assert page.evaluate("flushConversationsForClose()") == {"ok": True}
    saved = api.load_conversations()
    messages = saved[0]["msgs"]
    assert messages[-1]["role"] == "argus" and messages[-1]["kind"] != "thinking"
    assert any(m.get("text") == "Pending response" for m in messages)


@pytest.mark.parametrize("entry", ["card", "followup"])
@pytest.mark.parametrize("outcome", ["draft", "error"])
def test_regression_draft_pending_owner_autosave_and_reload(desktop_browser, entry, outcome):
    page, api, project = desktop_browser
    page.evaluate("""({entry,outcome}) => {
      const real=window.pywebview.api;
      window.stubRelease=null; window.savedDraft=false; window.stubArgs=null;
      window.pywebview.api=new Proxy(real,{get:(target,key)=>{
        if(key==='regression_stub') return async(...args)=>{
          window.stubArgs=args;
          await new Promise(resolve=>window.stubRelease=resolve);
          if(outcome==='error') throw new Error('Draft unavailable; try again');
          return {ok:true,id:'round-five-draft',name:'Regression',filename:'regression.test.yaml',
            yaml:'name: Regression\\ntarget:\\n  adapter: cli\\n  launch: echo ready\\nsteps:\\n  - assert:\\n      exit_code_is: 0\\n',notes:[]};
        };
        if(key==='save_conversations') return async(chats)=>{
          const r=await target[key](chats);
          window.savedDraft=chats.some(c=>c.msgs.some(m=>m.kind==='spec' || (m.error && m.text.includes('Draft unavailable'))));
          return r;
        };
        return target[key];
      }});
      const snap={id:'roam-round-five',target:'echo ready',adapter:'cli',env_label:'Local',
        running:false,status:'pass',started_at:1,ended_at:2,minutes:1,tokens:0,memory:true,
        log:[],findings:[],regressions:['regression.test.yaml']};
      push({role:'argus',kind:'roam',snap});
      if(entry==='followup') followupsAfter(state.conv,state.conv.msgs.at(-1));
    }""", {"entry": entry, "outcome": outcome})
    owner = page.evaluate("state.conv.id")
    if entry == "card":
        page.get_by_role("button", name="Write regression test", exact=True).click()
    else:
        page.locator("#followups").get_by_role("button", name="Turn finding 1 into a test", exact=True).click()
    page.wait_for_function("window.stubRelease !== null")
    assert page.evaluate("window.stubArgs") == ["roam-round-five", 0]
    assert page.evaluate("state.pendingActions") == 1
    assert page.evaluate("state.conv.msgs.some(m=>m.kind==='thinking')")
    assert page.evaluate("flushConversationsForClose()") == {"ok": False}
    page.evaluate("newChat()")
    page.evaluate("window.stubRelease()")
    page.wait_for_function("state.pendingActions === 0 && window.savedDraft")
    assert not page.evaluate("state.conv.msgs.some(m=>m.kind==='spec' || m.error)")
    saved = next(c for c in api.load_conversations() if c["id"] == owner)
    assert not any(m["kind"] == "thinking" for m in saved["msgs"])
    if outcome == "draft":
        draft = next(m["draft"] for m in saved["msgs"] if m["kind"] == "spec")
        assert draft["id"] == "round-five-draft" and not draft.get("saved")
        assert any(f.get("intent", {}).get("intent") == "save_test" for f in saved["followups"])
        assert not (project / ".argus" / "regression.test.yaml").exists()
    else:
        assert any(m.get("error") and "Draft unavailable" in m["text"] for m in saved["msgs"])
    page.reload()
    page.wait_for_function("id=>state.conversations.some(c=>c.id===id)", arg=owner)
    restored = page.evaluate("id=>state.conversations.find(c=>c.id===id)", owner)
    assert restored["msgs"] == saved["msgs"]


@pytest.mark.parametrize("known", [False, True])
def test_knowledge_roam_chip_requires_original_launch_target(desktop_browser, monkeypatch, known):
    page, api, project = desktop_browser
    monkeypatch.setattr(api, "knowledge", lambda target="": {
        "ok": True, "target": "http-localhost-3000", "launch_target": "http://localhost:3000" if known else None,
        "targets": ["http-localhost-3000"], "backend": "json", "states": 3, "transitions": 2, "bugs": 0, "sessions": 1})
    monkeypatch.setattr(api, "knowledge_reset", lambda target: {"ok": True, "target": target})
    page.evaluate("execute(I('knowledge',{action:'show'}))")
    followups = page.evaluate("state.conv.followups")
    assert any(f["intent"]["args"].get("action") == "export" for f in followups)
    roams = [f for f in followups if f["intent"]["intent"] == "roam"]
    assert len(roams) == int(known)
    if known:
        assert roams[0]["intent"]["args"]["target"] == "http://localhost:3000"
    page.evaluate("() => { window.confirm=()=>true; }")
    page.evaluate("execute(I('knowledge',{action:'reset'}))")
    roams = page.evaluate("state.conv.followups")
    assert len(roams) == int(known)
    if known:
        assert roams[0]["intent"]["args"]["target"] == "http://localhost:3000"


def test_poll_requests_during_a_delayed_tick_cannot_start_parallel_ticks(desktop_browser):
    page, api, project = desktop_browser
    page.wait_for_function("!state.pollInFlight")
    page.evaluate("""() => {
      clearTimeout(state.pollTimer); state.pollTimer=null;
      const real=window.pywebview.api;
      window.pollCalls=0; window.pollActive=0; window.pollMax=0; window.pollRelease=null;
      window.pywebview.api=new Proxy(real,{get:(target,key)=>key==='watch_status'?async()=>{
        window.pollCalls++; window.pollActive++; window.pollMax=Math.max(window.pollMax,window.pollActive);
        await new Promise(resolve=>window.pollRelease=resolve);
        window.pollActive--;
        return {id:'delayed',running:window.pollCalls===1,events:[],settled_count:0};
      }:target[key]});
      state.watchOn=true; poll();
    }""")
    page.wait_for_function("window.pollCalls===1")
    page.evaluate("() => { for(let i=0;i<10;i++) poll(); }")
    page.wait_for_timeout(900)
    assert page.evaluate("window.pollCalls") == 1
    page.evaluate("window.pollRelease()")
    page.wait_for_function("window.pollCalls===2")
    page.evaluate("window.pollRelease()")
    page.wait_for_function("!state.pollInFlight && !state.pollTimer")
    assert page.evaluate("window.pollMax") == 1
    assert page.evaluate("window.pollCalls") == 2


def test_bounded_watch_history_still_refreshes_tests_after_new_completion(desktop_browser):
    page, api, project = desktop_browser
    page.wait_for_function("!state.pollInFlight")
    page.evaluate("""() => {
      clearTimeout(state.pollTimer); state.pollTimer=null;
      const real=window.pywebview.api;
      window.refreshCount=0; refreshTests=async()=>{window.refreshCount++};
      window.pywebview.api=new Proxy(real,{get:(target,key)=>key==='watch_status'?async()=>({
        id:'bounded',running:false,settled_count:201,
        events:Array.from({length:200},(_,i)=>({file:String(i),status:'pass'}))
      }):target[key]});
      state.watchSettled=200; state.watchOn=true; tick();
    }""")
    page.wait_for_function("!state.pollInFlight && state.watchSettled===201")
    assert page.evaluate("window.refreshCount") == 1


def test_close_waits_for_in_flight_poll_before_saving_final_snapshot(desktop_browser):
    page, api, project = desktop_browser
    page.wait_for_function("!state.pollInFlight")
    page.evaluate("""() => {
      clearTimeout(state.pollTimer); state.pollTimer=null;
      const real=window.pywebview.api; let first=true;
      window.pollRelease=null; window.closeResult=null;
      window.pywebview.api=new Proxy(real,{get:(target,key)=>key==='watch_status'?async()=>{
        if(first){ first=false; await new Promise(resolve=>window.pollRelease=resolve); }
        return {id:'close-poll',running:false,settled_count:1,events:[{file:'latest',status:'pass'}]};
      }:target[key]});
      push({role:'argus',kind:'watch',watch:{id:'close-poll',running:true,events:[]}});
      state.watchOn=true; poll();
    }""")
    page.wait_for_function("window.pollRelease !== null")
    page.evaluate("() => { flushConversationsForClose().then(r=>window.closeResult=r); }")
    assert page.evaluate("window.closeResult") is None
    page.evaluate("window.pollRelease()")
    page.wait_for_function("window.closeResult && window.closeResult.ok")
    saved = api.load_conversations()
    watch = next(m["watch"] for c in saved for m in c["msgs"] if m.get("kind") == "watch")
    assert watch["events"][-1] == {"file": "latest", "status": "pass"}
    assert not watch["running"] and page.evaluate("state.pollTimer") is None
