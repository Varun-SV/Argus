# Benchmarks: harness, metrics and baselines

This page describes how the performance budgets of
[specification §11](specification.md#11-performance-budgets-and-how-they-are-measured)
(gate **G-PERF**) are measured, and records the P0 baselines. §11 requires: "G-PERF is
measured with a committed benchmark harness. The harness samples whole-process-tree RSS at
50 ms and reports median of 5 runs." That harness is `argus-bench` (`crates/argus-bench`), with
its scenarios, fixtures and results under [`bench/`](../../bench/README.md). §12 P0 lists the
harness and the baselines ("Measure the desktop-app and guest-runtime baselines on Windows")
as part of gate G-FOUND.

- Scenario file format, placeholders and commands: [bench/README.md](../../bench/README.md).
- Steps for the Windows-only baselines: [bench/windows-baselines.md](../../bench/windows-baselines.md).

## How to run

```sh
cargo build --release -p argus-bench          # target/release/argus-bench
B=target/release/argus-bench

# Validate every scenario file.
$B check bench/scenarios/*.toml

# Python baselines: point {PYTHON} at the interpreter of a venv with argus-app-testing
# installed; {ARGUS} is then found next to it.
$B run --scenario bench/scenarios/python-cli-test-run.toml --python /path/to/venv/bin/python \
    --out cli-test-run.json

# Rust targets, once argus-next has the feature (the scenarios are `pending` until then).
$B run --scenario bench/scenarios/argus-next-cli-test-run.toml --argus-next target/release/argus-next
```

Defaults: 5 counted runs (`--runs`), 50 ms sampling (`--interval-ms`), the scenario's warm-up
runs (1 unless the scenario says otherwise) before the counted runs. The table goes to
stdout; `--out` writes the JSON result (`argus-bench-result-v1`), `--json` prints it.
`argus-bench merge` combines results into one `argus-bench-result-set-v1` file and
`argus-bench show` prints the tables of a result or a set.

The harness itself is tested in `cargo test --workspace` (unit tests with a fake process
source; integration tests on real `sh`/`sleep` process trees on Unix). Benchmarks are **not**
run in CI: shared CI runners are too noisy for budgets. G-PERF is checked by running the
scenarios on the gate machines (Windows 11 x64 and Ubuntu 24.04 x64, §11).

### Attach mode: the desktop app and the guest runtime

Some workloads are driven by a person or run where the harness cannot start them:

```sh
argus-bench attach --name argus-gui --duration 45s --out desktop-run.json   # desktop app during a run
argus-bench attach --pid 4242 --duration 60s --out idle.json                # any tree, by PID
argus-bench attach --name argus-guest --duration 60s --out guest-idle.json  # inside a Capsule guest
```

Each top-most process whose name matches (case-insensitive, `.exe` optional), or the given
PID, becomes a root; it and all its descendants are sampled for the duration. With
`--measure-after 5s` the first five seconds do not count towards the peak. The result has one
run; repeat it five times for a median. Attach mode fails (exit code 1) if the measured tree
exits before the duration is over.

For the desktop app the tree is the launcher, the Python process and the webview processes
(WebView2 `msedgewebview2.exe` on Windows, WebKitGTK `WebKitWebProcess` on Linux). The
§11 "backend" figure is the sum of the per-name entries of the launcher and Python (later: the
Tauri app process); the "webview" figure is the webview entries. The Windows procedure,
including the "during a run" fixture, is in
[bench/windows-baselines.md](../../bench/windows-baselines.md).

## Metric definitions

| Term | Definition |
|---|---|
| Process tree | The measured root process and **all** its descendants, found from the parent links in the operating system's process table at every sample. A process is identified by PID **and** start time, so a reused PID is never mistaken for a member. Once seen, a member stays counted while it lives, even if it is re-parented (for example a grandchild whose parent exited). |
| Per-process memory | **Linux:** resident set size (`/proc/<pid>/statm`, resident pages × page size). **macOS:** resident size (`proc_pidinfo`, `pti_resident_size`). **Windows:** working set (`GetProcessMemoryInfo`, `WorkingSetSize`). The `sysinfo` crate reads these; each result records the metric name (`rss` or `working_set`) and this definition in `metric`. |
| Sample | One reading of the process table: the sum of per-process memory over the tree at that moment. Taken every `interval_ms` (default 50 ms), the first one immediately after the root is spawned. |
| Peak tree memory | Per run: the highest sample. Samples before `measure_after` are excluded. |
| Mean tree memory | Per run: the mean of the counted samples that contained at least one process. |
| Wall time | Per run: from just before the root process is spawned until the harness observes its exit (a helper thread waits on the child, so this is not rounded to the sampling interval). Includes interpreter or runtime start-up and shutdown; excludes setup and teardown. For a `duration` scenario or attach mode it is the sampling window. |
| Distinct processes | Per run: how many different processes (PID + start time) were seen in the tree. |
| Per-name peak | Per run and process name (the executable file name, else the OS process name): the highest per-sample sum over the processes with that name. Peaks of different names can occur at different samples, so they need not add up to the tree peak. |
| Leftover processes | Run mode: tree members still alive when the root exited, with their names. The harness terminates them after recording them. |
| Summary | Over the **counted** runs (warm-up runs and failed runs excluded): median, minimum and maximum of each per-run value. For an even count the median is the lower-rounded mean of the two middle values. |
| Status | `ok`; `failed` (a run exited with an unexpected code, timed out, exited before its duration, or setup/teardown failed); `skipped` (the scenario's probe failed, e.g. no Chromium); `pending` (placeholder for a feature argus-next does not have yet). |
| Units | Results store bytes and microseconds. Tables print MiB (1,048,576 bytes) and ms. Where §11 writes MB, the scenario targets use the stricter decimal reading (15 MB = 15,000,000 bytes = 14.3 MiB). |
| Environment | OS (`linux`/`macos`/`windows`), OS name and version, CPU architecture, logical CPU count, total RAM, tool version, UTC timestamp (`--timestamp` or the clock). No host name, user name or machine path is recorded; commands are recorded as templates. |

Known limits (also written into every result as `known_limits`):

- **Short-lived processes can be missed.** A process that starts and exits between two
  samples is never seen, and a memory peak shorter than the interval can be missed. A command
  that lives one or two intervals is sampled only right after it started, so its peak is
  underestimated; such a result carries a warning (fewer than 3 samples in a counted run).
  Measure these commands again with `--interval-ms 5` (see `python -c pass` below).
- Shared pages (libraries, shared memory) are counted once per process, so the tree sum can
  exceed the physical memory the tree really uses. This matches how ADR-001 measured and is
  consistent between the Python and Rust implementations.
- Reading the whole process table every 50 ms costs the harness a little CPU, more on
  machines with many processes.

## P0 baseline on Linux x86_64: ADR-001 §2 reproduced

Committed result: [`bench/results/baseline-python-linux-x86_64.json`](../../bench/results/baseline-python-linux-x86_64.json)
(8 results; print them with `argus-bench show`). Machine: shared 4-CPU Linux x86_64 cloud
sandbox, Ubuntu 24.04, 15.7 GiB RAM; Python 3.13.16; argus-app-testing 0.1.6.dev10 (this
branch); 1 warm-up + 5 counted runs; 50 ms sampling unless noted. ADR-001 measured on `main`
at `8e7ebe5` with Python 3.11 on a **different** Linux x86_64 sandbox, so differences are
expected; they are stated below rather than adjusted.

| Workload (scenario) | ADR-001 §2 | This harness, median [min–max] | Processes |
|---|---|---|---|
| `python -c pass` (`python-pass`) | 10 MB | 30.6 ms; **2.0 MiB at 50 ms (unreliable, 1 sample per run)**; 10.7 MiB [10.6–10.7] at 5 ms | 1 |
| Import `argus.cli`, warm (`python-import-argus-cli`) | ~150 ms; 42 MB | 321.3 ms [280.8–421.8]; 42.3 MiB [42.3–42.5] | 1 |
| `argus --help`, warm (`python-help`) | ~150 ms (import alone, §11) | 298.2 ms [285.9–325.5]; 42.6 MiB [42.4–42.6] | 1 |
| `argus run`, CLI test, two assertions (`python-cli-test-run`) | 0.67 s; 48 MB | 618.8 ms [577.8–653.8]; 49.4 MiB [49.3–49.5] | 1 |
| `argus run`, one browser assertion (`python-browser-one-assertion`) | 7.0 s; 504 MB; 8 processes | 1362.6 ms [1278.4–1470.2]; 525.7 MiB [522.2–526.3] | 8 |
| – of which Chromium (`headless_shell`) | 363 MB (72%) | 362.3 MiB (69%) | 6 at once |
| – of which Playwright Node driver (`node`) | 84 MB (17%) | 105.9 MiB (20%) | 1 |
| – of which Python | 57 MB (11%) | 59.0 MiB (11%) | 1 |

The 5 ms re-measurements of the other short commands agree with the 50 ms ones (import
42.4 MiB, `--help` 42.4 MiB), so 50 ms is adequate for anything that lives a few hundred
milliseconds or more.

Deviations, honestly stated:

- **Memory agrees** with ADR-001 within a few percent for every row except the Node driver
  (+26%). The browser run used Playwright 1.56.0 (the Chromium revision available offline in
  the sandbox), not the version ADR-001 used; the driver's Node runtime differs between
  Playwright releases. ADR-001 does not say whether its MB are 10^6 or 2^20 bytes; read as
  MiB, the agreement is closer.
- **`python -c pass` at 50 ms is not a usable number.** The interpreter lives about 30 ms, so
  each run is sampled once, right after `exec`, before the interpreter has loaded. The harness
  flags this with a warning; at 5 ms it reproduces ADR-001's 10 MB. The §11 rows that matter
  for G-PERF (CLI and browser runs) last long enough for 50 ms sampling.
- **Wall times for import and `--help` are about twice ADR-001's ~150 ms.** ADR-001's figure
  is the import alone (measured inside Python); the harness measures the whole process,
  including about 30 ms of interpreter start-up and the shutdown. Measured inside Python on
  this machine the import took 194–257 ms (three runs), slower than ADR-001's machine; this
  sandbox is shared, and other builds ran concurrently (wall-time spread up to ±30%).
- **The browser run is much faster (1.4 s versus 7.0 s).** The fixture loads an inline
  `data:` URL, so there is no network wait; ADR-001's test page and Playwright version are not
  recorded. The memory figures are the comparable part of this row.
- **The Python process exits before its browser does.** In every browser run, one `node`
  process and two Chromium processes were still alive when the `argus` process exited; the
  harness recorded and terminated them (`leftover_names` in the result). The Rust browser
  adapter should own and reap its browser (§6.2).

Reproduce (the browser scenario also needs Playwright's Chromium for the installed Playwright
version, or it is skipped):

```sh
B=target/release/argus-bench; PY=/path/to/venv/bin/python; OUT=/tmp/argus-bench-baseline
mkdir -p "$OUT"
for s in python-pass python-import-argus-cli python-help python-cli-test-run python-browser-one-assertion; do
  $B run --scenario bench/scenarios/$s.toml --python "$PY" --out "$OUT/$s.json"
done
for s in python-pass python-import-argus-cli python-help; do
  $B run --scenario bench/scenarios/$s.toml --python "$PY" --interval-ms 5 \
      --label variant=interval-5ms --out "$OUT/$s-5ms.json"
done
$B merge --description "..." --out bench/results/baseline-python-linux-x86_64.json \
  "$OUT"/python-pass.json "$OUT"/python-pass-5ms.json "$OUT"/python-import-argus-cli.json \
  "$OUT"/python-import-argus-cli-5ms.json "$OUT"/python-help.json "$OUT"/python-help-5ms.json \
  "$OUT"/python-cli-test-run.json "$OUT"/python-browser-one-assertion.json
```

## Windows baselines (blocked here)

These are the P0 baselines §11 marks "to be measured". They need a Windows 11 x64 machine
with a desktop; the sandbox has neither. Steps: [bench/windows-baselines.md](../../bench/windows-baselines.md).

| Baseline | Scenario / method | Value |
|---|---|---|
| Desktop app idle (window open, no job), Python `argus-gui` | `windows-desktop-idle.toml` (run mode, 60 s, first 15 s excluded) | **blocked here** |
| Desktop app during a run, Python `argus-gui` | attach mode while `bench/fixtures/desktop-project` runs its 30 s test | **blocked here** |
| Capsule guest runtime (PyInstaller bundle), Windows | `argus-guest-runtime-build`, archive and unpacked size | **blocked here** |

Data point from this sandbox, for orientation only (the Ubuntu guest bundle, not the Windows
one): `argus-guest-runtime-build` with PyInstaller 6.22.3 from the same Python 3.13.16
environment (Playwright 1.63.0 installed, so the browser adapter is frozen in) produced a
68,701,328-byte archive (65.5 MiB) that unpacks to 188,917,798 bytes (180.2 MiB, 189 archive entries).
The `argus-guest` entry executable is 6.1 MB; the Playwright package with its Node driver is
140.2 MB (74%) of the unpacked size, `cryptography` 14.4 MB, `libpython3.13` 7.4 MB. The §11
target for the native guest is a binary of at most 15 MB.

## Spec §11 rows and their scenarios

| §11 row | Python baseline | Rust target scenario | State |
|---|---|---|---|
| `argus --help`, warm | `python-help` (above) | `argus-next-help` (wall ≤ 30 ms) | pending: full command set |
| CLI test run (two assertions) | `python-cli-test-run` (above) | `argus-next-cli-test-run` (≤ 200 ms, ≤ 14.3 MiB) | pending: `run` with the CLI adapter |
| Browser test, one assertion | `python-browser-one-assertion` (above) | `argus-next-browser-one-assertion` | pending: CDP browser adapter. Checked from the per-name breakdown: no `node` entry; Argus entry ≤ 25 MB; tree minus Chromium entries ≤ 25 MB |
| Knowledge, graph only | – | `argus-next-knowledge-graph` (≤ 10 MB extra over the CLI run) | pending: knowledge store |
| Knowledge, local embedder | chromadb path 271 MB (ADR-001, not re-measured: needs the knowledge extra and a model download) | `argus-next-knowledge-onnx` (≤ 120 MB extra), `argus-next-knowledge-ollama` (≤ 10 MB extra) | pending: embedding backends |
| Desktop app idle | `windows-desktop-idle` (blocked here) | `argus-next-desktop-idle` (backend ≤ 30 MB) | pending: Tauri app |
| Desktop app during a run | attach mode (blocked here) | `argus-next-desktop-run` (attach mode) | pending: Tauri app. "No periodic polling" is checked in code review and with a CPU/wake-up trace, not by this memory harness |
| Capsule guest runtime | PyInstaller bundle size (blocked here; Linux data point above) | `argus-next-guest-idle` (idle ≤ 10 MB) | pending: `argus-guest`. Binary size ≤ 15 MB is a file-size check: `stat -c %s target/release/argus-guest` or `(Get-Item argus-guest.exe).Length` |
| Wheel size per platform | – | – | File-size check on the built wheels (`ls -l dist/*.whl`, ≤ 40 MB each); not a harness scenario |

"Extra" memory rows compare two harness results on the same machine: the median peak of the
scenario with the feature minus the median peak of `argus-next-cli-test-run`.
