# Golden fixtures: configuration and test specs (C-01)

These fixtures record how the **Python implementation** reads `.argus/config.yaml`
and `*.test.yaml` files today. The Rust implementation must read the same inputs
with the same meaning (compatibility contract **C-01**, specification §5). That
includes unknown keys, which must be warned about or rejected exactly as they are
today. Corrections are proposed and never applied silently (**CFG-07**). Existing
`knowledge` settings map as specification §7.1 and decision **R-2** say. The
fixtures cover the rows of [parity inventory §2](../../../docs/rearchitecture/parity-inventory.md#2-project-files-and-formats)
for `.argus/config.yaml`, `*.test.yaml` and the ten assertions.

`expected/` is the contract. When an expected file looks wrong, that is still what
Python does today. Do not "fix" it here: raise it as a CFG-07 proposal.

## Layout

| Path | Loader that interprets it | What it is |
|---|---|---|
| `cases/config/<case>.yaml` | `argus.config.load_config(<PROJECT>)` | Copied to `<PROJECT>/.argus/config.yaml` |
| `cases/config/<case>.load.json` | (sidecar, optional) | `env`: test-only variables set for this load; `provider`: the desktop session argument `load_config(provider=...)`; `config_file: "absent"`: no config file (the case then has only a sidecar) |
| `cases/spec/<case>.test.yaml` | `argus.engine.spec.load_spec(<PROJECT>/.argus/<case>.test.yaml)` | One test file |
| `cases/programmatic/<case>.json` | `TestSpec(...)`, `NLStep(...)`, `AssertStep(...)` | Programmatic construction (strict step-metadata validation) |
| `expected/<category>/<case>.json` | – | Interpretation, schema `argus-golden-config-v1` |
| `expected/_manifest.json` | – | Case list, outcome counts and loader-source hashes |

Generator: [`scripts/golden/export_config.py`](../../../scripts/golden/export_config.py).

```
python scripts/golden/export_config.py --write   # regenerate expected/
python scripts/golden/export_config.py --check   # exit 1 and list every drifting case
```

The exporter runs the loaders in a child process with a scrubbed environment:

- `HOME` and `XDG_*` point into a new temporary directory;
- no `ARGUS_*`, provider-key or proxy variables are inherited;
- socket connections and name lookups raise;
- no provider client, secret store or knowledge store is created.

Run it on **Linux**. It refuses to run elsewhere, because `load_config` picks the
`knowledge.type` default by platform. The default is `json` on hosts without
`/proc`, such as macOS, and `local` on Linux and Windows. Output is
deterministic. Running `--write` twice, or deleting `expected/` and regenerating
it, gives byte-identical files. Check the files out byte-exact (no CRLF
conversion), because the input hashes in `expected/` are computed over raw bytes.

## Expected-file schema (`argus-golden-config-v1`)

| Field | Meaning |
|---|---|
| `schema`, `case`, `category`, `loader` | Identity of the case and of the Python entry point that read it |
| `input.files[]` | Case files with their sha256, so an edited input without regeneration shows up as drift |
| `input.env`, `input.provider_argument`, `input.config_file` | Config cases only: the load parameters from the sidecar |
| `implementation` | `name` plus `loader_sources_sha256`, a combined hash of `argus/config.py`, `argus/engine/spec.py`, `argus/capsule/files.py`, `argus/secrets.py` and `argus/tokens.py`. A change to any of them shows up as drift in every file |
| `outcome` | `loaded` or `error` |
| `value` | Normalized result (keys sorted). Config: `provider`, `budgets`, `knowledge`, `execution` (every `capsule.*` field), `raw` (the parsed YAML), and `derived` (`make_budget()` limits, `knowledge_persist_dir()`, and every `secret://` string with the verdict of `validate_secret_ref`). Spec: `name`, `adapter`, `launch`, `retries`, `continue_on_failure`, `staging`, `collect`, `path`, `file_name`, and `steps` in execution order (class, kind, text or assertion, expected, `describe()`) |
| `error` | `category` (`spec_error`, `yaml_error`, `value_error`, `type_error`, `attribute_error`, ...), Python `type` and `module`, `message`, and the chained `cause` when there is one. Never a traceback |
| `diagnostics` | Warnings, log records, stdout and stderr emitted during the load. All are empty today: the loaders never warn |

Placeholders: `<PROJECT>` is the project directory, `<HOME>` the redirected home
directory, `<TMP>` the export directory. YAML values JSON cannot hold are tagged:
`{"$mapping": [[k, v], ...]}` for non-string keys, `{"$float": "inf"}`,
`{"$date": ...}`, `{"$bytes_base64": ...}`.

`_manifest.json` also records `generated_with`: package version, git commit, last
commit touching the loader sources, and the Python and PyYAML versions. That block
is informational and `--check` ignores it. Everything else in the manifest is
compared.

## Case map

Counts when this README was written (`expected/_manifest.json` has the current ones): config 74 (55 loaded, 19 errors), spec 49 (16 loaded, 33
errors), programmatic 19 (7 accepted, 12 rejected). 142 cases in total.

| Area | Contract / parity row | Cases |
|---|---|---|
| Minimal and empty files | C-01; parity §2 config row | `config/minimal`, `empty`, `comments-only`, `absent` |
| Provider blocks | C-01; parity §2 (`provider`, per-provider blocks) | `provider-ollama`, `provider-anthropic`, `provider-openai`, `provider-gemini`, `provider-openai-compatible-gemini`, `provider-azure-litellm-blocks`, `provider-no-block`, `provider-unknown` |
| Provider precedence (env pin, session picker) | C-01 | `provider-env-pin`, `provider-session-override`, `provider-session-override-pinned`, `provider-api-key-env-set` |
| Secret references | C-01, C-06 | `provider-api-key-secret-reference`, `execution-capsule-hyperv` (`guest_token_ref`), `capsule-secret-reference-uppercase`, `capsule-secret-reference-invalid` |
| Budgets | parity §2 (`time_minutes`, `max_tokens`) | `budgets`, `budgets-ollama-ignores-max-tokens`, `budgets-zero`, `budgets-null-time`, `budgets-numeric-strings`, `budgets-fractional` |
| Knowledge (R-2, §7.1 rows) | C-01, §7.1, R-2 | defaults (`chroma` / `all-MiniLM-L6-v2`): `knowledge-defaults`, `minimal`; explicit local + chroma + all-MiniLM-L6-v2: `knowledge-explicit-local-chroma`; `type: auto`: `knowledge-type-auto`; `docker`: `knowledge-docker`; `qdrant` + `vector_url`: `knowledge-qdrant-vector-url`; other embedding model (ARG-09): `knowledge-ollama-embedding-model`; `enabled`: `knowledge-disabled`, `knowledge-enabled-quoted-false`, `knowledge-enabled-yaml11-off`; `persist_dir`: `knowledge-persist-dir-relative`, `-absolute`, `-home`; `knowledge-empty-strings` |
| Execution environment and `capsule.*` | C-01; parity §2 (`execution`) | `execution-local`, `execution-capsule-hyperv` (incl. `network_mode: allowlist`, `egress_allowlist`), `execution-capsule-libvirt`, `execution-capsule-provisioned`, `execution-environment-unknown`, `capsule-*` (token env, allowlist, execution modes, strict booleans, numbers) |
| Unknown keys | C-01 ("unknown keys keep today's behaviour") | `config/unknown-top-level-keys`, `config/unknown-nested-keys`, `spec/unknown-keys`, `spec/staging-unknown-field` (the one place unknown keys are rejected) |
| Wrong types | C-01 | `config/root-list`, `root-scalar`, `wrong-type-*`; `spec/scalar-coercions`, `setup-as-string`, `steps-as-mapping`, `retries-*`, `steps-not-iterable` |
| YAML syntax and features | C-01 | `config/invalid-yaml-unclosed`, `invalid-yaml-tab-indent`, `duplicate-keys`, `yaml-anchors-merge`; `spec/invalid-yaml`, `duplicate-keys`, `cp1252-encoding` |
| All ten assertions | parity §2 assertions row | `spec/assert-all-ten`, `desktop-notepad`, `cli-command`, `browser-page`, `assert-expected-unvalidated` |
| `setup` / `teardown` / `retries` / `staging` / `collect` | parity §2 `*.test.yaml` row | `spec/setup-teardown-retries`, `assert-in-setup-teardown`, `staging-collect`, `empty-optional-sections`, `name-and-adapter-defaults` |
| Spec errors, one per distinct message | parity §2 `*.test.yaml` row | `spec/invalid-yaml`, `empty-file`, `not-a-mapping`, `missing-target`, `target-*`, `no-steps`, `assert-*`, `step-*`, `staging-*`, `collect-*` |
| Programmatic step metadata (strict validation) | parity §2 ("including the strict validation of programmatic step metadata") | `programmatic/accepted-*`, `programmatic/rejected-*` |

## Adding a case

1. Add one input file:
   - `cases/config/<case>.yaml`, with `<case>.load.json` beside it if it needs
     environment variables or the session provider;
   - `cases/spec/<case>.test.yaml`;
   - or `cases/programmatic/<case>.json`, using `construct` (`TestSpec`, `NLStep` or
     `AssertStep`) and `args`. Steps in `args.steps` are `{"class": "NLStep" |
     "AssertStep", "args": {...}, "mutate": {...}}` or `{"class": "raw", "value": ...}`.

   Names are lowercase-with-hyphens. Use a short leading comment to say what the
   case pins down. Unknown file names fail the exporter.
2. Keep inputs free of real credentials, usernames, hostnames and private paths.
   Key-like values must contain `TEST-ONLY`. Environment variables in sidecars use
   `ARGUS_GOLDEN_TEST_ONLY_*` names or the documented `ARGUS_*` names, with
   test-only values.
3. Run `python scripts/golden/export_config.py --write`, read the new
   `expected/...json`, then run `--check`.
4. If the interpretation is surprising, keep it. Add it to the list below.

## Notable current behaviours

All of these are recorded as-is. C-01 preserves them until a CFG-07 proposal
changes one.

**No warnings anywhere.** Neither loader warns or logs: `diagnostics` is empty in
every case.

**Unknown keys are silently ignored, with one exception.** Unknown keys are
ignored in the config file (top level and nested), at the top level of a test,
under `target:`, and beside an `assert:` block. They stay visible only in the
config's `raw`. The exception is `staging[]` entries, which reject unknown fields.

**Configuration (`load_config`)**

- **Knowledge type default.** The `knowledge.type` default is platform-dependent:
  `local` on Linux and Windows, `json` without `/proc` (macOS). It is not the
  `KnowledgeConfig` dataclass default `auto` that specification §7.1 describes.
  `vector_backend: chroma` and `embedding_model: all-MiniLM-L6-v2` are the defaults
  (R-2).
- **Truthiness of `knowledge.enabled`.** It uses `bool()`: the quoted string
  `"false"` means enabled. YAML 1.1 `off` / `no` mean disabled.
- **Zero means "no limit" for budgets.** `budgets.time_minutes: 0` and
  `max_tokens: 0` become `null`. A fractional `max_tokens` is truncated
  (`1500.9` becomes `1500`), and numeric strings are converted.
- **Ollama budgets.** With provider `ollama`, `max_tokens` is kept in the config but
  `make_budget()` never limits tokens.
- **Provider name is not validated.** An unknown provider name loads and falls back
  to the model `gemma3:9b`; it fails only when a client is created. A numeric
  provider (`provider: 42`) stays an integer. `provider: no` (YAML false) falls back
  to `ollama`.
- **`api_key` is used verbatim.** A `secret://argus/...` value in `api_key` is not
  resolved.
- **Secret reference spelling.** `execution.capsule.guest_token_ref` is stored at
  load time and validated only when a Capsule starts. The validator is
  case-sensitive and accepts only lowercase `secret://argus/<name>`. The
  `SECRET://ARGUS/NAME` spelling used in the specification and CLI help is rejected
  (`capsule-secret-reference-uppercase`).
- **Environment and session precedence.** `ARGUS_PROVIDER`, `ARGUS_MODEL`,
  `ARGUS_BASE_URL` and `ARGUS_API_KEY` override the file. A session `provider=`
  argument selects another configured provider and then ignores the generic
  `ARGUS_MODEL` / `ARGUS_BASE_URL` / `ARGUS_API_KEY`. `ARGUS_PROVIDER` beats the
  session argument.
- **Zero is falsy for some Capsule numbers.** `guest_port: 0`,
  `boot_timeout_seconds: 0` and `agent_timeout_seconds: 0` fall back to their
  defaults (`8765`, `120.0`, `60.0`). `memory_mb: 0` and `cpu_count: 0` stay `0`.
  An explicit `null` keeps `memory_mb` / `cpu_count` unset.
- **Allowlist entries are not validated.** `egress_allowlist` entries are
  stringified and stripped but not checked as CIDRs. `execution.environment` is
  not validated either (`cloud` loads).
- **Strict booleans.** Capsule booleans are strict. They accept YAML booleans and
  the strings `1/0`, `true/false`, `yes/no`, `on/off` (case-insensitive). The
  integer `1` is rejected.
- **Wrong section types.** A wrong-typed section raises a raw Python
  `AttributeError` (`'str' object has no attribute 'get'`) or `ValueError` from
  `int()` / `float()` / `dict()`, not a dedicated configuration error. An empty
  list (`knowledge: []`) is falsy and behaves like an absent section.
- **YAML loader behaviour.** Duplicate keys keep the last value. Anchors and `<<`
  merge keys are resolved. YAML errors are reported against `"<unicode string>"`,
  not the file path.

**Test specs (`load_spec`, `TestSpec`)**

- **Coercions.** `name`, `adapter` and `launch` go through `str()` (`name: 123`
  becomes `"123"`). `retries` goes through `int()` (`"3"` becomes `3`). A
  non-numeric `retries` raises a plain `ValueError` / `TypeError`, not `SpecError`.
  `continue_on_failure` uses `bool()`: `"false"` means true.
- **Name and adapter defaults.** Without `name`, the name is the file stem, which
  keeps `.test` (`name-and-adapter-defaults.test`). Without `adapter`, the adapter
  is `desktop-gui`.
- **String or mapping where a list is expected.** A string is iterated character by
  character (`setup: ab` gives two setup steps `a` and `b`). A mapping is iterated
  over its keys (`steps: {assert: ...}` gives one natural-language step with the
  text `assert`). An integer raises `TypeError`.
- **Launch values.** A falsy `launch` (`0`) is reported as a missing target.
- **Step order.** Steps always run setup, then steps, then teardown, whatever the
  key order. Only teardown entries equal to `close` (case- and space-insensitive)
  are normalized to `close`. A `close` in `setup` stays an ordinary step.
- **Assertions.** They are allowed in `setup` and `teardown`. Expected values are
  not type-checked (`exit_code_is: zero`, `text_visible: null` load), and the
  adapter is not checked against the assertion kind.
- **Encoding fallback.** Files that are not valid UTF-8 are read as cp1252.
- **Path normalization.** `staging` and `collect` convert `\` to `/` and strip
  whitespace. `sha256` is lower-cased. Windows aliasing is applied on every host:
  case-insensitive duplicates, reserved device names and trailing dots or spaces
  are rejected.
- **Programmatic specs.** `TestSpec(...)` validates step objects and `collect`. It
  does **not** validate `staging`: absolute or `..` paths and malformed digests are
  accepted. It also accepts an empty `steps` list, which the YAML parser rejects.
  `AssertStep` does not validate `expected`.

## Not covered here

- **Default model names.** The defaults are not listed as a table here, because they
  contain vendor model names. Read them from `argus/config.py` (`_default_model`,
  `DEFAULT_CONFIG`). Where a case omits its model, the resolved default is recorded
  in that case's expected file: `provider-no-block` records the OpenAI default. The
  `provider-anthropic` case sets its model explicitly.
- **Use-time behaviour.** Checks that happen only when the configuration is used
  are recorded only as far as `derived` shows them. This includes Capsule
  environment-variable overrides (`ARGUS_CAPSULE_*`) and per-session Capsule
  overrides in `make_execution_environment`, and backend resolution in
  `make_knowledge_store`. Those belong to later golden sets (P4/P6).
