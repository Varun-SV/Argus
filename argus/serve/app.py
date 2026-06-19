"""Flask web dashboard for Argus — shows run history and roam reports."""
from __future__ import annotations

import json
import time
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from argus.config import ArgusConfig

# Module-level live state shared by in-process roam sessions.
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

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Argus Dashboard</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 2rem; background: #0d1117; color: #c9d1d9; }
  h1 { color: #58a6ff; }
  table { width: 100%; border-collapse: collapse; margin-top: 1rem; }
  th { text-align: left; padding: 0.5rem; border-bottom: 1px solid #30363d; color: #8b949e; }
  td { padding: 0.5rem; border-bottom: 1px solid #21262d; }
  .pass { color: #3fb950; } .fail { color: #f85149; } .error { color: #d29922; }
  .tokens { color: #8b949e; font-size: 0.85em; }
</style>
</head>
<body>
<h1>Argus Dashboard</h1>
<p class="tokens">Project: <code>{{ project_dir }}</code></p>
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
<p>No roam sessions yet — run <code>argus roam</code> to get started.</p>
{% endif %}
</body>
</html>"""


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
    template = Template(HTML_TEMPLATE)

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
            for session_file in sorted(roam_dir.glob("*/session.json"), reverse=True)[:20]:
                try:
                    data = json.loads(session_file.read_text(encoding="utf-8"))
                    sessions.append({
                        "target": data.get("target", "?"),
                        "findings": len(data.get("findings", [])),
                        "actions": len(data.get("actions", [])),
                        "duration_s": data.get("duration_s", 0),
                        "stopped_reason": data.get("stopped_reason", "?"),
                    })
                except (json.JSONDecodeError, OSError):
                    continue

        return template.render(
            project_dir=str(cfg.project_dir),
            runs=runs,
            sessions=sessions,
        )

    @flask_app.route("/api/runs")
    def api_runs():
        from flask import jsonify
        return jsonify(load_runs(cfg.project_dir, limit=50))

    @flask_app.route("/api/live")
    def api_live():
        """Return the latest screenshot as PNG (from in-process session or disk)."""
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
                # Stream from in-process log
                with _live_lock:
                    log_snapshot = list(_live_log)
                for line in log_snapshot[mem_seen:]:
                    yield f"data: {json.dumps(line)}\n\n"
                mem_seen = len(log_snapshot)

                # Stream from disk events file (written by CLI roam sessions)
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
