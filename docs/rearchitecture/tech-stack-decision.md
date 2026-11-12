# ADR-001: Rebuild Argus on Rust + Tauri 2

Status: **Accepted by the operator (2026-10-07). Specification only; no runtime change yet.**

Related: [re-architecture specification](specification.md), [functional parity inventory](parity-inventory.md), [stabilization specification](../stabilization-spec.md), [usability issue register](../usability-issues.md).

## 1. Problem

Argus is a single Python package (about 47,000 lines of Python in `argus/`, plus 1,700 lines of desktop UI and 34,000 lines of tests). In operator testing it felt heavy on system resources. The operator asked for one deliberate technology change that:

- keeps every existing capability (nothing is removed);
- keeps `pip install argus-app-testing` working where possible;
- may restructure the whole repository;
- also fixes the `argus serve` dashboard, whose look does not match the PR #28 design.

This is a one-time decision, so it was made from measurements rather than from the assumption that "Python is slow".

## 2. What actually costs resources today

Measured on `main` at `8e7ebe5` (PR #28 merged), Linux x86_64 cloud sandbox, Python 3.11, warm file cache. Peak resident memory (RSS) was sampled every 50 ms across the whole process tree.

| Workload | Wall time | Peak memory (whole tree) | Where the memory is |
|---|---|---|---|
| `python -c pass` | – | 10 MB | interpreter |
| Import `argus.cli` (warm) | ~150 ms | 42 MB | `argus.config` → providers → `requests` dominate import time |
| Import `argus.cli` (first run, cold) | ~2.0 s | – | bytecode compilation |
| `argus run` – CLI test (two assertions) | 0.67 s | **48 MB** | Python only |
| `argus run` – one browser assertion | 7.0 s | **504 MB**, 8 processes | Chromium `headless_shell` 363 MB (72%), Playwright Node driver 84 MB (17%), Python 57 MB (11%) |
| Knowledge extra, chromadb path | – | 520 MB on disk; **271 MB** after one embedding call | chromadb + ONNX Runtime + downloaded MiniLM model (167 MB cache) |

Not measured here, and marked as such:

- **Desktop app** (`argus-gui`, pywebview + WebView2/WebKitGTK): no webview is available in the sandbox. It must be measured on Windows before and after (see the [specification](specification.md#11-performance-budgets-and-how-they-are-measured)).
- **`sentence-transformers` + PyTorch**: the CPU wheel index is blocked by the sandbox proxy, and the default PyPI build pulls several GB of CUDA libraries. This path is known to be larger than the chromadb path above, but no number is claimed.
- **Qdrant in Docker**: no Docker daemon in the sandbox.

Design costs found in the code, independent of language:

- The desktop UI polls the backend every 150 ms while a job is active (`app.js` `poll()`), serialising job state through the pywebview bridge each time.
- Live view moves full-size PNG screenshots, base64-encoded, through the same bridge.
- Browser automation always starts a separate Node.js process (the Playwright driver) next to Chromium.
- Optional knowledge storage loads a full ML runtime (PyTorch or ONNX via chromadb) inside the Argus process, or starts a Qdrant container.
- The Capsule guest runtime is a PyInstaller bundle of a Python interpreter.

**Conclusion.** The Python interpreter itself is about 10–20% of the cost of a browser run. Swapping the language alone would not fix what the operator saw. The win comes from changing the language **and** the architecture together: no interpreter, no Node sidecar, no in-process ML framework by default, push events instead of polling, smaller live frames, and a native guest agent. The chosen stack has to make those architectural changes natural.

## 3. Requirements the stack had to meet

1. Native, small, fast-starting binaries on Windows, Linux and macOS, x64 and ARM64 (the current release matrix).
2. Keep the PR #28 desktop design. Reuse the existing HTML/CSS/JS instead of redrawing it.
3. First-class Windows UI Automation (COM), Linux AT-SPI/X11, process ownership and job objects.
4. Interactive terminals: ConPTY on Windows and PTY on POSIX (ARG-04).
5. Chromium automation without a Node.js driver (operator decision: Chromium family only).
6. Local embeddings and vector search without PyTorch.
7. Strong cryptography and TLS for ATES evidence, Capsule control and Fleet.
8. Memory safety: Argus parses untrusted guest output, model output and evidence files.
9. Ship through `pip install` (binaries inside wheels), plus a small Python API.
10. Concurrency for the planned Environment Matrix scheduler and Fleet.

## 4. Options considered

| | Rust + Tauri 2 | Go + Wails | .NET + WebView2 | TypeScript + Electron / Node | Python, re-architected |
|---|---|---|---|---|---|
| Runtime overhead | none (native) | small GC runtime | CLR runtime | Node + bundled Chromium (heaviest) | interpreter, ~50 MB per process |
| Desktop shell reusing PR #28 UI | Tauri 2, stable, system webview | Wails v2 stable; v3 still alpha in 2026 | WebView2 on Windows; weak on Linux | Electron bundles its own Chromium | pywebview (today) |
| Windows UIA | official `windows` crate, `uiautomation` | go-ole, manual COM | native, best on Windows | node-ffi / addons | pywinauto (today) |
| Linux AT-SPI / X11 | `atspi`, `zbus`, `x11rb` | dbus, xgb | weak | weak | python-xlib (today) |
| ConPTY / PTY | `portable-pty` (from WezTerm), `conpty` crates | creack/pty (POSIX), separate ConPTY libraries | built-in ConPTY | node-pty | pywinpty / ptyprocess |
| Chromium without Node | CDP crates (`chromiumoxide`, newer Playwright-style crates), or Argus-owned CDP client | `chromedp` (mature) | CDP libraries | Playwright (needs Node anyway) | CDP clients exist |
| Local embeddings | `ort` (ONNX Runtime), `fastembed-rs`, `candle` | onnxruntime-go (cgo) | ONNX Runtime .NET | onnxruntime-node | ONNX Runtime / torch |
| pip distribution | **maturin `bin` wheels** (how ruff and uv ship) + PyO3 | hand-rolled binary wheels | hand-rolled | hand-rolled | native |
| Memory safety without GC | yes | GC | GC | GC | GC |
| Main cost | steepest learning curve, longer compiles, young CDP ecosystem | weaker desktop/UIA/ML story | Linux and macOS automation | heaviest result | keeps the interpreter and the PyInstaller guest |

## 5. Decision

**Rebuild Argus as a Rust workspace, with Tauri 2 for the desktop app**, under these operator decisions (2026-10-07):

1. **Stack:** Rust (tokio) for every component, including the Capsule guest agent and the Fleet node agent. Tauri 2 hosts the existing PR #28 frontend.
2. **pip scope:** `pip install argus-app-testing` keeps installing the `argus` and `argus-gui` commands (native binaries inside platform wheels built with maturin). A small, documented Python API is kept through PyO3, covering the scripting entry points in [docs/os-environment-provisioning.md](../os-environment-provisioning.md). Internal module imports such as `argus.ates.*` and `argus.engine.*` are **not** preserved.
3. **Browsers:** Chromium family only (Chrome, Edge, Chromium) over the Chrome DevTools Protocol. Argus prefers an installed Chrome/Edge and downloads a pinned Chromium only when none is found. No Node driver.
4. **Sequencing:** the PR #29 stabilization issues (ARG-01…ARG-13) become acceptance gates of the new implementation instead of being fixed twice. The current Python release receives only critical fixes until the switch (security vulnerabilities and defects that can produce a false pass or wrong verdict; defined in [specification §18, R-7](specification.md#18-amendment-b--reconciliation-decisions), which also names the two fixes backported to the Python line, so ARG-06 is the one issue fixed in both lines).

The specification's open questions were answered the same day ([specification §16](specification.md#16-operator-decisions-on-the-open-questions-2026-10-07)). The minimum Windows host is Windows 10 (any release) or Windows Server 2016, with interactive CLI mode needing Windows 10 1809 or later. XP, Vista, 7, 8 and 8.1 cannot run a Rust/Tauri 2 build (specification §10.1). New projects default to graph-only knowledge. Capsule images get the native agent only on request or when a test needs it. PyPI switches in place at `0.2.0`.

## 6. Consequences

Positive:

- One native process for CLI runs. The Node driver disappears from browser runs, and the PyInstaller interpreter disappears from Capsule guests.
- The same UI code serves the desktop app (Tauri) and `argus serve` (HTTP), so the dashboard finally matches the PR #28 design.
- Typed protocol definitions shared by desktop, server, guest agent and Fleet node.
- A natural home for the Environment Matrix scheduler (async, bounded resources).

Negative, and how the plan handles each:

- **Rewrite size and risk.** About 47,000 lines, much of it security-critical (ATES, Capsule, Fleet). Mitigated by a phased plan with parity gates, golden fixtures exported from the Python implementation, and a black-box conformance suite (see the [specification](specification.md#12-migration-plan)).
- **Young Rust CDP ecosystem.** Argus owns a thin CDP layer behind its own `BrowserSession` trait, so it can use or replace a crate without touching the engine.
- **Python import compatibility ends** for internal modules. Only the documented Python API is kept, by operator decision.
- **Contributor skill set.** Rust experience is needed. The Python test suite remains useful as executable documentation while the conformance suite is built.
- **Chromium only.** Firefox and WebKit users (via Playwright's `browser_type`) lose those engines. The operator accepted this; the default was already Chromium.

## 7. Sources

- Tauri 2 vs alternatives, 2026: [digitalapplied.com decision matrix](https://www.digitalapplied.com/blog/desktop-apps-web-stack-tauri-electron-deno-wails-2026), [tech-insider.org Tauri vs Electron 2026](https://tech-insider.org/tauri-vs-electron-2026/)
- maturin `bin` bindings (binaries in wheels): [maturin.rs/bindings](https://maturin.rs/bindings), [PyO3/maturin](https://github.com/PyO3/maturin)
- Rust Chromium/BiDi automation: [ferridriver](https://docs.rs/crate/ferridriver/0.5.0), [rustenium](https://docs.rs/crate/rustenium/latest), [Rustwright (alpha)](https://www.skyvern.com/blog/rustwright-playwright-rewritten-in-rust-that-occupies-70-less-memory-and-is-2-55x-faster/)
- Local embeddings: [fastembed-rs](https://github.com/congphuong/fastembed-rs) (ONNX Runtime via `ort`)
- Windows UI Automation and Linux AT-SPI in Rust: [uiautomation-rs](https://gittrend.io/repo/leexgone/uiautomation-rs), [atspi-common](https://rust-digger.code-maven.com/crates/atspi-common)
- Interactive terminals: [portable-pty](https://openapps.pro/packages/portable-pty), [conpty-oxide](https://docs.rs/crate/conpty-oxide/0.1.2)

Crate choices are reviewed again at the start of each phase. The specification depends on Argus-owned traits, not on these crates.
