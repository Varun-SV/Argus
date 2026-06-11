"""Flask web dashboard for Argus — shows run history and roam reports."""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from argus.config import ArgusConfig

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

    return flask_app
