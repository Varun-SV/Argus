# Argus Console — Desktop App UI kit

A high-fidelity recreation of the **Argus desktop app** (Tauri · Rust backend, React frontend). It demonstrates the three core surfaces of the app in one interactive shell.

## Run it
Open `index.html`. Everything is fake/demo data — no backend.

## Screens (switch via the titlebar nav)
- **Runs** — the signature view. Test tree (left) → run detail with segmented progress, a **captured-frame stage** (what Argus saw, with the acted-on element highlighted), a **filmstrip** of per-step frames, and a step timeline. The right **Inspector** shows the selected step's model reasoning, the assertion diff (expected vs actual on failure), and the log tail.
- **Editor** — YAML source with line numbers + syntax highlighting beside a live **resolved-steps** preview (hybrid-agentic: NL steps + structured `assert` blocks).
- **Providers** — the provider-agnostic layer: a grid of providers (Anthropic, OpenAI, Ollama, Gemini, Azure, LiteLLM) with an active selection and a connection config form.

## Interactions
- Click any test in the tree to load it; failing tests auto-select the failed step.
- Click filmstrip thumbnails or step rows to drive the Inspector.
- "Run all" / "Re-run" animate a running state. Toggle **watch** mode. Switch the active provider.

## Files
- `index.html` — shell + all kit layout CSS, script wiring.
- `data.js` — demo suites, steps, providers, YAML (window.ARGUS_DATA).
- `base.jsx` — Lucide `Icon`, `Status`, `Tag`, `Btn`, `IconBtn`, `CapturedFrame`.
- `Sidebar.jsx` · `RunDetail.jsx` · `Inspector.jsx` · `Editor.jsx` · `Providers.jsx` · `app.jsx`

## Notes
- Built on the design system's CSS layer (`styles.css` tokens + `.ds-*` component classes) so it stays self-contained and offline-capable. Icons are **Lucide** (CDN). The "captured frames" are abstract wireframe placeholders — drop in real screenshots for a production mock.
