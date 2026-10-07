# Argus re-architecture specification (Rust + Tauri 2)

Version: **v0.1 / DRAFT for operator review.** Date: 2026-10-07.

Decision record: [ADR-001](tech-stack-decision.md). Functional checklist: [parity inventory](parity-inventory.md). Stabilization requirements carried into this work: [stabilization specification](../stabilization-spec.md) and [issue register](../usability-issues.md).

Keywords MUST, MUST NOT, SHOULD and MAY are used as in RFC 2119.

## 1. Goals and non-goals

Goals:

1. Replace the Python implementation with a Rust workspace that delivers **every** capability in the [parity inventory](parity-inventory.md).
2. Lower resource use where it was measured to matter: no interpreter, no Node driver, no in-process ML framework by default, event-driven UI, smaller live frames, native guest agent ([ADR-001 §2](tech-stack-decision.md#2-what-actually-costs-resources-today)).
3. Keep `pip install argus-app-testing` working, delivering native `argus`/`argus-gui` binaries and the documented Python API.
4. Keep the PR #28 desktop design; make `argus serve` use the same UI.
5. Close ARG-01…ARG-13 as acceptance gates of the new implementation.
6. Leave a clean base for the [Environment Matrix](../environment-matrix.md) and Fleet growth.

Non-goals for this change:

- New user-facing features beyond the parity inventory and the stabilization fixes. The Environment Matrix is built **after** the switch, on the new core.
- Firefox or WebKit automation (operator decision).
- Running Argus on Windows XP, Vista, 7, 8 or 8.1 (§16.1). Testing applications on those systems inside Capsules is a separate, later feature.
- Changing the ATES evidence format, the Fleet wire protocol, or the `.argus/` file formats. Compatibility with existing data is required (§5).

## 2. Architectural invariants carried over

These are requirements, not suggestions. Each one is re-verified at the matching gate in §12.

- The execution chain stays `conversation / test plan → ExecutionEnvironment → Capsule (where requested) → Adapter → target → canonical ATES evidence`. No second execution path, no Capsule-to-local fallback.
- Model output never reaches an adapter without passing the action schema and policy (`actions.py`, `policy.py`).
- Free text in the desktop app and `argus serve` never changes state by itself (PR #28 propose-then-confirm); slash commands and explicit clicks do.
- PR #27 security invariants: immutable image verification, runtime identity, fresh per-generation credentials and TLS, non-admin target user, quarantine/reconnect, cleanup uncertainty fencing, provider ownership, protected bootstrap guard (CAP-07).
- ATES privacy: secrets, provider responses, target stdin/stdout and screenshots follow the existing privacy policy.

## 3. Process model

```text
                ┌────────────────────────── one native process ─────────────────────────┐
argus (CLI) ───►│ argus-core (tokio)                                                      │
argus-gui ─────►│   engine · providers · adapters · knowledge · ATES · execution client  │──► Chromium (CDP, pipe)
argus serve ───►│   argus-api: typed requests + event stream                              │──► target processes (owned, job objects / process groups)
                └──────────────────────────────────────────────────────────────────────────┘──► Capsule VM ──► argus-guest (native)
                         │ Tauri webview (desktop)     │ HTTP + SSE (serve)
                         ▼                             ▼
                      ui/ (PR #28 HTML/CSS/JS, shared by both)
```

- `argus`, `argus-gui` and `argus serve` are front-ends to the **same** core library. The desktop app runs the core in-process. There is no Python and no Node.js anywhere in the runtime.
- Browser automation talks to Chromium directly over the DevTools protocol using `--remote-debugging-pipe` (no open port).
- Every spawned process (target, browser, PTY child) is owned: Windows job object or POSIX process group, with the process start time recorded to avoid PID reuse (CLI-05).
- Heavy optional parts load only when used. Knowledge with an embedding model, Capsule providers and Fleet are separate crates behind Cargo features, and are inert at runtime unless configured.

## 4. Repository layout after the switch

```text
Cargo.toml                 workspace
crates/
  argus-core/              config, test-spec parsing, actions + policy, secrets, budgets, tokens, run history
  argus-protocol/          serde types for every UI/API request, response and event (one source of truth)
  argus-providers/         Ollama, Anthropic, OpenAI-compatible (incl. Gemini); readiness; embedding role
  argus-ates/              event store, runtime evidence, dispatch, privacy, artifacts, finalization, verify, reports
  argus-adapters/          cli (command + PTY/ConPTY), browser (CDP), windows-uia, linux (X11 + AT-SPI)
  argus-engine/            scripted runner, agent loop, roam, findings, regression drafts, watch
  argus-knowledge/         state graph, embedded vector index, embedding backends, optional Qdrant
  argus-execution/         ExecutionEnvironment, local, capsule client, staging/collect
  argus-capsule/           Hyper-V, isolated Hyper-V, libvirt, bootstrap/ISO 9660, secure control
  argus-provisioning/      environment definitions, plans, unattended media, runtime bundle, publication
  argus-fleet/             enrollment, identity, heartbeat, placement/fencing, ATES transport
  argus-assistant/         intent routing, grounding rules, slash parser, propose-then-confirm
  argus-api/               the request/event handlers used by desktop and serve
  argus-cli/      (bin)    argus
  argus-desktop/  (bin)    argus-gui (Tauri 2)
  argus-server/            argus serve (axum: static UI + /api + SSE)
  argus-guest/    (bin)    Capsule guest agent + target worker
  argus-py/                PyO3 module for the documented Python API
ui/                        PR #28 frontend, moved from argus/gui/web/ (fonts included)
python/argus/              thin Python package: PyO3 extension + console entry points
tests/conformance/         black-box suites run against the CLI, the API and golden data
tests/golden/              fixtures exported from the Python implementation
legacy/python/             the Python implementation, kept read-only until G-RETIRE, then deleted
packaging/                 existing Windows / macOS / Linux packaging, re-pointed at the native binaries
docs/
```

During migration (§12) the Python package stays at `argus/` so current releases continue unchanged. The move to `legacy/python/` happens at the switch.

## 5. Compatibility contracts

C-01. Existing `.argus/config.yaml` and `*.test.yaml` files MUST load with the same meaning. Unknown keys keep today's behaviour (warn or reject exactly as today). Corrections are proposed, never silently rewritten (CFG-07).

C-02. Existing run history, token usage, saved conversations and knowledge graph files MUST load and display identically.

C-03. **ATES:** evidence written by the Python implementation MUST verify under the Rust implementation, and evidence written by Rust MUST verify under the last Python release. Canonical encodings, hashes, signatures and version strings stay identical. This is checked with golden stores exported from Python (`tests/golden/ates/`).

C-04. **Fleet:** all `argus-fleet-*-v1` identifiers and message formats stay identical, so a fleet can mix Python and Rust nodes during migration.

C-05. **Capsule guest protocol:** the host must keep working with images whose guest runtime is the Python PyInstaller bundle, until the operator republishes images with the native `argus-guest`. The runtime bundle manifest gains a runtime kind (`python-pyinstaller` | `native`) under the same approval and digest rules. No approval is inferred from the kind. Argus never republishes an image by itself, and there is no end date for `python-pyinstaller` images; §16.3 says when republishing happens.

C-06. The secrets store keeps its location, format and `SECRET://ARGUS/NAME` references.

C-07. CLI commands, options and exit codes stay as listed in the parity inventory. Human-readable output may change wording but MUST keep the same information.

## 6. Adapters

### 6.1 CLI (closes ARG-04, ARG-05)

- **Command mode:** explicit argv, working directory, environment policy, timeout, bounded stdout/stderr captured independently, exit code. Normal non-zero exit is not a crash (CLI-01, CLI-07).
- **Interactive mode:** an owned PTY (POSIX) or ConPTY (Windows 10 version 1809 or later, see §10.1) session with operations `send`, `read_until(text | prompt | timeout)`, `status`, `close`. Output is bounded and streamed into ATES under the privacy policy (CLI-02…06).
- A launch with no arguments is classified as interactive or rejected with guidance. It never hangs until classified as a crash (CLI-03).
- Inside a Capsule the same operations run in `argus-guest` over the authenticated control channel. There is never a separate shell channel (CLI-06).

### 6.2 Browser over CDP (closes ARG-06, ARG-07, ARG-13)

- **Browser discovery order:** configured path → installed Chrome → installed Edge → Argus-managed pinned Chromium (downloaded with consent and verified by hash). A supplied executable path with spaces is used exactly (TGT-02).
- **One session object** owns the browser process, its user-data directory and the CDP pipe. `close()` is idempotent, also cleans up a partially launched session, and never touches other browsers (ACT-07, TGT-04).
- **Observation-scoped element map:** each observation assigns IDs once, and every action resolves IDs through that same map. Typing into an element that cannot be resolved or is not editable is rejected before dispatch and never redirected to the focused element (ACT-01, ACT-02).
- **Effect checks:** after `type`, the element value is read back and recorded. A mismatch is a failed action, not a success (ACT-06).

### 6.3 Windows desktop (closes ARG-08)

- UI Automation through COM (`windows` crate). Semantic patterns (Invoke, Value, Toggle, ExpandCollapse, SelectionItem, Scroll) are discovered at observation time. Only elements with a supported pattern are advertised as actionable for that operation (ACT-03).
- Safe semantic input stays the default. Physical input stays an explicit opt-in.
- Proven pre-dispatch rejection allows re-observation. Post-dispatch uncertainty keeps ATES fencing (ACT-04).

### 6.4 Linux desktop

X11 screenshots and input (`x11rb`) as today, with AT-SPI (`atspi`/`zbus`) added for semantic element discovery where the target exposes it.

## 7. Knowledge store

Today the default local backend pulls in chromadb or sentence-transformers/PyTorch. The new design:

- **Graph:** the same JSON graph files, read and written natively (C-02).
- **Vector index:** embedded in Argus (a pure-Rust HNSW/flat index stored next to the graph). No database process.
- **Embedding backend** is explicit configuration (CFG-05):
  - `none`: graph-only, **the default for new projects** (§16.2), shown as "graph only" in the UI;
  - `ollama`: uses Ollama's embedding API with the configured embedding model (fixes ARG-09);
  - `onnx`: in-process ONNX Runtime with a small quantized sentence model, downloaded only on explicit consent and verified by hash;
  - `qdrant` / `external`: today's remote options stay available, including `argus knowledge docker up/down/status`.
- When the project's provider is Ollama and the server is reachable, `argus init`, `argus providers` and the desktop app **offer** `ollama` embeddings with a named embedding model. The offer is a proposed change (propose-then-confirm); the backend changes only after the operator confirms it.
- Vector records store backend, model and dimension. A mismatch requires an explicit re-index and is never mixed silently (CFG-06).

## 8. `argus serve` uses the same UI

- `argus-server` (axum) serves `ui/` and exposes the same API as the desktop bridge: `POST /api/<method>` for requests, `GET /api/events` (SSE) for job, live-frame and watch updates.
- The UI talks to a single transport shim (`ui/api.js`). It selects Tauri `invoke` in the desktop app and HTTP + SSE in the browser. The rest of the PR #28 code is unchanged apart from replacing polling with events. The dashboard therefore looks and behaves exactly like the desktop app, which resolves the theme mismatch.
- **Security (new requirement):** today's Flask dashboard accepts `POST /api/roam/start` with no authentication, and the README suggests `--host 0.0.0.0`. The new server MUST require a per-launch access token (printed once, stored only in a cookie scoped to the origin) for every API call, MUST check `Origin`, and MUST refuse to bind a non-loopback address unless the operator passes an explicit flag. Propose-then-confirm applies exactly as on the desktop.

## 9. Desktop app (Tauri 2)

- Hosts `ui/` with the system webview (WebView2 on Windows, WKWebView on macOS, WebKitGTK on Linux), the same engines pywebview uses today. The window, project selection, per-project windows and close protocol behave as in PR #28.
- **No polling.** The 150 ms job poll and the 700 ms follow-up timer are replaced by core events (`job.updated`, `job.finished`, `live.frame`, `watch.event`). A reconcile call remains for close and restart (PR #28 close-time reconciliation).
- **Live view:** frames are downscaled to the panel size and sent as WebP/JPEG. They are only sent while the live panel is visible, and an unchanged frame is skipped by comparing a cheap hash.
- Tauri capabilities restrict the webview to the Argus API. No shell or filesystem plugins are exposed to the UI.

## 10. Python packaging and API

- PyPI name stays `argus-app-testing`. The release **switches in place** (§16.4): the first Rust release is the next breaking version of the same package, `0.2.0`, with no pre-release cycle on PyPI. Under the 0.x rule a minor bump is breaking, so anyone who needs the Python line pins `argus-app-testing<0.2`.
- **Platform wheels** (Windows x64/ARM64, macOS universal2, manylinux x86_64/aarch64, musllinux where feasible) built with maturin. They contain:
  - the `argus` and `argus-gui` binaries (maturin `bin`-style scripts on PATH);
  - the PyO3 extension that backs the documented Python API ([parity inventory §10](parity-inventory.md#10-python-api-kept-on-pypi-operator-decision)).
- The sdist builds from source with a Rust toolchain.
- Optional extras (`[browser]`, `[gui]`, `[knowledge]` …) become no-ops that still install, so existing install commands keep working. The capabilities are built into the binary and enabled by configuration.
- `python -m argus` keeps working, by executing the binary.
- The provisioning Python API is a thin, typed wrapper. Errors map to the same exception names (`ProvisioningError`, `ProvisioningCleanupError`).

### 10.1 Supported operating systems (§16.1)

| Component | Minimum |
|---|---|
| Windows host: `argus`, `argus-gui`, `argus serve` | **Windows 10** (any release, including LTSB/LTSC), Windows 11, Windows Server 2016 or later; x64 and ARM64 |
| Interactive CLI mode on Windows (ConPTY, §6.1) | Windows 10 version 1809, Windows Server 2019 or later. On older Windows 10 builds every other feature works, including CLI command mode; preflight rejects an interactive-mode test before it starts and names the required version |
| Hyper-V Capsules | as above, on an edition with Hyper-V (Pro, Enterprise, Education or Server). Preflight checks the Hyper-V features a Capsule needs (for example a virtual TPM for Windows 11 guests) by probing them, not by trusting a version number (ARG-02) |
| Windows desktop app | also needs the WebView2 Evergreen runtime (included in Windows 11; the installer bootstraps it on Windows 10) |
| macOS | 10.15 or later (Tauri 2 minimum), universal2 |
| Linux CLI and `argus serve` | glibc 2.28 or later (`manylinux_2_28`), x86_64 and aarch64 |
| Linux desktop app | WebKitGTK 4.1 (Ubuntu 22.04, Debian 12, Fedora 36 or later) |
| Native guest agent (`argus-guest`) | the guest systems that image provisioning already supports (Windows 11 23H2/24H2 unattended profiles, the pinned Ubuntu profiles) |

Windows 10 is the minimum because it is the oldest Windows that Rust's standard targets and WebView2 both support. TLS uses `rustls` (§14), not the Windows TLS stack, so model providers, Capsule control and Fleet work the same on every supported Windows build. Microsoft no longer supports most Windows 10 releases; Argus still runs on them, but bugs that occur only there are fixed on a best-effort basis.

Why Windows XP, Vista, 7, 8 and 8.1 cannot be supported:

- Rust's standard Windows targets need Windows 10 or later. Windows 7 and 8 were dropped in Rust 1.78 (2024) and now have only unsupported tier-3 targets. No Rust target supports XP.
- Tauri 2 needs WebView2. Microsoft stopped WebView2 support for Windows 7, 8 and 8.1 in 2023, and it never supported XP.
- XP has no usable TLS 1.2 or 1.3 stack, so it cannot reach model providers, verify Capsule TLS or take part in Fleet.
- The Chromium-family browser adapter needs a current Chrome or Edge. Chrome stopped supporting XP at version 49 (2016) and Windows 7 at version 109 (2023).
- The current Python product cannot run there either: Python 3.10, its minimum, needs Windows 8.1 or later.

Testing applications that run on old Windows is a different question from running Argus there. It would be a Capsule *guest* feature: a provisioning definition for the old system, plus a small legacy guest agent built separately (for example with the tier-3 `*-win7-windows-msvc` targets, or in C for XP). It is not part of this change. It needs its own specification once the operator asks for it.

## 11. Performance budgets and how they are measured

Baselines are the Python numbers in [ADR-001 §2](tech-stack-decision.md#2-what-actually-costs-resources-today). Targets are acceptance gates (G-PERF) checked on Windows 11 x64 and Ubuntu 24.04 x64. A Windows 10 test host (LTSC 2016, version 1607, the oldest Windows 10 release still under Microsoft support when this was written) runs the conformance suite to prove the minimum (§10.1). G-PERF is measured with a committed benchmark harness. The harness samples whole-process-tree RSS at 50 ms and reports median of 5 runs.

| Scenario | Python baseline | Target |
|---|---|---|
| `argus --help`, warm | ~150 ms (import alone) | ≤ 30 ms |
| CLI test run (two assertions) | 0.67 s, 48 MB | ≤ 0.2 s, ≤ 15 MB |
| Browser test, one assertion | 504 MB tree; Node 84 MB, Python 57 MB | no Node process; Argus ≤ 25 MB; tree ≤ Chromium + 25 MB |
| Knowledge, graph only | – | ≤ 10 MB extra |
| Knowledge, local embedder | chromadb path 271 MB; torch path not measured | `onnx` backend ≤ 120 MB extra; `ollama` backend ≤ 10 MB extra (model runs in Ollama) |
| Desktop app idle (window open, no job) | **to be measured** on Windows before work starts | backend ≤ 30 MB; no timers firing; webview as measured |
| Desktop app during a run | **to be measured** | no periodic polling; live frames only while visible |
| Capsule guest runtime | PyInstaller bundle (size to be recorded) | native binary ≤ 15 MB; idle ≤ 10 MB |
| Wheel size per platform | – | ≤ 40 MB (excluding optional downloaded Chromium/ONNX model) |

If a target proves impossible, the gate report MUST state the measured value and the reason. It is not silently relaxed.

## 12. Migration plan

The Python release line stays the shipped product until G-SWITCH. Rust components are built in `crates/` alongside it and released as a **preview** binary (`argus-next`) for operator testing. Each phase ends with a gate report: implemented requirements, tests, measured results and remaining gaps (same rule as the stabilization specification).

| Phase | Scope | Exit gate |
|---|---|---|
| **P0 Foundations** | Cargo workspace, CI for Windows/macOS/Linux × x64/ARM64, maturin wheels, benchmark harness, golden fixtures exported from Python (configs, specs, run history, conversations, knowledge graphs, ATES stores, Fleet messages), black-box conformance runner. **Measure the desktop-app and guest-runtime baselines on Windows.** | G-FOUND: CI green on every target; golden fixtures committed; baselines recorded |
| **P1 Core** | `argus-core`, `argus-providers`, readiness and embedding roles | G-CORE: configs/specs/history load identically (C-01, C-02); A01 provider-setup gate; CFG-01…04 |
| **P2 Evidence** | `argus-ates` | G-ATES: C-03 both directions on all golden stores; ATES test vectors pass |
| **P3 Adapters** | CLI command + interactive, browser CDP, Windows UIA, Linux | A05–A09, A16 from the stabilization spec; ARG-03…08, ARG-13 closed |
| **P4 Engine and knowledge** | runner, agent, roam, findings, regression drafts, watch, knowledge | A02, A10–A12; ARG-07, ARG-09…12 closed; CLI-level parity for `run`, `roam`, `watch`, `report`, `tokens`, `knowledge`, `secrets`, `init`, `providers` |
| **P5 UI** | `argus-assistant` (port of intents, grounding, slash parser, propose-then-confirm, with the phrasing sweep as data), `argus-api`, Tauri desktop, `argus serve` | G-UI: every bridge method and intent in parity §11; phrasing sweep and confirmation suites pass; serve auth (§8); PR #28 visual comparison approved by the operator |
| **P6 Capsules and provisioning** | `argus-execution`, `argus-capsule`, `argus-guest`, `argus-provisioning` | A03, A04 (native hosts); C-05 with both runtime kinds, using a native-agent **test** image built by the conformance suite (operator images are republished only as §16.3 says); ARG-02 closed; CAP-01…07 |
| **P7 Fleet** | `argus-fleet` | C-04 mixed-fleet test (Python node + Rust node) |
| **P8 Python API and switch** | `argus-py`, packaging re-pointed at native binaries, docs | G-PARITY: every row in the parity inventory checked; G-PERF met or explained; **G-SWITCH**: operator approval, then `argus-app-testing` `0.2.0` releases the Rust implementation in place (§16.4) |
| **P9 Retire** | Python implementation removed from the tree after one release of overlap | G-RETIRE: no open parity gaps and no regression reports from the overlap release |

The Environment Matrix ([environment-matrix.md](../environment-matrix.md)) is implemented after G-SWITCH, on the new core. Its own prerequisite gate is unchanged.

### Mapping of stabilization issues to phases

| Issue | Phase | Component |
|---|---|---|
| ARG-01 provider readiness | P1 | argus-providers |
| ARG-02 Capsule preflight | P6 | argus-capsule / argus-provisioning |
| ARG-03 exact executable paths, Chrome | P3 | argus-adapters (target model), browser discovery |
| ARG-04 interactive CLI | P3 | argus-adapters::cli (PTY/ConPTY) |
| ARG-05 adapter identity in failures/regressions | P3–P4 | adapters + engine |
| ARG-06 browser element mapping | P3 | argus-adapters::browser |
| ARG-07 useful browser Roam | P4 | engine + browser |
| ARG-08 UIA actionability | P3 | argus-adapters::windows |
| ARG-09 embedding backend | P1/P4 | providers (role) + knowledge |
| ARG-10 placeholder drafts | P4 | engine (drafts) + assistant |
| ARG-11 regression stubs | P4 | engine |
| ARG-12 honest reporting | P4–P5 | engine + ATES reports + UI |
| ARG-13 browser teardown | P3 | argus-adapters::browser |

## 13. Testing strategy

- **Rust unit and property tests** inside each crate. This includes fuzzing of every parser that reads untrusted input: action JSON, test specs, CDP messages, guest responses, evidence files.
- **Golden fixtures** exported once from the Python implementation (P0), committed under `tests/golden/`, and used for C-01…C-05.
- **Conformance suite** (`tests/conformance/`): black-box tests that drive the `argus` binary and the API over its public interfaces. They run against both implementations while both exist. A test that passes on Python and fails on Rust is a parity bug.
- **Porting the existing 126 Python test files:** each Python test is either ported to Rust, turned into a data-driven conformance case (the phrasing sweep, slash round-trips and intent grounding are already data-shaped), or listed as covered by an equivalent test. The mapping is a tracked checklist; no test is dropped silently.
- **Native acceptance** (Hyper-V, libvirt, Windows UIA, ConPTY, installed Chrome/Edge) runs on real hosts as in the stabilization gates. Mocks never stand in for them.

## 14. Security requirements specific to the rewrite

- Unsafe Rust is limited to FFI boundaries (Windows COM/Win32, PTY, ONNX Runtime) in small, reviewed modules marked with `// SAFETY:` comments; `#![forbid(unsafe_code)]` everywhere else.
- Cryptography uses audited crates (`rustls`, `ring` or `aws-lc-rs`, `ed25519-dalek`). No hand-written primitives. Constant-time comparisons where the Python code uses them.
- Dependency policy: `cargo-deny` (licenses, advisories, duplicate versions) and `cargo-audit` in CI, with lockfile committed; release builds are reproducible from the tag.
- The webview gets no ambient authority (Tauri capabilities). `argus serve` requires the access token (§8).

## 15. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Rewrite takes longer than planned | Delayed fixes for users | Python line keeps critical fixes; phases are independently useful through the `argus-next` preview |
| ATES byte-compatibility subtleties | Evidence fails to verify | G-ATES golden stores in both directions before anything else depends on ATES |
| Young Rust CDP crates | Browser instability | Argus-owned `BrowserSession` trait; thin internal CDP client if a crate falls short |
| Windows COM/ConPTY edge cases | Desktop/CLI regressions | Early P3 native tests on real Windows hosts; ARG-04/ARG-08 gates |
| Losing Firefox/WebKit | Users of non-Chromium engines | Operator-accepted; documented in the release notes |
| Python API users beyond the documented surface | Broken scripts | Deprecation notice in the last Python release; documented replacement via CLI/JSON |

## 16. Operator decisions on the open questions (2026-10-07)

1. **Minimum OS (P0).** The operator asked whether Windows support could reach back to XP. It cannot, for the reasons in §10.1. The operator then set the minimum to **Windows 10** (any release) and Windows Server 2016, on x64 and ARM64. The one exception is the interactive CLI mode, which needs ConPTY and therefore Windows 10 version 1809 or later; on older builds it is rejected at preflight with a clear message. The other platforms are listed in §10.1. Testing applications on old Windows inside Capsules is possible later as a separate feature, and only when the operator requests it.
2. **Default embedding backend (P4).** The operator had no preference. The default for new projects is `none` (graph only): it needs no download and no running server, and it reports its limits honestly. When an Ollama provider is configured and reachable, Argus offers `ollama` embeddings as a confirmed change (§7). It never switches silently.
3. **Republishing Capsule images with the native guest agent (P6).** Images are republished in two cases only:
   - the operator asks for it, by provisioning an image with the `native` runtime kind through the provisioning API;
   - a test needs a capability that only the native agent advertises, such as an interactive terminal inside a Capsule (ARG-04). Preflight then fails before anything starts, names the missing capability and the image, and shows the exact command that republishes it. It never republishes automatically and never falls back to local execution.

   Until then, `python-pyinstaller` images keep working with no end date (C-05). The P6 conformance suite builds its own native-agent test image, so the gate does not depend on the operator's images.
4. **PyPI release (P8).** The release switches in place: same package, `argus-app-testing` `0.2.0`, with no PyPI pre-release cycle (§10). Before the switch, operator testing uses the `argus-next` preview binaries attached to GitHub releases. The last Python release (`0.1.x`) carries the deprecation notice for the internal Python imports that are not kept (§15).
