# Argus

**Argus** is an autonomous application testing tool that uses multimodal LLMs to test any software — desktop GUI, web, CLI, or shell scripts — the way a real user would: by looking at screenshots, reading the accessibility tree, and taking actions.

**No selectors. No scripts. Just describe what you want.**

```
$ argus run checkout.test.yaml

Argus v0.1.0 · provider: ollama:gemma3:9b

Running checkout.test.yaml…
  ✓  Type 'hello from argus' into the editor          2.31s
  ✓  text_visible: 'hello from argus'                  0.12s
  ✓  Open the File menu                                1.08s
  ✓  element_exists: {name: Save, control_type: MenuItem}  0.09s

4 passed · 0 failed · 0 skipped · 3.6s · 1840 tokens · exit 0
```

---

## Why Argus?

| Traditional automation | Argus |
|---|---|
| Brittle XPath/CSS selectors | Natural language + accessibility tree |
| Breaks on UI changes | Adapts like a human tester |
| Platform-specific scripts | Same YAML format on Windows, Linux, macOS, web |
| Visual tests need special tooling | Multimodal LLMs understand screenshots natively |
| Exploratory testing is manual | `argus roam` autonomously hunts for bugs |

---

## Install

```bash
# Core (works on any OS):
pip install argus-app-testing

# Windows desktop-GUI testing:
pip install "argus-app-testing[windows]"

# Browser testing (Playwright):
pip install "argus-app-testing[browser]"
playwright install chromium

# Linux GUI testing (X11/Xvfb):
pip install "argus-app-testing[linux]"

# Web dashboard:
pip install "argus-app-testing[serve]"

# Everything:
pip install "argus-app-testing[all]"
```

Requires **Python 3.10+**. For free local testing, install [Ollama](https://ollama.com) and pull a multimodal model:

```bash
ollama pull gemma3:9b
```

---

## Quick start

```bash
argus init                              # scaffold .argus/ (config + example test)
argus providers                         # check connection and vision support
argus run                               # run every .argus/*.test.yaml
argus run checkout.test.yaml --dry-run  # preview steps without running
argus watch                             # re-run on file change (CI dev mode)
argus roam "notepad.exe" --minutes 5    # free-roam bug hunting
argus roam "http://localhost:3000" --adapter browser --minutes 10
argus roam "my-script.sh" --adapter cli
argus serve                             # web dashboard at http://localhost:5000
argus tokens                            # cumulative token usage
argus report                            # recent run history
argus gui                               # native desktop app
```

---

## Writing tests

Tests are YAML files in `.argus/`, mixing **natural-language steps** with **structured assertions**. The LLM handles the NL steps; assertions run deterministically — the model is never consulted for them:

### Desktop GUI (Windows/Linux)

```yaml
name: Notepad smoke test
target:
  adapter: desktop-gui
  launch: notepad.exe
retries: 1                              # retry flaky steps once

steps:
  - "Type 'hello from argus' into the editor"
  - assert:
      text_visible: "hello from argus"
  - "Open the File menu"
  - assert:
      element_exists:
        name: "Save"
        control_type: MenuItem
  - assert:
      window_title_contains: "Notepad"

teardown:
  - close
```

### Browser (Playwright)

```yaml
name: Homepage loads
target:
  adapter: browser
  launch: "https://example.com"

steps:
  - "Check that the page loaded"
  - assert:
      page_title_contains: "Example"
  - assert:
      url_contains: "example.com"
  - "Click the first link"
  - assert:
      process_running: true

teardown:
  - close
```

### CLI / Shell script

```yaml
name: Script returns zero
target:
  adapter: cli
  launch: "python my_script.py --check"

steps:
  - assert:
      exit_code_is: 0
  - assert:
      stdout_contains: "OK"
```

**All assertion types:**

| Assertion | Adapters | Example |
|---|---|---|
| `text_visible` | desktop, browser | `text_visible: "Hello"` |
| `window_title_contains` | desktop | `window_title_contains: "Notepad"` |
| `element_exists` | desktop | `element_exists: {name: Save}` |
| `process_running` | all | `process_running: true` |
| `dialog_open` | desktop | `dialog_open: "Error"` |
| `stdout_contains` | cli | `stdout_contains: "OK"` |
| `stderr_contains` | cli | `stderr_contains: "warning"` |
| `exit_code_is` | cli | `exit_code_is: 0` |
| `url_contains` | browser | `url_contains: "dashboard"` |
| `page_title_contains` | browser | `page_title_contains: "Home"` |

**Exit codes:** `0` all pass · `1` failure · `2` error/crash

---

## Free-roam mode: autonomous bug hunting

```bash
argus roam "your-app.exe" --minutes 10
argus roam "http://localhost:3000" --adapter browser --minutes 5
argus roam "./my-cli.sh" --adapter cli --minutes 2
```

The LLM explores the application like a curious tester — opens every menu, fills every field, tries empty/long/special-character input — with no script. Argus:

- **Detects bugs automatically**: crashes, error dialogs, hangs, and anything the model itself flags
- **Remembers across sessions**: `--memory` (default) stores explored paths per target so each run discovers *new* territory
- **Documents everything**: session journal + screenshot per finding
- **Writes `.argus/roam/<stamp>/report.md`** + auto-generated regression-test stubs to refine and keep

```bash
argus roam "notepad.exe" --minutes 5 --no-memory  # fresh exploration every time
```

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
  openai:
    model: gpt-4o
    api_key_env: OPENAI_API_KEY

budgets:
  time_minutes: 10        # time budget (all providers)
  max_tokens: null        # optional token budget (ignored for ollama — it is local/free)
```

Environment overrides: `ARGUS_PROVIDER`, `ARGUS_MODEL`, `ARGUS_API_KEY`, `ARGUS_BASE_URL`

**Vision:** Argus auto-detects whether your model is multimodal. If not, it degrades gracefully to accessibility-tree-only observation and warns you.

**Token tracking and cost estimation:** Every LLM call is metered. `argus tokens` shows cumulative usage. Cost estimates are available for cloud providers (GPT-4o, Claude, Gemini, etc.).

---

## Watch mode (CI development)

```bash
argus watch                   # re-runs all tests whenever any .test.yaml changes
argus watch my-test.yaml      # watch a specific test file
```

Useful while writing tests: save the YAML, see the result instantly.

---

## Web dashboard

```bash
pip install "argus-app-testing[serve]"
argus serve                   # opens at http://127.0.0.1:5000
argus serve --host 0.0.0.0 --port 8080
```

Shows run history and roam session summaries from `.argus/runs/` and `.argus/roam/`.

---

## Windows-specific notes

- `notepad.exe` on Windows 11 is a **WinUI3 packaged app** — Argus automatically falls back from PID-based connection to a process-tree scan, so it just works.
- `explorer.exe` is a system singleton that cannot be re-launched — Argus attaches to the existing process automatically.
- YAML files written by `argus init` use UTF-8; files saved by other editors (cp1252 on older Windows installs) are read with a cp1252 fallback.

---

## Project layout

```
argus/
  providers/        LLM layer — Ollama, Anthropic, OpenAI, Azure, Gemini, LiteLLM
  adapters/         desktop-gui (Windows+Linux), cli, browser (Playwright)
  engine/           spec parser, hybrid-agentic runner, free-roam explorer
  serve/            Flask web dashboard
  gui/              native desktop app (pywebview)
  cli.py            init / run / roam / watch / serve / providers / tokens / report / gui
tests/              pytest suite — no OS or LLM deps (fake provider + fake adapter)
.argus/             auto-created: config.yaml, *.test.yaml, runs/, roam/
```

---

## License

MIT — see [LICENSE](LICENSE).
