# Conformance suite (`tests/conformance/`)

Black-box tests that drive the `argus` command line through its public interface only
([specification §13](../../docs/rearchitecture/specification.md#13-testing-strategy)). The same
cases run against both implementations while both exist. **A case that passes on Python and
fails on Rust is a parity bug.** The cases check the contract in
[C-07](../../docs/rearchitecture/specification.md#5-compatibility-contracts): commands, options
and exit codes stay as listed in
[parity inventory §1](../../docs/rearchitecture/parity-inventory.md#1-command-line-argus), and
human-readable output may change wording but MUST keep the same information. Assertions
therefore check information content (a file name, a count, an option name), not exact wording.

The runner is the Rust crate `argus-conformance` (workspace member). Case files are JSON under
`cases/`. This directory contains no Python test files, so pytest does not collect anything here.

## Running

All commands run from the repository root.

```sh
# Python implementation: `<python> -m argus`. The interpreter must have Argus installed.
# Windows: `--python '.venv\Scripts\python.exe'` (single quotes keep the backslashes in PowerShell).
cargo run -p argus-conformance -- run --impl python --python .venv/bin/python
ARGUS_CONFORMANCE_PYTHON=.venv/bin/python cargo run -p argus-conformance -- run --impl python

# Rust preview binary (default target/release/argus-next).
cargo build --release -p argus-cli
cargo run -p argus-conformance -- run --impl rust
cargo run -p argus-conformance -- run --impl rust --bin path/to/argus-next

# Any explicit command line; --profile picks which case statuses apply (default rust).
cargo run -p argus-conformance -- run --impl custom --cmd 'path/to/argus-next' --profile rust

# Useful options
#   --cases DIR        case directory (default tests/conformance/cases), searched recursively
#   --filter TEXT      only cases whose id or file path contains TEXT
#   --report FILE      write the JSON report
#   --work-dir DIR     parent directory for the per-case sandboxes (default: system temp)
#   --keep-failed      keep the sandbox of failing cases and print its path
#   -v, --verbose      also print notes and the reasons behind pending/known-gap results

# Parse and validate every case file without running anything (exit 1 if one is invalid).
cargo run -p argus-conformance -- validate
```

Runner exit codes: `0` no case failed or errored; `1` at least one `fail` or `error`; `2` usage or
configuration error (no interpreter given, binary not found, cases directory missing, report not
writable). `pending`, `known-gap` and `skipped` never make the run fail.

## Isolation

Every case gets a fresh sandbox directory, removed afterwards:

```
argus-conf-XXXXXX/
  project/     working directory of every step; setup files are written here
  home/        HOME and USERPROFILE (home/AppData/{Roaming,Local} are APPDATA, LOCALAPPDATA)
  xdg/{config,data,state,cache}/   XDG_CONFIG_HOME, XDG_DATA_HOME, XDG_STATE_HOME, XDG_CACHE_HOME
  tmp/         TMPDIR, TMP, TEMP, XDG_RUNTIME_DIR
```

- **Environment.** Nothing is inherited from the host except `PATH` (on Windows also
  `SYSTEMROOT`, `SYSTEMDRIVE`, `WINDIR`, `PATHEXT`, `COMSPEC`). No provider API key or other
  credential can reach the implementation. Fixed values: `LANG=LC_ALL=C.UTF-8`,
  `PYTHONIOENCODING=utf-8`, `PYTHONUTF8=1`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONNOUSERSITE=1`,
  `NO_COLOR=1`, `TERM=dumb`, `COLUMNS=200` (so console wrapping is deterministic), `LINES=50`,
  `CI=1`. A step's `env` is applied on top.
- **No network.** `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` (both cases) point at
  `http://127.0.0.1:9`, a closed local port, and `NO_PROXY` is empty, so any attempt to reach the
  network fails fast. Cases that need a provider configure one on a closed local port too.
- **Hard timeout.** Each step has a timeout (default 30 s, maximum 600 s). On Unix the step is
  started as the leader of a new process group; when the timeout expires the whole group gets
  `SIGKILL`, so servers, browsers and other grandchildren die with it. The group is also killed
  after a normal exit, so nothing outlives its step. A step also counts as timed out when it has
  exited but a leftover process still holds its output open at the deadline. On Windows only
  the direct child is killed (see "Platform notes").
- Captured output is capped at 4 MiB per stream; the report keeps 16 KiB per stream.

## Case format (`argus-conformance-case-v1`)

One JSON object per file. Unknown keys are rejected everywhere, so a typo cannot silently weaken
a case. Files are run in case-id order, whatever their location under `cases/`.

```json
{
  "schema": "argus-conformance-case-v1",
  "id": "cli.run.invalid-spec",
  "title": "argus run on an invalid spec exits 2 and names the file and problem",
  "requirements": ["C-07", "PARITY-1", "PARITY-2"],
  "impls": {
    "python": { "status": "active" },
    "rust": { "status": "pending", "note": "commands arrive in later phases" }
  },
  "platforms": ["linux", "macos", "windows"],
  "setup": {
    "dirs": [".argus/runs"],
    "files": [
      { "path": ".argus/config.yaml", "content": "provider: ollama\n" },
      { "path": ".argus/history.json", "from": "tests/golden/some/fixture.json" }
    ]
  },
  "normalizers": ["timestamps", { "regex": "port \\d+", "replace": "port N" }],
  "steps": [
    {
      "args": ["run", "broken.test.yaml"],
      "env": { "EXTRA": "value" },
      "stdin": "y\n",
      "timeout_s": 30,
      "expect": {
        "exit": 2,
        "output": { "contains": ["broken.test.yaml"], "regex": ["(?i)\\btarget\\b"] },
        "files": [{ "path": ".argus/runs", "exists": false }],
        "json": [{ "file": ".argus/usage.json", "path": "$.calls", "equals": 0 }]
      }
    }
  ],
  "note": "optional free text shown with --verbose"
}
```

| Key | Meaning |
|---|---|
| `schema` | Must be `argus-conformance-case-v1`. |
| `id` | Unique; lowercase `[a-z0-9._-]`, starts with a letter. Prefix with the area (`cli.`). |
| `title` | What the case proves, in one line. |
| `requirements` | Requirement IDs the case covers (see below). |
| `impls` | Which implementations the case applies to (`python`, `rust`) and its status for each (below). An implementation not listed is reported `skipped`. |
| `platforms` | Optional; `linux`, `macos`, `windows`. Empty or absent means all. |
| `setup.dirs` / `setup.files` | Created in the project directory before step 1. A file has exactly one of `content` (inline text) or `from` (a repository path under `tests/golden/`, resolved against `--repo-root`). Paths must be relative and cannot contain `..`. |
| `normalizers` | Extra normalizers after the defaults (below). |
| `steps` | Run in order in the same sandbox. After the first failing step the remaining steps are not run (reported as such). |

**Step:** `args` are the arguments after the executable (for Python the executable is
`<python> -m argus`). `args`, `env` values and `stdin` may use the placeholders `${PROJECT}`,
`${SANDBOX}` and `${HOME}`. `stdin` is written and then closed; without it stdin is empty.
`timeout_s` defaults to 30.

**Expect:**

| Key | Meaning |
|---|---|
| `exit` | An exit code (`2`), a list of allowed codes (`[0, 2]`), `"nonzero"` or `"any"`. Required unless `timeout` is true. A process killed by a signal only satisfies `"nonzero"`/`"any"`. |
| `timeout` | `true`: the step must still be running at its timeout (for servers and watchers); it is killed and the output so far is checked. Exclusive with `exit`. |
| `stdout`, `stderr`, `output` | Text assertions on the normalized streams; `output` is stdout followed by stderr. Prefer `output`: an implementation may print a message on either stream. Keys: `contains`, `not_contains`, `regex`, `not_regex` (lists; Rust `regex` syntax, use `(?i)` and `(?m)` flags), `empty` (bool). A failed `not_contains` never echoes the surrounding text, so it is safe for secret values. |
| `files` | `path` relative to `root` (`project` default, `sandbox`, `home`, `xdg_config`, `xdg_data`, `xdg_state`, `xdg_cache`; `"."` is the root itself). Keys: `exists` (bool), `kind` (`file`/`dir`), `contains`, `not_contains` (file content), `tree_not_contains` (no file anywhere below the path contains these bytes, used to prove a secret is never stored in plaintext). |
| `json` | Assertions on a JSON file a step produced: `file`, `root`, `path` (subset: `$`, `.key`, `["key"]`, `[index]`), and any of `exists`, `equals` (any JSON value), `type` (`null`, `bool`, `number`, `string`, `array`, `object`), `contains` (substring of a string, or an element of an array of strings), `min_len`. |

### Statuses

| Status | Run? | Reported as | Fails the run? |
|---|---|---|---|
| `active` | yes | `pass` or `fail` | yes, on `fail` |
| `pending` | yes | `pending` (with `would_pass`) | no |
| `known-gap` | yes | `known_gap` (with `would_pass`) | no |
| `skip` | no | `skipped` | no |

`pending` means "not implemented yet in this implementation" (most Rust cases during P0).
`known-gap` means "this implementation has a known defect": the case asserts the **correct**
behaviour, never the defect, and its note says what is wrong. Both require a `note`. When
`would_pass` is true the assertions now hold and the status can be promoted to `active`; the
human summary says so. A case file that cannot be parsed, a setup failure or a command that
cannot be started is an `error`.

### Normalizers

Assertions and the report see normalized output. Defaults, always applied in this order:

- `strip_ansi`: removes CSI and OSC escape sequences (colours, hyperlinks).
- `crlf`: `\r\n` becomes `\n`.
- `tmpdir`: every spelling of the sandbox root (as created, canonical, and with `/` or `\`
  separators) becomes `<TMP>`; the project directory is `<TMP>/project`.

Opt-in by name: `timestamps` (ISO 8601 and `YYYYMMDD-HHMMSS` become `<TIMESTAMP>`), `versions`
(`1.2.3`, `v1.2.3-rc.1`, `0.1.6.dev10+g1234abc` become `<VERSION>`; opt-in because it would also
rewrite dotted IP addresses), `uuids` (UUIDs and hex strings of 32 or more digits become
`<UUID>`), `durations` (`1.25s`, `30 ms` become `<DURATION>`). Custom:
`{"regex": "...", "replace": "..."}`, applied after the built-ins.

### Requirement IDs

Cases cite the IDs used in the re-architecture documents: `C-01`…`C-07` (specification §5),
`R-1`…`R-7` (Amendment B, specification §18), `SEC-1` and `ARG-nn` (stabilization register).
Parity inventory sections are cited as `PARITY-n` (for example `PARITY-1` is
[parity inventory §1](../../docs/rearchitecture/parity-inventory.md#1-command-line-argus), the
command line; `PARITY-2` is §2, project files and formats).

## The JSON report (`argus-conformance-report-v1`)

```json
{
  "schema": "argus-conformance-report-v1",
  "implementation": "python",
  "command": ["python", "-m", "argus"],
  "platform": "linux",
  "filter": null,
  "summary": { "total": 46, "pass": 44, "fail": 0, "pending": 0, "known_gap": 2, "skipped": 0, "error": 0 },
  "results": [
    {
      "id": "cli.help.gui", "file": "cli/help.gui.json", "title": "...", "requirements": ["C-07"],
      "declared": "active", "outcome": "pass", "failures": [], "duration_ms": 312,
      "steps": [{ "index": 0, "args": ["gui", "--help"], "exit_code": 0, "timed_out": false,
                  "duration_ms": 311, "failures": [], "stdout": "...", "stderr": "" }]
    }
  ]
}
```

Results are sorted by case id. `command` holds only the executable's file name, never its
directory, and step output is normalized, so a report contains no host paths. `would_pass` and
`note` appear only when set; `declared` is `none` for a case that does not list the
implementation.

## Current case set (`cases/cli/`)

46 cases covering the command line in parity inventory §1: `--help`/`--version`/no command,
unknown commands and options, `--help` of every command and subcommand with its documented
options, the `init` scaffold and never overwriting existing files, `tokens` and `report` on an
empty project, `tokens` reading persisted usage (C-02), `run --dry-run` on valid specs and its
non-use of tokens, `run` on invalid specs, invalid YAML, unknown assertions, missing files and
an empty project, the `secrets` round trip with a dummy value that must never be printed or
stored in plaintext (C-06), `serve` refusing a non-loopback host without `--allow-remote` (R-3,
SEC-1), `providers` on an unreachable provider and `knowledge stats` on an empty project.

Not covered here on purpose: `gui` and `watch` behaviour (desktop and long-running; the help
text is covered), `roam` and real `run` execution (need a model provider; covered by later
phases with a stub provider), `knowledge docker` (needs Docker), and cases built on the golden
fixtures in `tests/golden/` (wired in by the integration stage).

Known gaps in the Python implementation, recorded as `known-gap` cases:

- `cli.run.dry-run-shows-step-kind`: `argus run --dry-run` prints each line as rich markup, so
  the `[step]`/`[assert]`/`[teardown]` kind tag is swallowed and never shown. The same handling
  mangles error messages that contain `[`.
- `cli.secrets.uppercase-reference`: C-06 and the `secrets set` help text write
  `SECRET://ARGUS/NAME`, but only the lowercase `secret://argus/` prefix is accepted. This needs
  an operator decision (accept the scheme case-insensitively, or document the lowercase form).

## Platform notes

- Process-group kill on timeout is Unix only. On Windows the runner kills the direct child; a
  grandchild can survive a timeout until a Job Object based kill is added. The Windows and macOS
  paths of the runner are not exercised in the Linux development sandbox; run
  `cargo test -p argus-conformance` and the suite on those hosts.
- Unit tests that use shell scripts are compiled on Unix only (`#[cfg(unix)]`).

## Adding a case

1. Run the real command in a throwaway directory with `HOME` and the `XDG_*` variables pointing
   into it, and note the information the output must carry.
2. Write the case with information-content assertions (`contains` the file name, `regex` for a
   count) rather than whole sentences.
3. `cargo run -p argus-conformance -- validate`, then run it against Python (it must pass, or be
   a `known-gap` with a note) and against the Rust binary (`pending` until implemented).
4. Never put real credentials, user names, host names or private paths in a case; label dummy
   secret values `TEST-ONLY`.
