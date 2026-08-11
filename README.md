# Argus

**Argus** is an autonomous application testing tool that uses multimodal LLMs to test desktop GUIs, web applications, CLI tools, and scripts the way a real user would: observe the application, decide what to do, act, and verify the result.

**No brittle selectors. No test scripts for every interaction. Describe the behavior you want to validate.**

```text
$ argus run checkout.test.yaml

Argus v0.1.0 · provider: ollama:gemma3:9b

Running checkout.test.yaml…
  ✓  Type 'hello from argus' into the editor          2.31s
  ✓  text_visible: 'hello from argus'                  0.12s
  ✓  Open the File menu                                1.08s
  ✓  element_exists: {name: Save, control_type: MenuItem}  0.09s

4 passed · 0 failed · 0 skipped · 3.6s · 1840 tokens · exit 0
```

## Production architecture: Argus Capsules

Argus now separates **how a target is driven** from **where it executes**.

```text
Runner / Roam
     |
     v
ExecutionEnvironment
   |            |
   |            +---- CapsuleExecutionEnvironment
   |                       |
   |                       v
   |                  Capsule provider
   |                  (Hyper-V/libvirt)
   |                       |
   |                       v
   |                  Guest adapter
   |
   +---- LocalExecutionEnvironment
                |
                v
             Adapter
```

### Capsules — recommended for production

Use a **Capsule** for production, destructive, exploratory, or otherwise high-risk testing. Capsules execute inside disposable virtual machines and preserve the isolation boundary between Argus-driven input and the operator's desktop.

Implemented Capsule providers:

| Provider | Host | Guest | Notes |
|---|---|---|---|
| Hyper-V | Windows | Windows | Secure disposable Generation-2 VM sessions |
| libvirt/QEMU/KVM | Linux | Linux | Isolated KVM sessions with qcow2 overlays |
| `auto` | Windows/Linux | supported matching guest | Selects the host-appropriate provider |

Current Capsule architecture includes:

- pinned HTTPS guest control for production mode;
- bootstrap-to-random per-session bearer rotation;
- isolated networking and fail-closed provider capabilities;
- explicit host-to-guest staging and guest-to-host artifact collection;
- optional forensic **Failure Capsule** retention on supported scripted `argus run` failure paths;
- no silent fallback to local execution when Capsule requirements cannot be met;
- Hyper-V and Linux libvirt/QEMU/KVM providers.

See the [Argus documentation hub](docs/README.md), [Hyper-V Capsule guide](docs/capsules-hyperv.md), and [multi-OS Capsule guide](docs/capsules-multi-os.md).

### Local — lightweight mode

`local` remains the compatibility default. It is useful for development, quick checks, and machines where virtualization is unavailable, but it is a **shared, non-isolated execution environment**.

On Windows desktop tests, Argus defaults to target-constrained semantic UI Automation rather than intentionally injecting host-wide physical mouse and keyboard input. Legacy physical input is available only through explicit opt-in and should be treated as a higher-risk mode.

> **Important:** "Capsules are recommended for production" is product guidance. Existing configurations continue to default to local execution for compatibility.

---

## Why Argus?

| Traditional automation | Argus |
|---|---|
| Brittle XPath/CSS selectors | Natural language + structured observations |
| Breaks on ordinary UI changes | Can adapt based on what the application currently shows |
| Platform-specific interaction code | One test format across supported adapters |
| Exploratory testing is mostly manual | `argus roam` autonomously explores and records findings |
| Test driver often shares the operator environment | Production tests can run inside disposable Capsules |
| Remote AI action can become host-wide input | Windows local desktop mode defaults to semantic, target-constrained execution |

---

## Install

```bash
# Core
pip install argus-app-testing

# Windows desktop GUI
pip install "argus-app-testing[windows]"

# Browser (Playwright)
pip install "argus-app-testing[browser]"
playwright install chromium

# Linux GUI (X11/Xvfb)
pip install "argus-app-testing[linux]"

# Web dashboard
pip install "argus-app-testing[serve]"

# Everything
pip install "argus-app-testing[all]"
```

Requires **Python 3.10+**. For local LLM inference, install [Ollama](https://ollama.com) and pull a compatible model, for example:

```bash
ollama pull gemma3:9b
```

Capsule execution additionally requires a prepared virtualization host and golden guest image. Start with the [Capsule documentation](docs/README.md#recommended-production-mode-capsules) rather than assuming a VM can be created from the Python package alone.

---

## Quick start

```bash
argus init                               # scaffold .argus/
argus providers                          # check provider and vision support
argus run                                # run every .argus/*.test.yaml
argus run checkout.test.yaml --dry-run   # preview without execution
argus watch                              # re-run on test-file changes
argus roam "notepad.exe" --minutes 5
argus roam "http://localhost:3000" --adapter browser --minutes 10
argus roam "my-script.sh" --adapter cli
argus serve                              # dashboard
argus tokens                             # cumulative token usage
argus report                             # run history
argus gui                                # native desktop app
```

---

## Adapters versus execution environments

These concepts are intentionally separate.

### Adapter = how Argus interacts

- `desktop-gui`
- `browser`
- `cli`

### Execution environment = where it runs

- `local` — lightweight/shared
- `capsule` — isolated VM execution; recommended for production

That separation lets the runner and test specification stay mostly independent of the virtualization provider.

---

## Writing tests

Tests are YAML files in `.argus/`, mixing **natural-language steps** with **structured assertions**. Natural-language actions use the model; supported assertions execute deterministically.

### Desktop GUI

```yaml
name: Notepad smoke test
target:
  adapter: desktop-gui
  launch: notepad.exe
retries: 1

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

### Browser

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

teardown:
  - close
```

### CLI / shell

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

Supported assertions include `text_visible`, `window_title_contains`, `element_exists`, `process_running`, `dialog_open`, `stdout_contains`, `stderr_contains`, `exit_code_is`, `url_contains`, and `page_title_contains`.

**Exit codes:** `0` all pass · `1` failure · `2` error/crash

---

## Free-roam mode

```bash
argus roam "your-app.exe" --minutes 10
argus roam "http://localhost:3000" --adapter browser --minutes 5
argus roam "./my-cli.sh" --adapter cli --minutes 2
```

The model explores the application without a pre-authored interaction script. Argus can detect crashes/error dialogs, record findings, retain cross-session exploration memory, and create roam reports/regression stubs.

For invasive roaming, **Capsule execution is the recommended production boundary**.

> **Current retention limitation:** roam findings do not currently invoke the Capsule failure-retention hook before teardown. `retain_on_failure: true` must therefore not be relied on to preserve a Failure Capsule for `argus roam` findings yet. Failure Capsule retention described below applies to the scripted `argus run` failure paths that call the execution environment's failure-recording lifecycle.

---

## Safe Windows local input

Windows local desktop execution defaults to semantic UI Automation through the safe adapter path. This avoids intentional host-wide physical pointer/keyboard injection for normal supported actions.

The safe execution layer also applies central action normalization, capability checks, execution policy, process identity checks, and host/window shortcut restrictions before actions reach the platform adapter.

Some actions that require unrestricted coordinate/global input are intentionally unavailable in safe mode. Legacy physical mode exists as an explicit compatibility escape hatch; use a Capsule instead when unrestricted GUI input is genuinely required for production testing.

---

## Failure Capsules

Supported scripted `argus run` Capsule failure paths can opt into retaining the failed VM as a **forensic Failure Capsule**.

Retention preserves disk/config evidence, not a resumable authenticated live session. Secure Failure Capsules do **not** write active recovery bearer tokens or TLS private keys back into the retained evidence simply to make the VM remotely controllable later.

Successful scripted runs and failures without retention remain ephemeral. Capsule-backed `argus roam` findings are also currently torn down without invoking failure retention; wiring roam findings into the retention lifecycle is future runtime work rather than behavior claimed by this documentation PR.

See [Hyper-V Capsules](docs/capsules-hyperv.md) for configuration and lifecycle details.

---

## Explicit staging and artifact collection

Capsules do not depend on shared folders or clipboard transfer. File movement is explicit and policy-controlled:

- the test specification declares staging/collection authority;
- host sources are constrained to authorized roots;
- guest paths are canonicalized and validated;
- transfers are bounded and hashed;
- collection happens before destructive teardown;
- failures during collection can participate in Failure Capsule retention policy.

See the Capsule guides for the exact currently supported configuration syntax and security boundary.

---

## Providers and budgets

Configure `.argus/config.yaml`:

```yaml
provider: ollama

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
  time_minutes: 10
  max_tokens: null
```

Environment overrides include `ARGUS_PROVIDER`, `ARGUS_MODEL`, `ARGUS_API_KEY`, and `ARGUS_BASE_URL`.

Argus auto-detects model vision capability. Text-only models can fall back to structured observations where supported.

---

## Documentation and roadmap

### ATES — Argus Test Evidence Specification

**Planned / specification stage.**

[ATES](docs/ates.md) defines the next documentation foundation for Argus: an always-on canonical evidence stream for every run, with stable run/step/action/observation/assertion identities, stable event IDs and monotonic per-run sequencing, failure and explicit-checkpoint evidence, provenance, artifact hashes, requirement traceability, append-oriented audit history, and optional approvals.

Reports will be derived from that evidence rather than becoming the source of truth themselves. Hashes provide integrity/corruption detection; a package is only described as tamper-evident when its final manifest is independently bound by a signature, trusted external digest, immutable storage boundary, or equivalent mechanism.

ATES is designed as an Argus-native open standard. Future ISO/IEC/IEEE or regulated-industry mappings are optional compatibility layers and will not be required for normal Argus operation.

### Argus Fleet

**Planned / specification stage.**

[Argus Fleet](docs/fleet.md) extends Capsules across multiple physical machines. A central Control Center will enroll Node Agents, schedule Capsule sessions, aggregate ATES events, and expose a **strictly read-only Observer** interface for watching live tests without sending mouse/keyboard/guest-control operations.

Planned architecture:

```text
Control Center
    -> Node Agent
        -> ExecutionEnvironment
            -> Capsule
                -> Adapter
                    -> application under test

              ATES events
                   -> Observer / reports / audit
```

---

## Web dashboard

```bash
pip install "argus-app-testing[serve]"
argus serve
argus serve --host 0.0.0.0 --port 8080
```

The current dashboard shows run history and roam-session summaries. The Fleet Observer described above is a future architecture and should not be confused with the existing dashboard.

---

## Project layout

```text
argus/
  providers/        LLM layer
  adapters/         desktop-gui, cli, browser
  execution/        execution-environment boundary
  capsule/          Capsule providers, guest control, isolation/transfer logic
  engine/           spec parser, runner, free-roam explorer
  serve/            Flask web dashboard
  gui/              native desktop app
  cli.py            command-line entrypoint

docs/
  README.md               documentation hub
  capsules-hyperv.md      Windows/Hyper-V Capsule guide
  capsules-multi-os.md    multi-OS + Linux libvirt guide
  ates.md                 planned evidence specification
  fleet.md                planned distributed execution architecture

tests/              pytest suite
.argus/             project configuration, tests, runs, roam output
```

---

## Architecture history

PRs #8–#14 established the current safety/execution foundation:

1. safe execution policy and semantic Windows input;
2. `ExecutionEnvironment` boundary;
3. Hyper-V Capsule MVP;
4. Failure Capsules;
5. explicit staging/artifact collection;
6. secure Capsule isolation/control plane;
7. multi-OS Capsule providers with Linux libvirt/QEMU/KVM.

The next planned layers are ATES and Argus Fleet.

---

## License

MIT — see [LICENSE](LICENSE).