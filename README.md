# Argus

**Argus** is a universal application testing tool driven by multimodal LLMs. It watches an application the way a person would — through **screenshots** and the **OS accessibility tree** — then drives it to satisfy tests written as a mix of natural-language steps and structured assertions.

It is fully **provider-agnostic**: Ollama (local, free), Anthropic, OpenAI, Azure, Gemini, LiteLLM.

```
$ argus run checkout.test.yaml
Argus v0.1.0 · provider: ollama:gemma3:9b

Running checkout.test.yaml…
  ✓  Type the sentence 'hello from argus' into the editor  2.31s
     ↳ typed 'hello from argus' — type into the editor area
  ✓  text_visible: 'hello from argus'  0.12s
  ✓  Open the File menu  1.08s
  ✓  element_exists: {name: Save, control_type: MenuItem}  0.09s

4 passed · 0 failed · 0 skipped · 3.6s · 1840 tokens · exit 0
```

---

## Install

```bash
# Windows (the first supported desktop-GUI platform):
pip install -e ".[windows,gui]"

# engine only (any OS — desktop-gui adapter requires Windows for now):
pip install -e .
```

Requires Python 3.10+. For local testing install [Ollama](https://ollama.com) and pull a **multimodal** model:

```bash
ollama pull gemma3:9b
```

## Quick start

```bash
argus init             # scaffold .argus/ (config + example notepad test)
argus providers        # check the connection AND whether your model has vision
argus run              # run every .argus/*.test.yaml
argus roam "notepad.exe" --minutes 5    # free-roam exploratory testing
argus tokens           # cumulative token usage, any time
argus report           # recent run history
argus gui              # open the desktop app
```

---

## Writing tests

Tests are YAML files in `.argus/`, mixing **natural-language steps** (the LLM fills in the ambiguity) with **structured assertions** (executed deterministically — the model is never consulted for them):

```yaml
name: Notepad types and finds text
target:
  adapter: desktop-gui
  launch: notepad.exe

steps:
  - "Type the sentence 'hello from argus' into the editor"
  - assert:
      text_visible: "hello from argus"
  - "Open the File menu"
  - assert:
      element_exists:
        name: "Save"
        control_type: MenuItem

teardown:
  - close
```

Supported assertions: `text_visible`, `window_title_contains`, `element_exists`, `process_running`, `dialog_open`.

Exit codes: `0` all pass · `1` failure · `2` error/crash.

---

## Free-roam mode

```bash
argus roam "your-app.exe" --minutes 10
```

The LLM explores the application **like a curious child** — opens every menu, fills every field, tries empty/long/special-character input — with no script. While it roams, Argus:

- **detects bugs automatically**: crashes, error dialogs, hangs — plus anything the model itself flags as broken,
- **documents everything** in a session journal,
- **captures a screenshot for each finding**, and
- **writes `.argus/roam/<stamp>/report.md`** plus auto-generated regression-test stubs you can refine and keep.

> On Windows, roaming drives the real desktop — avoid using the machine while it runs. A virtual-display mode (Linux/Xvfb) is on the roadmap so roaming can run fully in the background.

---

## Providers & budgets

Configure in `.argus/config.yaml` (created by `argus init`):

```yaml
provider: ollama          # ollama | anthropic | openai | azure | gemini | litellm
providers:
  ollama:
    model: gemma3:9b
    base_url: http://localhost:11434
  anthropic:
    model: claude-sonnet-4-6
    api_key_env: ANTHROPIC_API_KEY

budgets:
  time_minutes: 10        # time budget — the only budget used for ollama (local = free)
  max_tokens: null        # optional token budget for paid providers
```

Env overrides: `ARGUS_PROVIDER`, `ARGUS_MODEL`, `ARGUS_API_KEY`, `ARGUS_BASE_URL`.

**Vision detection.** Argus asks the backend **once** whether your model is multimodal (e.g. Ollama's capability metadata). If it isn't, Argus tells you that vision-related testing is unavailable and degrades gracefully to accessibility-tree-only observation.

**Token tracking.** Every LLM call is metered. `argus tokens` (or the GUI status bar) shows cumulative usage at any point in time.

---

## The desktop app

```bash
pip install -e ".[gui]"
argus gui
```

A native window (WebView2 on Windows) built on the Argus design system: run tests with live step results, drive free-roam sessions with a live journal, check provider connectivity/vision, and watch token usage.

---

## Project layout

```
argus/              the application
  providers/        provider-agnostic LLM layer (ollama, anthropic, openai-compatible)
  adapters/         target adapters (windows desktop-gui; more to come)
  engine/           spec parser, hybrid-agentic runner, free-roam explorer
  gui/              the desktop app (pywebview + design-system UI)
  cli.py            argus init/run/roam/providers/tokens/report/gui
tests/              pytest suite (fake provider + fake adapter, no OS deps)
design/             the Argus design system (tokens, components, UI kits)
```

## Roadmap

- Adapters: Linux/macOS desktop-GUI, browser (Playwright), CLI/terminal, TUI
- Virtual-display roaming (Xvfb) — fully background exploration
- `argus watch` (re-run on change) and `argus serve` (web dashboard)
- Run history dashboards from `.argus/runs/`

## License

MIT — see [LICENSE](LICENSE).
