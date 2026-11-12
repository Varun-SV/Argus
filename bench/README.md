# Benchmark harness: scenarios, fixtures and results

This directory holds the inputs and committed results of `argus-bench`, the benchmark harness
for the performance budgets in [specification §11](../docs/rearchitecture/specification.md#11-performance-budgets-and-how-they-are-measured)
(gate G-PERF). How to run it, the metric definitions and the committed baselines are in
[docs/rearchitecture/benchmarks.md](../docs/rearchitecture/benchmarks.md). Windows-only
baselines: [windows-baselines.md](windows-baselines.md).

```
bench/
  scenarios/   one TOML file per measured scenario (format below)
  fixtures/    small Argus projects the scenarios copy into a fresh working directory
  results/     committed result files (argus-bench-result-v1 / -result-set-v1 JSON)
```

The harness source is `crates/argus-bench`. Build it with
`cargo build --release -p argus-bench`; the binary is `target/release/argus-bench`
(`argus-bench.exe` on Windows).

## Commands

```sh
# Run a scenario: 1 warm-up run (scenario default) + 5 counted runs, 50 ms sampling.
argus-bench run --scenario bench/scenarios/python-cli-test-run.toml \
    [--runs 5] [--interval-ms 50] [--out result.json] [--json] \
    [--python PATH] [--argus PATH] [--argus-gui PATH] [--argus-next PATH] \
    [--warmup N] [--root REPO] [--timestamp 2026-10-08T12:00:00Z] [--label key=value]... \
    [--show-output]

# Sample an already running tree (by PID or by process name) for a fixed time.
argus-bench attach (--pid N | --name NAME)... --duration 60s \
    [--measure-after 5s] [--interval-ms 50] [--out result.json] [--json] [--label key=value]...

# Validate scenario files without running them.
argus-bench check bench/scenarios/*.toml

# Combine result files into one result set, and print the tables of a result or a set.
argus-bench merge --description "..." --out set.json a.json b.json ...
argus-bench show set.json
```

Exit codes: `0` when the status is `ok`, `skipped` or `pending`; `1` when a run failed
(unexpected exit code, timeout, early exit, failed setup or teardown); `2` for invalid
arguments or scenario files.

The table goes to stdout; with `--json` the JSON document goes to stdout instead. `--out`
writes the JSON document in either case. The output of the measured command is captured to a
temporary file (not into the result); when a run fails, its last 20 lines are printed on
stderr. `--show-output` lets the command print to the terminal instead.

## Scenario files

A scenario is a TOML file. Unknown fields are errors, so a typo cannot silently change what is
measured. `argus-bench check` validates files without running them; the test
`crates/argus-bench/tests/scenarios.rs` checks every committed scenario.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `schema` | string | – | Optional; when present it must be `argus-bench-scenario-v1`. |
| `name` | string | required | 1–64 lowercase letters, digits and hyphens; the committed files use it as the file name. |
| `description` | string | required | One paragraph. |
| `tags` | array of strings | `[]` | Free-form. |
| `command` | array of strings | required | The **argv** that is measured. There is no shell and no shell interpolation; the only substitution is the placeholders below. |
| `cwd` | string | `{WORKDIR}` | Working directory. A relative path is taken relative to `{REPO}`. |
| `env` | table of strings | `{}` | Variables set for `command`, `setup`, `teardown` and the probe, on top of the harness's environment. A value that expands to the empty string **removes** the variable. |
| `setup` | array of argv arrays | `[]` | Run in order before each run, outside the measured window. Each must exit 0. |
| `teardown` | array of argv arrays | `[]` | Run in order after each run, outside the measured window. Each must exit 0. |
| `timeout` | duration string | `120s`, or `duration` + 30 s | Hard limit per run (and per setup/teardown command); the whole tree is terminated when it is reached and the run fails. |
| `warmup` | integer | `1` | Uncounted runs before the counted ones (`--warmup` overrides). |
| `expected_exit` | integer | `0` | Exit code a counted run must end with (not checked when `duration` is set). |
| `duration` | duration string | – | For applications that do not exit by themselves: each run is stopped after this long, and stopping it is the expected outcome. An earlier exit fails the run. |
| `measure_after` | duration string | `0s` | Samples before this offset do not count towards the peak (excludes start-up in idle scenarios). They still count for the process inventory. |
| `pending` | string | – | Marks a placeholder that is parsed but not run; the result has status `pending`. Rust-side rows use the text `needs argus-next feature: ...`. |
| `[workdir] copy_from` | string | – | Repository-relative directory copied into `{WORKDIR}` before each run (never modified in place). `/` separators, no `..`. |
| `[workdir] create_dirs` | array of strings | `[]` | `{WORKDIR}`-relative directories created before each run. |
| `[skip_unless] command` | argv array | – | Probe run once before the scenario (with the scenario's `env`). If it cannot start or exits non-zero, the result has status `skipped` and the scenario is not run. |
| `[skip_unless] reason` | string | required with a probe | Recorded in the result when the probe fails. Probe output is not recorded. |
| `[skip_unless] timeout` | duration string | `60s` | Probe time limit. |
| `[target] wall_ms` | number | – | Upper bound on the **median** wall time, in milliseconds (spec §11). |
| `[target] peak_tree_mib` | number | – | Upper bound on the **median** peak tree memory, in MiB (1 MiB = 1,048,576 bytes). Where the spec writes MB, the committed files use the stricter decimal reading (15 MB = 14.3 MiB). |
| `[target] source` | string | – | Where the bound comes from, e.g. `spec section 11 row 2`. |

Durations use [humantime](https://docs.rs/humantime) syntax: `500ms`, `30s`, `2m`.

### Placeholders

Any string in `command`, `cwd`, `env` values, `setup`, `teardown` and the probe may contain
these placeholders. An unknown `{UPPERCASE}` token is an error; other braces (for example a
Python dict literal in `-c` code) are left alone.

| Placeholder | Value |
|---|---|
| `{PYTHON}` | `--python`, else `ARGUS_BENCH_PYTHON`, else `python3` (`python` on Windows). |
| `{ARGUS}` | `--argus`, else `ARGUS_BENCH_ARGUS`, else the `argus` script next to `{PYTHON}` (a venv's `bin/` or `Scripts\`), else `argus`. |
| `{ARGUS_GUI}` | `--argus-gui`, else `ARGUS_BENCH_ARGUS_GUI`, else `argus-gui` next to `{PYTHON}`, else `argus-gui`. |
| `{ARGUS_NEXT}` | `--argus-next`, else `ARGUS_BENCH_ARGUS_NEXT`, else `argus-next`. |
| `{REPO}` | `--root`, else the nearest ancestor of the scenario file that has a `bench/` directory. |
| `{SCENARIO_DIR}` | Directory of the scenario file. |
| `{WORKDIR}` | A fresh empty temporary directory per run, deleted afterwards. |
| `{OS}` | `linux`, `macos` or `windows`. |
| `{OS_FAMILY}` | `unix` or `windows`. |
| `{EXE_SUFFIX}` | `.exe` on Windows, empty elsewhere. |
| `{PLAYWRIGHT_BROWSERS_PATH}` | The `PLAYWRIGHT_BROWSERS_PATH` variable if set, else Playwright's per-OS default cache under the real home directory. Needed because the Python scenarios point `HOME` into `{WORKDIR}`. |

Paths given with `--python` and friends (or the variables) are made absolute when they contain
a path separator, so they survive the per-run working directory. Bare names are looked up on
`PATH`. Scenario files never contain machine-specific paths; results record the command
**templates**, never the expanded paths.

### Example

```toml
schema = "argus-bench-scenario-v1"
name = "python-cli-test-run"
description = "`argus run` of a two-assertion CLI test in bench/fixtures/cli-project."
command = ["{ARGUS}", "run", ".argus/two-assertions-{OS_FAMILY}.test.yaml"]
cwd = "{WORKDIR}"
warmup = 1

[workdir]
copy_from = "bench/fixtures/cli-project"
create_dirs = ["home"]

[env]
HOME = "{WORKDIR}/home"
```

## Committed scenarios

| File | Spec §11 / ADR-001 §2 row | State |
|---|---|---|
| `python-pass.toml` | ADR `python -c pass` | runnable |
| `python-import-argus-cli.toml` | ADR import `argus.cli` (warm) | runnable |
| `python-help.toml` | §11 `argus --help`, warm (Python baseline) | runnable |
| `python-cli-test-run.toml` | §11 CLI test run (two assertions); ADR same | runnable |
| `python-browser-one-assertion.toml` | §11 browser test, one assertion; ADR same | runnable; skipped when Playwright cannot launch Chromium |
| `windows-desktop-idle.toml` | §11 desktop app idle (Python baseline) | runnable on Windows only, skipped elsewhere |
| `argus-next-help.toml` | §11 row 1 target | pending |
| `argus-next-cli-test-run.toml` | §11 row 2 target | pending |
| `argus-next-browser-one-assertion.toml` | §11 row 3 target | pending |
| `argus-next-knowledge-graph.toml` | §11 row 4 target | pending |
| `argus-next-knowledge-onnx.toml`, `argus-next-knowledge-ollama.toml` | §11 row 5 targets | pending |
| `argus-next-desktop-idle.toml` | §11 row 6 target | pending |
| `argus-next-desktop-run.toml` | §11 row 7 target (attach mode) | pending |
| `argus-next-guest-idle.toml` | §11 row 8 target (idle memory) | pending |

The fixtures are assertion-only Argus projects: no model is called and no network access is
made (the configured provider points at a closed loopback port and is never contacted).
`browser-project` loads an inline `data:` URL. `desktop-project` holds a 30-second CLI test
for the "desktop app during a run" measurement.

## What is measured, and known limits

Every interval the harness reads the process table, finds the measured root process and
**all** its descendants, and sums their memory: resident set size on Linux and macOS, working
set on Windows (recorded in each result as `metric`). Per run it records the peak summed
value, the wall time, the number of distinct processes seen, and a per-process-name peak
breakdown. Over the counted runs it reports the median, minimum and maximum. Details and
definitions: [benchmarks.md](../docs/rearchitecture/benchmarks.md#metric-definitions).

Known limits, also copied into every result as `known_limits`:

- **Short-lived processes can be missed.** A process that starts and exits between two
  samples is never seen, and a peak shorter than the interval can be missed. A command that
  lives only one or two intervals (for example `python -c pass`, about 30 ms) is sampled once,
  right after it started, and its peak is underestimated. The result then carries a warning;
  measure such commands again with `--interval-ms 5`.
- Memory is summed per process, so pages shared between processes are counted once per
  process; the sum can exceed the physical memory in use.
- A process that leaves the tree (is re-parented) before its first sample is not counted;
  once seen, a process stays counted while it lives, even after re-parenting.
- Wall time runs from just before the root process is spawned until it exits; it includes
  interpreter or runtime start-up and excludes setup and teardown.
- The harness itself runs on the same machine; reading the process table every 50 ms costs a
  little CPU, which matters on machines with very many processes.

## Results

`results/` holds committed result files. A result file has the schema id
`argus-bench-result-v1`; `argus-bench merge` combines several into one
`argus-bench-result-set-v1` file. Environment metadata is limited to the OS name and version,
CPU architecture, logical CPU count, total RAM, tool version and a UTC timestamp; results
contain no host name, user name or machine path.

| File | Contents |
|---|---|
| `results/baseline-python-linux-x86_64.json` | ADR-001 §2 reproduction on a Linux x86_64 sandbox (see benchmarks.md for the comparison). |
