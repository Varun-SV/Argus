# P0 gate report: G-FOUND

Phase P0 "Foundations" of the [migration plan](specification.md#12-migration-plan) closes at
gate **G-FOUND**: "CI green on every target; golden fixtures committed; baselines recorded". This
report lists every P0 deliverable, what was checked and where, and what is still open. Per §11, a
target that cannot be met is reported with its measured value and the reason; nothing is
relaxed silently.

**Verdict: G-FOUND is not yet passed.** Everything that can be built and checked on Linux and in
CI is in place. The Windows baselines (desktop app and guest runtime) need the operator's Windows
host and are **blocked here**. P1 should not start until they are recorded, or until the
operator explicitly accepts starting with them open.

## Deliverables

| P0 item | State | Evidence |
|---|---|---|
| Cargo workspace | Done | `Cargo.toml`, `crates/argus-{core,protocol,cli,py,bench}`, `tests/conformance`. Exact-pinned dependencies, `Cargo.lock` committed, `unsafe_code = "forbid"` except the PyO3 crate |
| Lint and dependency policy | Done | `cargo fmt --check`, `cargo clippy -D warnings`, `cargo deny check` (advisories, bans, licenses, sources) and `cargo audit` all pass; checked locally and in the `lint` job of `.github/workflows/rust-ci.yml` |
| MSRV | Done | `cargo +1.85 check --workspace --all-targets --locked` passes (the `msrv` job) |
| CI on Windows/macOS/Linux × x64/ARM64 | In CI | `rust-ci.yml` `test` matrix: Linux x64 and ARM64, Windows x64 and ARM64, macOS Apple silicon and Intel; builds, tests, a release CLI smoke test and an architecture check per target. Locally on Linux x64: 106 workspace tests pass. The gate needs the PR's CI run green on every row |
| maturin wheels | Done (prototype) | [P0 packaging findings](p0-packaging-findings.md). One wheel carries the `argus-next` binary and the `argus_next._native` extension. `rust-wheels.yml` builds Linux x64/ARM64 (`manylinux_2_28`), Windows x64/ARM64 and macOS universal2, and smoke-tests each in a clean venv. Linux x64 verified locally |
| Benchmark harness | Done | `argus-bench` ([benchmarks](benchmarks.md), [`bench/`](../../bench/README.md)): whole-process-tree sampling at 50 ms, median of 5, attach mode for the desktop app and guest runtime. Tested in `cargo test` |
| Golden fixtures (C-01…C-06) | Done | See the next section. Each set regenerates with `--check` in the `golden` job of `rust-ci.yml` |
| Conformance runner | Done | `tests/conformance`: 46 CLI-surface cases. Python: 44 pass, 2 known-gap (`cli.run.dry-run-shows-step-kind`, `cli.secrets.uppercase-reference`). `argus-next` stub: 3 pass, 43 pending. The Python run is part of the `golden` CI job |
| Baselines on Linux | Done | [`bench/results/baseline-python-linux-x86_64.json`](../../bench/results/baseline-python-linux-x86_64.json): `python-pass`, `python-import-argus-cli`, `python-help`, `python-cli-test-run`, `python-browser-one-assertion`, all `ok` |
| **Baselines on Windows** | **Blocked here** | Desktop app idle, desktop app during a run, and the PyInstaller guest runtime. Procedure: [`bench/windows-baselines.md`](../../bench/windows-baselines.md). `scripts/windows/verify-pr30.ps1` drives the Windows verification run; it has not been executed, because PowerShell and Windows are not available here |

## Golden fixtures

All sets are written by the real Python implementation in a scrubbed child process with
deterministic clocks, identifiers and keys, and checked byte for byte. Generated under Python
3.13 and checked clean under Python 3.12.

| Contract | Directory | Exporter | Content |
|---|---|---|---|
| C-01 config and specs | `tests/golden/config/` | `scripts/golden/export_config.py` | 142 cases: configuration, test specs and programmatic loading |
| C-02 state, C-06 secrets | `tests/golden/state/` | `scripts/golden/export_state.py` | run history, token usage, saved conversations, knowledge files, the secret store (dummy values) |
| C-03 ATES | `tests/golden/ates/` | `scripts/golden/export_ates.py` | three real `argus run` stores (pass, fail, launch error) with the verifiers' decisions, plus 13 tampered copies that must be rejected |
| C-04 Fleet | `tests/golden/fleet/` | `scripts/golden/export_fleet.py` | every signed message kind (canonical bytes, Ed25519 signatures, verification and tampering), one enrollment-to-ATES-transport flow, the Node key file format, all `argus-fleet-*-v1` identifiers |
| C-05 Capsule | `tests/golden/capsule/` | `scripts/golden/export_capsule.py` | a guest runtime bundle and its manifest (including the `runtime_kind` rejection C-05 expects of the Python line), a bootstrap attempt, the host and guest control records |

The C-05 bootstrap certificate is the one input that cannot be reproduced (ECDSA signing is
randomized), so it was generated once by the real code and is reused; see
`tests/golden/capsule/README.md`.

## Critical fixes on the Python line (R-7)

| Fix | State |
|---|---|
| SEC-1, `argus serve` lockdown ([§8.1](specification.md#81-critical-fix-in-the-python-line-now)) | Shipped in PR #31 (37 tests) |
| ARG-06, browser typing reports false success | In this PR: `argus/adapters/browser_adapter.py`; `tests/test_browser_element_mapping.py`, 29 tests, 4 of them against real Chromium, all pass |

## What closes the gate

1. The PR's `rust-ci.yml` and `rust-wheels.yml` runs are green on every target.
2. The operator runs `bench/windows-baselines.md` on a Windows 11 x64 host (and
   `scripts/windows/verify-pr30.ps1`), commits the three Windows baseline results, and replaces
   the "blocked here" rows in [benchmarks](benchmarks.md#windows-baselines-blocked-here).
