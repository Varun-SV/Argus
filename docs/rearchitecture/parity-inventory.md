# Functional parity inventory

Status: **Draft checklist for the Rust re-architecture.** Source of truth: `main` at `8e7ebe5` (PR #28 merged, PR #27 included).

The operator's rule is that **no functionality is removed**. Every row below must be implemented by the Rust version, or explicitly marked *superseded* with the replacement named, before the Python implementation is retired ([specification §12](specification.md#12-migration-plan), gate G-PARITY).

Two operator-approved scope changes are recorded here so they are not mistaken for regressions:

| Area | Python today | Rust target | Why |
|---|---|---|---|
| Browser engines | Playwright `browser_type` (Chromium default; Firefox/WebKit possible) | Chromium family only (Chrome, Edge, Chromium) via CDP | Operator decision, [ADR-001](tech-stack-decision.md) §5 |
| Python imports | Every `argus.*` module importable | Only the documented Python API (§10 below) | Operator decision, ADR-001 §5 |

Legend: **Keep** = same behaviour and same user-visible contract. **Fix** = keep, and also close a listed stabilization issue. **Superseded** = replaced by an equivalent, named.

## 1. Command line (`argus`)

| Command | Options / subcommands | Parity |
|---|---|---|
| `argus init` | – | Keep (same `.argus/` scaffold, never overwrites existing files) |
| `argus run [TEST]` | `--minutes`, `--max-tokens`, `--dry-run` | Keep |
| `argus roam TARGET` | `--minutes`, `--max-tokens`, `--no-regressions`, `--adapter`, `--memory/--no-memory` | Fix (ARG-03, ARG-05, ARG-07) |
| `argus watch` | `--minutes`, `--max-tokens` | Keep (native file watcher) |
| `argus serve` | `--host`, `--port`, `--debug`; new `--allow-remote` | **Fix**: serves the PR #28 UI over HTTP instead of the Flask templates, with a per-launch access token and an `Origin` check (see [specification §8](specification.md#8-argus-serve-uses-the-same-ui)). `--host` and `--port` keep their meaning. Documented behaviour change (C-07): a non-loopback `--host` such as `0.0.0.0` requires `--allow-remote`, and without it the command exits non-zero naming that flag. `--debug` is verbose logging only, with no debugger or reloader |
| `argus gui` / `argus-gui` | – | Keep (Tauri app) |
| `argus providers` | – | Fix (ARG-01: readiness states) |
| `argus report` | `--limit` | Keep |
| `argus tokens` | – | Keep |
| `argus knowledge stats/export/reset` | `TARGET` | Keep |
| `argus knowledge docker up/down/status` | – | Keep (Qdrant stays an optional backend) |
| `argus secrets set/list/remove` | `SECRET://ARGUS/NAME` references | Keep (same per-user store, values never printed) |
| `argus-guest-runtime-build` | – | Superseded by a native guest-agent build target (`cargo build -p argus-guest`), same approved-bundle manifest and digests |

Exit codes, the `rich` console summaries (✓/✗ lines, step durations, summary line) and `--help` text stay equivalent.

## 2. Project files and formats

| Item | Parity |
|---|---|
| `.argus/config.yaml`: `provider`, per-provider blocks (`ollama`, `anthropic`, `openai`), budgets (`time_minutes`, `max_tokens`), `knowledge`, `execution` (`environment`, `capsule.*`) | Keep. The same files load unchanged; compatibility rules from CFG-07 apply. `knowledge` settings map as in [specification §7.1](specification.md#71-migration-of-existing-knowledge-configuration) |
| `*.test.yaml`: `name`, `target` (`adapter`, `launch`), `steps` (natural language + `assert`), `setup`, `teardown`, `retries`, `staging`, `collect` | Keep, including the strict validation of programmatic step metadata |
| Assertions: `text_visible`, `window_title_contains`, `element_exists`, `process_running`, `dialog_open`, `stdout_contains`, `stderr_contains`, `exit_code_is`, `url_contains`, `page_title_contains` | Keep, all ten |
| Run history `.argus/runs/` (result JSON, flat history, `report.md`, allocation-ordered names, project order counter) | Keep. Existing history must load and sort identically |
| Token usage persistence | Keep |
| Desktop conversation store (per-user state root, keyed by project identity, busy-chat retention) | Keep. Existing saved chats must load |
| Knowledge files (`<key>.graph.json`, `states.ndjson`, `bugs.ndjson`) | Keep. Existing graph, states and bugs files must load unchanged. Existing chromadb vector indexes are not loaded: they are reported as "re-index required" and rebuilt from `states.ndjson`/`bugs.ndjson` only after the operator confirms; the chromadb files are never modified or deleted ([specification §7.1](specification.md#71-migration-of-existing-knowledge-configuration)) |

## 3. Action vocabulary and policy

| Item | Parity |
|---|---|
| Actions: `click`, `double_click`, `right_click`, `type`, `key`, `scroll`, `menu`, `wait`, `done`, `navigate`, `run`, `execute`, `report_bug` | Keep, with the same validation (`actions.py`) |
| Canonical key-chord grammar (no backend-specific key syntax from models) | Keep |
| Global execution policy (`policy.py`) | Keep |
| Safe semantic Windows input by default; legacy physical input only as explicit opt-in | Keep |

## 4. Adapters

| Adapter | Today | Parity |
|---|---|---|
| `cli` | Command execution with stdout/stderr/exit code | Fix: command mode **plus** persistent interactive mode (ConPTY/PTY), ARG-04, ARG-05, CLI-01…07. Interactive mode on Windows needs Windows 10 1809 or later (known limitation, [specification §10.1](specification.md#101-supported-operating-systems-161)) |
| `browser` | Playwright Chromium, headless | Fix: Argus-owned CDP session, one observation-scoped element map, owned teardown, prefer installed Chrome/Edge (ARG-06, ARG-07, ARG-13, ACT-01…07) |
| `desktop-gui` on Windows | pywinauto UIA tree, `mss` screenshots, safe semantic input | Fix: UIA through COM, actionability checked before dispatch (ARG-08, ACT-03, ACT-04) |
| `desktop-gui` / `linux-gui` on Linux | X11 or auto-started Xvfb `:99`; `xdotool` input and window title, `scrot` screenshots; coordinates only; X session escape chords blocked | Keep and Fix: native X11 input/screenshots, owned Xvfb on a free display, AT-SPI elements with pre-dispatch actionability, process-group teardown, Wayland guidance ([Linux platform §3](linux-platform.md#3-desktop-gui-adapter)) |
| Target guessing (`adapter_for`) and roam target parsing | Keep, Fix (ARG-03 exact executable paths with spaces) |

## 5. Engine

| Item | Parity |
|---|---|
| Scripted runner (setup/steps/teardown, retries, deterministic assertions, budgets) | Keep |
| LLM agent loop and free-roam (observation → action → validation → dispatch) | Keep |
| Findings, roam report, regression stubs | Fix (ARG-10, ARG-11, ARG-12, DRF-01…02, EVD-01…02) |
| Vision probe per job, assertion-only specs skip the model | Keep |
| Watch mode (re-run on change, owned-file attestation, linked specs ignored) | Keep |
| Budgets: time, tokens (Ollama exempt from token budget) | Keep |

## 6. Providers

| Provider | Parity |
|---|---|
| Ollama (chat, vision, local) | Keep |
| Anthropic | Keep |
| OpenAI-compatible (covers OpenAI, Gemini and other compatible endpoints) | Keep |
| Token tracking per call, per job, per session, per project | Keep |
| Provider readiness | Fix (ARG-01, CFG-01…04: parsed / credential / reachable / model / capability) |
| Embedding role separate from chat role | Fix (ARG-09, CFG-04…06) |
| Local embeddings: `vector_backend: chroma` with `embedding_model: all-MiniLM-L6-v2` (today's defaults; sentence-transformers) | Superseded by the `onnx` backend, enabled only after explicit operator consent to the model download and hash check; until then graph-only with the reason shown. No silent download or backend switch ([specification §7.1](specification.md#71-migration-of-existing-knowledge-configuration)) |
| Remote vectors: `type: docker` or `qdrant`, `vector_url` | Keep (same meaning, `qdrant` / `external` backends) |

## 7. ATES evidence (must stay byte-compatible)

All of ATES v0.1 is kept, and **existing evidence written by the Python implementation must verify unchanged under the Rust implementation** (gate G-ATES in the specification). Fields added by Rust-only or post-switch features are additive, opt-in and version-gated ([specification C-03](specification.md#5-compatibility-contracts)):

- durable ordered event store, run identities (`RunId` etc.), runtime lifecycle evidence;
- durable action dispatch and uncertainty fencing;
- privacy policy, protected references, redaction;
- protected artifacts, collection, commitments;
- transactional finalization, recovery policy, status;
- manifests, packaging and verification;
- derived reports, requirement traceability, approval/audit records.

## 8. Execution environments, Capsules, provisioning

| Item | Parity |
|---|---|
| `ExecutionEnvironment` boundary: `local`, `capsule` (`auto`, `hyperv`, `libvirt`) | Keep |
| Hyper-V Capsules, isolated Hyper-V, libvirt/QEMU/KVM | Keep (libvirt contract and Linux guest rules in [Linux platform §5–6](linux-platform.md#5-libvirt-capsules)) |
| Secure guest control: pinned HTTPS, per-generation credentials, bearer rotation, network isolation, Hyper-V side-channel restrictions | Keep |
| Guest agent, secure guest agent, target worker (non-admin target user), Windows target-user bootstrap | Keep; **native Rust guest binary** replaces the PyInstaller bundle |
| Bootstrap media (ISO 9660 builder, protected delivery guard) | Keep (CAP-07 guard unchanged; Hyper-V protected delivery is an open design, [ADR-002](adr-002-hyperv-protected-bootstrap.md), decided before P6) |
| Explicit staging and artifact collection, safe open/output | Keep |
| Failure Capsule retention, quarantine, reconnect, cleanup uncertainty | Keep |
| OS environment provisioning: definitions, plans, Windows/Ubuntu unattended, runtime bundle, baseline, image publication, provisioning evidence | Keep; Capsule preflight Fix (ARG-02, CAP-01…05) |

## 9. Fleet

Node enrollment, Ed25519 node identity and key files, authenticated heartbeats and capability advertisements, clock assessment, durable placement and fencing, remote Capsule execution, and ATES transport/reconciliation. **Keep**, including these wire/version identifiers so mixed Python/Rust fleets interoperate during migration:

`argus-fleet-registry-v1`, `argus-fleet-bootstrap-sha256-v1`, `argus-fleet-heartbeat-v1`, `argus-fleet-clock-assessment-v1`, `argus-fleet-node-ed25519-v1`, `argus-fleet-node-key-v1`, `argus-fleet-placement-v1`, `argus-fleet-placement-auth-v1`, `argus-fleet-ates-transport-v1`.

## 10. Python API kept on PyPI (operator decision)

The `argus` Python package keeps the scripting surface documented in [os-environment-provisioning.md](../os-environment-provisioning.md), backed by the Rust core through PyO3:

- `argus.capsule.base.CapsuleSettings`
- everything in `argus.provisioning.__all__`: `load_published_derived_image`, `AtesProvisioningRecorder`, `BuildPayload`, `DerivedImageManifest`, `EnvironmentDefinition`, `EnvironmentProvisioner`, `GuestRuntimeBundleBuildResult`, `GuestRuntimeBundleManifest`, `GuestRuntimeIdentity`, `InstallationMediaSource`, `InstallationSpec`, `HyperVProvisioner`, `LibvirtProvisioner`, `MachineSpec`, `ProvisioningCleanupError`, `ProvisioningCleanupState`, `ProvisioningError`, `ProvisioningPlan`, `ProvisioningProviderCapabilities`, `ProvisioningResult`, `VerifiedFile`, `VerifiedGuestRuntimeBundle`, `build_provisioning_plan`, `create_build_payload`, `create_guest_runtime_bundle`, `capsule_settings_from_derived_image`, `derived_image_advertisement`, `environment_definition_from_mapping`, `load_environment_definition`, `validate_provider_capabilities`, `verify_guest_runtime_bundle`, `verify_installation_media`, `verify_provisioning_evidence`, `verify_regular_file`
- `argus.cli.main` and `argus.gui.app.run_gui` as console entry points (they `exec` the native binaries).

The PyO3 surface is versioned and covered by its own tests. Anything not listed is internal.

## 11. Desktop app (PR #28 design)

The PR #28 HTML/CSS/JS, fonts and visual design are kept. The backend bridge keeps every call the UI makes today:

`app_info`, `capture_live`, `check_provider`, `draft_test`, `dry_run`, `environment`, `evidence`, `explain`, `help`, `init_project`, `interpret`, `job_status`, `knowledge`, `knowledge_export`, `knowledge_reset`, `list_tests`, `live`, `load_conversations`, `open_project`, `recent_runs`, `regression_stub`, `run_tests`, `save_conversations`, `save_test`, `set_environment`, `set_memory`, `set_provider`, `set_retain`, `start_roam`, `stop`, `token_usage`, `watch_start`, `watch_status`, `watch_stop`.

The bridge (`argus/gui/app.py`, class `ArgusAPI`) has two more public methods, 36 in total: `read_test` and `live_stats`. **Keep** (not called by the UI at `8e7ebe5`; retained for API parity).

Behaviour kept from PR #28:

- free-text routing to the 18 validated intents (`help`, `stop`, `run`, `dry_run`, `roam`, `write_test`, `save_test`, `explain`, `knowledge`, `evidence`, `report`, `tokens`, `providers`, `switch_provider`, `environment`, `init`, `watch`, `chat`);
- slash commands, including `/run` flags (`--env`, `--retain`), `/roam --env`, `/env --retain`;
- the **propose-then-confirm** rule: free text never changes state; it returns the exact slash command as a chip;
- the grounding rules and the phrasing sweep (`tests/test_gui_phrasing_sweep.py`, `tests/test_gui_confirmation.py`), ported as data-driven conformance cases;
- conversations, live view, recent runs, evidence card, environment/provider/memory/retain pickers, drafts, regression stubs, watch card, knowledge card, project windows.

## 12. Packaging and release

Keep every artifact the release pipeline produces today ([releasing.md](../releasing.md)): Windows portable ZIP, EXE installer and MSI (x64, ARM64); macOS universal2 app and DMG; Linux AppImage, DEB, RPM and Arch packages (x86_64, aarch64); and PyPI distributions (now platform wheels with native binaries, plus an sdist that builds from source with a Rust toolchain).
