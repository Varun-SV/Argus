# Argus 2 (preview)

Argus 2 is a small, new implementation of Argus in `argus2/`. It was written from scratch and shares no code with the `argus` package, the Capsule/Fleet/ATES machinery or the Rust workspace. It exists to make the core promise dependable first:

> Point Argus at an application, say what it should do in plain words, watch it use the application the way a person would, and get a verdict with the evidence.

It is a preview: the existing `argus` command and desktop app are unchanged and remain the released product.

## Start it

```bash
pip install "argus-app-testing[app]"
```

```bash
argus2
```

`argus2` starts a local server on this computer and opens the app: in a native window when `pywebview` is installed (`pip install "argus-app-testing[app,gui]"`), otherwise in your browser. The link it prints contains a one-time access token; the server listens on `127.0.0.1` only.

## The app

| Screen | What it is for |
|---|---|
| **Test something** | Choose a website or a command line, describe the behaviour one step per line, run it. Nothing has to be saved first. |
| **Running now** | The loop as it happens: the current stage (observe, decide, act, verify), each action with the model's reason and its real result, and what Argus sees (the page, or the terminal). **Stop** ends the run after the current action. |
| **Saved tests** | Tests kept in the workspace and run again later. The editor validates as you type and can ask the model to write a test from a description. |
| **Explore** | No script: Argus roams the target, tries awkward input, and reports defects it actually observed. JavaScript errors and HTTP 5xx responses are recorded as findings automatically. |
| **Results** | Every run with its steps, actions, screenshots or transcript, the model used and the tokens spent. |
| **Settings** | Providers, API keys, the model, the browser, run options, the workspace. |

### Settings: models and keys

Everything is configured in the app; no file has to be edited.

- Providers: Anthropic (Claude), OpenAI, Google Gemini, Ollama, and any OpenAI-compatible server (LiteLLM, LM Studio, vLLM, Azure OpenAI v1).
- **Save key** stores the key in the per-user settings file (`settings.json` in the Argus configuration folder, readable only by your account). Keys are never sent back to the page and never written into a workspace. If no key is saved, the provider's usual environment variable is used and labelled as such.
- **Find models** asks the provider for its real model list, so you choose from what your key can actually use.
- **Test and use this model** sends one real message before saving. A wrong key, address or model name is reported in plain words at this point, not in the middle of a run.

Tests that contain only exact steps run without any model.

## Test files

Saved tests live in `<workspace>/.argus/tests/*.yaml`:

```yaml
name: Sign-in rejects a wrong password
target:
  type: web                 # web | cli
  url: https://your-app.example.com/login
steps:
  - do: Sign in as "ann" with the password "wrong"       # the model acts
  - verify: An error says the password is incorrect      # the model judges
  - check: {text_visible: "Invalid password"}            # exact, no model
```

| Step | Target | Meaning |
|---|---|---|
| `do:` (or a plain sentence) | both | The model carries it out, one action at a time, and reports whether it worked |
| `verify:` | both | The model judges a statement against the current state |
| `check:` | both | Exact assertions. Web: `text_visible`, `text_absent`, `url_contains`, `title_contains`, `element_visible`, `element_absent`. CLI: `output_contains`, `output_absent`, `output_matches`, `exit_code`, `transcript_contains` |
| `goto:`, `click:`, `fill:`, `press:` | web | Exact actions with Playwright selectors |
| `run:`, `send:` | cli | Run one command; or type a line into the program named by `target.command` |
| `wait:` | both | Seconds |

A run stops at the first step that does not pass; later steps are reported as skipped. Each step records whether it was judged by the model or by an exact check. A test containing an unfinished placeholder such as `[EXPECTED TEXT]` is refused.

## Command line

```bash
argus2 run
```

```bash
argus2 explore https://your-app.example.com --actions 30
```

```bash
argus2 doctor
```

`argus2 run` runs the saved tests of the current folder and exits with 0 (all passed), 1 (a test failed) or 2 (a test could not run).

## What it does not do

- Desktop GUI targets, Capsules, Fleet, ATES evidence, the knowledge store and watch mode are not part of Argus 2.
- Pages inside iframes are not read.
- On Windows, a program kept open (`target.command`) is driven through pipes, not a console, so programs that buffer their output when not attached to a terminal need their unbuffered mode (for Python, `python -u -i`).
- Command-line tests run commands on this computer in the workspace folder. Explore a command line only where that is acceptable.
