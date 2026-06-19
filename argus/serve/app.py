"""Flask web dashboard for Argus — shows run history and roam reports.

Remote workflow: ``argus serve`` keeps running after you close your SSH/RDP
session (use tmux/screen/nohup). Then reconnect from any browser to start or
monitor a roam session via the /roam control page.
"""
from __future__ import annotations

import json
import time
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from argus.config import ArgusConfig

# Module-level live state for in-process roam/run sessions.
_live_lock = threading.Lock()
_live_png: Optional[bytes] = None
_live_log: list = []


def set_live_screenshot(png: bytes) -> None:
    global _live_png
    with _live_lock:
        _live_png = png


def append_live_event(line: str) -> None:
    with _live_lock:
        _live_log.append(line)


# ── HTML templates ─────────────────────────────────────────────────────────

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Argus Dashboard</title>
<style>
  body{font-family:system-ui,sans-serif;margin:2rem;background:#0d1117;color:#c9d1d9}
  h1{color:#58a6ff}a{color:#58a6ff}
  table{width:100%;border-collapse:collapse;margin-top:1rem}
  th{text-align:left;padding:.5rem;border-bottom:1px solid #30363d;color:#8b949e}
  td{padding:.5rem;border-bottom:1px solid #21262d}
  .pass{color:#3fb950}.fail{color:#f85149}.error{color:#d29922}
  .tokens{color:#8b949e;font-size:.85em}
  .btn{display:inline-block;padding:.4rem 1rem;border-radius:6px;border:none;
       cursor:pointer;font-size:.9rem;text-decoration:none}
  .btn-primary{background:#1f6feb;color:#fff}
  .btn-primary:hover{background:#388bfd}
</style>
</head>
<body>
<h1>Argus Dashboard</h1>
<p class="tokens">Project: <code>{{ project_dir }}</code></p>
<p><a class="btn btn-primary" href="/roam">▶ Launch Roam Session</a></p>
<h2>Recent Runs</h2>
{% if runs %}
<table>
<tr><th>Test</th><th>Status</th><th>Steps</th><th>Duration</th><th>Tokens</th><th>Provider</th></tr>
{% for r in runs %}
<tr>
  <td>{{ r.test_file }}</td>
  <td class="{{ r.status }}">{{ r.status }}</td>
  <td>{{ r.passed }}/{{ r.total }}</td>
  <td>{{ "%.1f"|format(r.duration_s) }}s</td>
  <td class="tokens">{{ r.total_tokens }}</td>
  <td>{{ r.provider }}</td>
</tr>
{% endfor %}
</table>
{% else %}
<p>No runs yet — run <code>argus run</code> to get started.</p>
{% endif %}
<h2>Roam Sessions</h2>
{% if sessions %}
<table>
<tr><th>Target</th><th>Findings</th><th>Actions</th><th>Duration</th><th>Stopped</th></tr>
{% for s in sessions %}
<tr>
  <td>{{ s.target }}</td>
  <td class="{{ 'fail' if s.findings else 'pass' }}">{{ s.findings }}</td>
  <td>{{ s.actions }}</td>
  <td>{{ "%.0f"|format(s.duration_s) }}s</td>
  <td>{{ s.stopped_reason }}</td>
</tr>
{% endfor %}
</table>
{% else %}
<p>No roam sessions yet.</p>
{% endif %}
</body>
</html>"""

_ROAM_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Argus — Remote Roam</title>
<style>
  *{box-sizing:border-box;margin:0}
  body{font-family:system-ui,sans-serif;background:#0d1117;color:#c9d1d9;
       display:grid;grid-template-columns:420px 1fr;height:100vh;overflow:hidden}
  .panel{padding:1.5rem;display:flex;flex-direction:column;gap:1rem;
         border-right:1px solid #30363d;overflow-y:auto}
  h1{color:#58a6ff;font-size:1.2rem}
  label{display:block;font-size:.8rem;color:#8b949e;margin-bottom:.3rem}
  input{width:100%;padding:.5rem .75rem;background:#161b22;border:1px solid #30363d;
        border-radius:6px;color:#c9d1d9;font-size:.9rem}
  input:focus{outline:none;border-color:#58a6ff}
  .btn{padding:.5rem 1.2rem;border-radius:6px;border:none;cursor:pointer;
       font-weight:600;font-size:.9rem}
  .btn-start{background:#1a7f37;color:#fff}.btn-start:hover{background:#2ea043}
  .btn-stop{background:#b62324;color:#fff}.btn-stop:hover{background:#da3633}
  .btn:disabled{opacity:.4;cursor:not-allowed}
  .row{display:grid;grid-template-columns:1fr 1fr;gap:.75rem}
  .status-row{display:flex;gap:1.5rem;font-size:.85rem;color:#8b949e}
  .status-row b{color:#c9d1d9}
  .log-wrap{flex:1;overflow-y:auto;background:#010409;border:1px solid #21262d;
            border-radius:6px;padding:.75rem;font-family:monospace;font-size:.78rem;
            line-height:1.6;white-space:pre-wrap}
  .finding{color:#e3b341}
  .live{flex:1;display:flex;flex-direction:column;overflow:hidden}
  .live-head{padding:.75rem 1rem;border-bottom:1px solid #30363d;
             display:flex;align-items:center;gap:1rem;font-size:.85rem;color:#8b949e}
  .live-body{flex:1;display:flex;align-items:center;justify-content:center;
             background:#010409;overflow:hidden}
  .live-body img{max-width:100%;max-height:100%;object-fit:contain}
  .live-body .placeholder{color:#30363d;font-size:.9rem}
  a{color:#58a6ff;text-decoration:none}
</style>
</head>
<body>
<div class="panel">
  <h1>Argus — Remote Roam</h1>
  <a href="/" style="font-size:.8rem">← Dashboard</a>
  <div>
    <label>Target command</label>
    <input id="target" placeholder="notepad.exe" spellcheck="false">
  </div>
  <div class="row">
    <div>
      <label>Minutes</label>
      <input id="minutes" type="number" value="10" min="1">
    </div>
    <div>
      <label>Max tokens (optional)</label>
      <input id="tokens" type="number" placeholder="none">
    </div>
  </div>
  <div style="display:flex;gap:.5rem">
    <button class="btn btn-start" id="btn-start" onclick="startRoam()">▶ Start</button>
    <button class="btn btn-stop" id="btn-stop" onclick="stopRoam()" disabled>■ Stop</button>
  </div>
  <div class="status-row">
    <span>status <b id="st-state">idle</b></span>
    <span>findings <b id="st-findings">0</b></span>
    <span>tokens <b id="st-tokens">—</b></span>
  </div>
  <div class="log-wrap" id="log">No session yet.</div>
  <div id="report" style="font-size:.8rem;color:#8b949e"></div>
</div>
<div class="live">
  <div class="live-head">
    Live View
    <span id="live-ts" style="margin-left:auto"></span>
  </div>
  <div class="live-body">
    <img id="live-img" style="display:none" alt="live view">
    <div class="placeholder" id="live-ph">No screenshot yet — start a roam session.</div>
  </div>
</div>
<script>
let polling = null;
let liveInterval = null;

async function startRoam() {
  const target = document.getElementById("target").value.trim();
  const minutes = parseFloat(document.getElementById("minutes").value) || 10;
  const maxTokens = document.getElementById("tokens").value || null;
  const res = await fetch("/api/roam/start", {
    method: "POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify({target, minutes, max_tokens: maxTokens ? parseInt(maxTokens) : null}),
  });
  const data = await res.json();
  if (!data.ok) { alert(data.error); return; }
  document.getElementById("btn-start").disabled = true;
  document.getElementById("btn-stop").disabled = false;
  document.getElementById("log").textContent = "";
  document.getElementById("report").textContent = "";
  document.getElementById("st-state").textContent = "roaming";
  clearInterval(polling);
  polling = setInterval(pollStatus, 1000);
  clearInterval(liveInterval);
  liveInterval = setInterval(pollLive, 800);
}

async function stopRoam() {
  await fetch("/api/roam/stop", {method:"POST"});
}

async function pollStatus() {
  const res = await fetch("/api/roam/status");
  const st = await res.json();
  const log = document.getElementById("log");
  log.innerHTML = (st.log || []).map(l =>
    l.includes("FINDING") ? `<span class="finding">${esc(l)}</span>\\n` : esc(l) + "\\n"
  ).join("");
  log.scrollTop = log.scrollHeight;
  document.getElementById("st-findings").textContent = st.findings || 0;
  if (!st.running) {
    clearInterval(polling);
    clearInterval(liveInterval);
    document.getElementById("btn-start").disabled = false;
    document.getElementById("btn-stop").disabled = true;
    document.getElementById("st-state").textContent = "finished";
    if (st.report) {
      document.getElementById("report").textContent = "Report: " + st.report;
    }
  }
}

async function pollLive() {
  const res = await fetch("/api/live");
  if (!res.ok) return;
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const img = document.getElementById("live-img");
  img.src = url;
  img.style.display = "";
  document.getElementById("live-ph").style.display = "none";
  document.getElementById("live-ts").textContent = new Date().toLocaleTimeString();
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
</script>
</body>
</html>"""


# ── Flask app ──────────────────────────────────────────────────────────────

def create_app(cfg: "ArgusConfig"):
    try:
        from flask import Flask
        from jinja2 import Template
    except ImportError as exc:
        raise ImportError(
            "Flask is required for the web dashboard — "
            "install with: pip install argus-app-testing[serve]"
        ) from exc

    from argus.engine.results import load_runs

    flask_app = Flask(__name__)
    dashboard_tmpl = Template(_DASHBOARD_HTML)

    # Per-app roam session state (shared across requests in the same process).
    _roam: dict = {"running": False, "log": [], "report": None, "findings": 0, "stop": False}
    _roam_lock = threading.Lock()

    # ── pages ──────────────────────────────────────────────────────────────

    @flask_app.route("/")
    def index():
        runs_raw = load_runs(cfg.project_dir, limit=50)
        runs = []
        for r in runs_raw:
            steps = r.get("steps", [])
            runs.append({
                "test_file": r.get("test_file", "?"),
                "status": r.get("status", "?"),
                "passed": sum(1 for s in steps if s.get("status") == "pass"),
                "total": len(steps),
                "duration_s": r.get("duration_s", 0),
                "total_tokens": r.get("tokens", {}).get("total_tokens", 0),
                "provider": r.get("provider", "?"),
            })
        sessions = []
        roam_dir = cfg.argus_dir / "roam"
        if roam_dir.is_dir():
            for f in sorted(roam_dir.glob("*/session.json"), reverse=True)[:20]:
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    sessions.append({
                        "target": data.get("target", "?"),
                        "findings": len(data.get("findings", [])),
                        "actions": len(data.get("actions", [])),
                        "duration_s": data.get("duration_s", 0),
                        "stopped_reason": data.get("stopped_reason", "?"),
                    })
                except (json.JSONDecodeError, OSError):
                    continue
        return dashboard_tmpl.render(
            project_dir=str(cfg.project_dir), runs=runs, sessions=sessions
        )

    @flask_app.route("/roam")
    def roam_page():
        from flask import Response
        return Response(_ROAM_HTML, mimetype="text/html")

    # ── roam control API ───────────────────────────────────────────────────

    @flask_app.route("/api/roam/start", methods=["POST"])
    def api_roam_start():
        from flask import jsonify, request
        data = request.get_json(silent=True) or {}
        target = (data.get("target") or "").strip()
        minutes = float(data.get("minutes") or 10)
        max_tokens = data.get("max_tokens")

        with _roam_lock:
            if _roam["running"]:
                return jsonify({"ok": False, "error": "a session is already running"})
            if not target:
                return jsonify({"ok": False, "error": "target is required"})
            _roam.update(running=True, log=[], report=None, findings=0, stop=False)

        def _worker():
            from argus.adapters import AdapterError, create_adapter
            from argus.engine.roam import roam
            from argus.providers.base import ProviderError
            from argus.tokens import TokenTracker

            tracker = TokenTracker()
            ks = None
            try:
                provider = cfg.make_provider(tracker)
                raw_adapter = create_adapter("desktop-gui")
                budget = cfg.make_budget(
                    tracker,
                    time_minutes=minutes,
                    max_tokens=int(max_tokens) if max_tokens else None,
                )
                ks = cfg.make_knowledge_store()
                stamp = time.strftime("%Y%m%d-%H%M%S")
                session_dir = cfg.argus_dir / "roam" / stamp

                class _CapAdapter:
                    def observe(self, include_screenshot=True):
                        obs = raw_adapter.observe(include_screenshot=include_screenshot)
                        if obs.screenshot_png:
                            set_live_screenshot(obs.screenshot_png)
                        return obs
                    def __getattr__(self, n): return getattr(raw_adapter, n)
                    def launch(self, t): return raw_adapter.launch(t)
                    def act(self, a): return raw_adapter.act(a)
                    def close(self): return raw_adapter.close()

                def _on_event(line):
                    with _roam_lock:
                        _roam["log"].append(line)
                    append_live_event(line)

                session = roam(
                    target=target,
                    provider=provider,
                    adapter=_CapAdapter(),
                    budget=budget,
                    session_dir=session_dir,
                    on_event=_on_event,
                    stop_flag=lambda: _roam["stop"],
                    knowledge_store=ks,
                )
                with _roam_lock:
                    _roam["findings"] = len(session.findings)
                    _roam["report"] = str(session_dir / "report.md")
            except (AdapterError, ProviderError, OSError) as exc:
                with _roam_lock:
                    _roam["log"].append(f"error: {exc}")
            finally:
                if ks is not None:
                    ks.close()
                tracker.persist(cfg.project_dir)
                with _roam_lock:
                    _roam["running"] = False

        threading.Thread(target=_worker, daemon=True).start()
        return jsonify({"ok": True})

    @flask_app.route("/api/roam/stop", methods=["POST"])
    def api_roam_stop():
        from flask import jsonify
        with _roam_lock:
            _roam["stop"] = True
        return jsonify({"ok": True})

    @flask_app.route("/api/roam/status")
    def api_roam_status():
        from flask import jsonify
        with _roam_lock:
            return jsonify(dict(_roam))

    # ── live media API ─────────────────────────────────────────────────────

    @flask_app.route("/api/runs")
    def api_runs():
        from flask import jsonify
        return jsonify(load_runs(cfg.project_dir, limit=50))

    @flask_app.route("/api/live")
    def api_live():
        """Return the latest screenshot as PNG (in-process or from disk)."""
        from flask import Response, abort
        with _live_lock:
            png = _live_png
        if png is None:
            live_file = cfg.argus_dir / "live.png"
            if live_file.is_file():
                try:
                    png = live_file.read_bytes()
                except OSError:
                    pass
        if png is None:
            abort(404)
        return Response(png, mimetype="image/png",
                        headers={"Cache-Control": "no-store"})

    @flask_app.route("/api/events")
    def api_events():
        """SSE endpoint streaming live roam/run log lines."""
        from flask import Response, stream_with_context

        live_events_file = cfg.argus_dir / "live.events"

        def generate():
            file_seen = 0
            mem_seen = 0
            while True:
                with _live_lock:
                    log_snapshot = list(_live_log)
                for line in log_snapshot[mem_seen:]:
                    yield f"data: {json.dumps(line)}\n\n"
                mem_seen = len(log_snapshot)

                if live_events_file.is_file():
                    try:
                        lines = live_events_file.read_text(encoding="utf-8").splitlines()
                        for line in lines[file_seen:]:
                            if line.strip():
                                yield f"data: {json.dumps(line)}\n\n"
                        file_seen = len(lines)
                    except OSError:
                        pass

                yield ": keepalive\n\n"
                time.sleep(0.5)

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return flask_app
