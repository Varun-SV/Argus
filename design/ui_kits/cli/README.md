# Argus CLI — Terminal UI kit

A recreation of the **`argus` command-line tool** — the primary interface for developers and CI/CD.

## Run it
Open `index.html`. The `argus run` transcript **streams in** on load; click **replay** to watch it again.

## What's shown
- **Command rail** (left) — the core verbs (`init`, `run`, `watch`, `record`, `report`, `serve`), exit-code convention (0/1/2), and the `ARGUS_*` env vars.
- **Terminal** (right) — a colored `argus run` transcript: per-step `✓ / ✗ / ⊘` glyphs, durations, agentic action notes (`↳`), an assertion failure with an expected-vs-actual diff, and a final summary + exit code.

## Files
- `index.html` — terminal/rail CSS + wiring.
- `data.js` — the structured transcript (window.CLI_DATA).
- `app.jsx` — `Terminal` (streaming), `CommandRail`.
- Reuses `../console/base.jsx` for the shared `Icon` atom.

## Notes
Pure CSS + tokens for the ANSI-style coloring (`--syntax-*`, `--status-*`). The streaming is a `setInterval` reveal; background tabs throttle timers, so a backgrounded preview streams slower.
