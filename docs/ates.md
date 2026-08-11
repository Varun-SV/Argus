# ATES — Argus Test Evidence Specification

> **Status:** design specification. ATES is not yet implemented by the Argus runtime.

ATES defines the canonical evidence contract for Argus test execution. It is intentionally separate from Markdown, HTML, PDF, JUnit, dashboards, or any other presentation format.

The central rule is:

> **Execution produces evidence. Monitoring visualizes evidence. Documentation renders evidence.**

Every Argus run should eventually produce an ATES Core record automatically, even when the user does not explicitly request a report.

## Goals

ATES is designed to provide:

- consistent documentation across all Argus runs;
- step-by-step traceability from execution intent to observed evidence;
- clear separation between observed facts, deterministic assertions, executed actions, and AI interpretation;
- failure and checkpoint evidence without capturing screenshots for every action by default;
- artifact-level sensitivity handling so binary evidence does not become a credential-exfiltration path;
- provenance for execution intent/test definitions, Argus builds, models, execution environments, Nodes, Capsules, and artifacts;
- cryptographic artifact digests for corruption/integrity detection;
- source-identity commitments that do not turn low-entropy secrets into offline-verifiable public hashes;
- a manifest model that becomes tamper-evident only when bound to a trusted external, immutable, or cryptographic trust boundary;
- append-oriented audit history rather than silent mutation;
- authenticated/authorized supersession so a forged correction cannot silently replace audit evidence;
- immutable/versioned requirement identities for requirement-to-test-to-evidence traceability;
- recoverable evidence from interrupted/crashed runs, including ambiguous action dispatch outcomes;
- immutable per-attempt identities so retries never overwrite prior execution history;
- deterministic canonical final-status aggregation shared by renderers and Fleet;
- transactional evidence finalization so a run is not exposed as passed before required integrity work durably commits;
- schema-level pre-persistence redaction for all free-form evidence text and target-generated observations;
- context-safe rendering of untrusted application/test evidence;
- explicit trust status for rendered reports;
- a stable machine-readable foundation for reports, dashboards, audit packages, and future compliance mappings.

## Non-goals

ATES does not:

- record or expose private model chain-of-thought;
- make an LLM interpretation equivalent to an observed fact;
- require ISO/IEC material for native Argus operation;
- claim compliance with a regulated standard merely because a report resembles one;
- make screenshots mandatory for every normal action;
- require persisting a screenshot when artifact privacy policy cannot safely retain it;
- allow report configuration to suppress mandatory core evidence;
- claim that a locally stored hash and locally stored manifest are tamper-evident without an independent trust boundary;
- treat a rendered report as trusted merely because its source evidence manifest verifies;
- allow untrusted evidence text to become executable HTML/Markdown/XML/report content by default;
- require an interrupted run to fabricate an end timestamp or successful finalization event;
- require every execution mode to invent a pre-authored scripted test-case identity;
- let a later retry erase or rewrite evidence from an earlier attempt;
- let a mutable requirement ID silently stand in for multiple historical requirement meanings.

## ATES Core is always on

There should be no supported equivalent of:

```yaml
ates:
  enabled: false
```

Users may choose whether optional reports are rendered or how much non-core diagnostic material is retained, but every execution produces the mandatory canonical evidence record.

## Evidence profiles

ATES Core defines mandatory metadata and events. Profiles control additional capture.

### `minimal`

Intended for high-volume CI while still retaining ATES Core.

Additional capture is limited to essential execution and failure evidence.

### `standard` — default

Recommended normal profile.

Captures:

- ATES Core;
- failure evidence;
- explicitly requested checkpoints;
- relevant assertion observations;
- selected application/test artifacts;
- timing, retries, and execution-environment metadata.

**Default screenshot policy:** Argus attempts screenshot evidence for failures and explicit checkpoints, not for every successful action. A screenshot is persisted only when the artifact-level sensitivity/capture policy permits it. When safe persistence is not possible, Argus must redact/mask before persistence, store it only in an explicitly authorized protected evidence class, or suppress the screenshot and record why it was not retained.

### `forensic`

Intended for investigations and high-assurance runs.

May add:

- expanded observation snapshots;
- guest/application logs;
- more detailed event history;
- additional artifact collection;
- Failure Capsule references;
- environment and configuration fingerprints;
- transport/session metadata that is safe to retain.

Secrets, live bearer tokens, TLS private keys, and other credentials must never be included merely because forensic capture is enabled. Forensic profile changes retention/detail, not the requirement to classify and protect sensitive artifacts.

### `custom`

A custom profile may add evidence but cannot remove mandatory ATES Core records.

## Canonical data model

The exact serialization may evolve, but the logical model should remain stable.

### Run

Every execution has one immutable logical `run_id`.

Fields required from run creation include at least:

- `run_id`;
- `execution_kind`;
- execution-source identity/provenance appropriate to that `execution_kind`;
- Argus version/build/commit where available;
- `started_at`;
- execution environment type;
- adapter type;
- host/Node identity where applicable;
- Capsule/provider identity where applicable;
- guest/host operating-system information where applicable;
- model/provider identity;
- effective evidence profile;
- configuration fingerprint/commitment that follows the secret-safe source-identity rules below.

#### Execution kind and source identity

ATES is always-on across Argus execution modes, so a Run must not assume that every execution originates from a pre-authored scripted test case.

`execution_kind` identifies the execution contract. Initial values should include at least:

- `scripted` — execution of a pre-authored Argus test specification;
- `roam` — exploratory/free-roam execution without a pre-authored scripted test case.

Future execution kinds may be added through schema evolution, but they must define their source/provenance requirements explicitly rather than borrowing fields from another kind.

For a scripted run, source identity is mandatory and should include the immutable test/spec identity plus a **secret-safe source commitment**, for example:

```yaml
execution_kind: scripted
source:
  kind: test_spec
  test_case_id: TEST-NETWORK-003
  commitment:
    method: sha256_redacted_canonical
    value: "sha256:..."
    canonicalization_profile: ates-source-v1
```

A plain SHA-256 digest of the raw authored specification is allowed only when the canonical source is known not to contain secret-bearing or otherwise sensitive low-entropy values. When a source can contain passwords, tokens, sensitive assertion values, private URLs, or similar material, ATES must use one of these approaches instead:

- replace secret values with stable secret references/placeholders **before** canonical hashing;
- hash a canonical redacted representation whose redaction semantics are recorded;
- use a keyed commitment such as HMAC where the verification key is protected outside ordinary evidence;
- use a salted commitment where the salt/verification material is protected according to policy and is not published in a way that restores cheap offline guessing.

A low-entropy secret must not become publicly dictionary-checkable merely because ATES needs provenance. The commitment method, canonicalization/redaction profile, and verification requirements must be recorded so a verifier knows what identity property is actually being claimed.

For a roam run, **test-case identity/digest is not required and must not be fabricated**. Instead, the Run records the immutable inputs that define that exploratory session, using the same secret-safe commitment rules, for example:

```yaml
execution_kind: roam
source:
  kind: roam_session
  objective_commitment:
    method: sha256_redacted_canonical
    value: "sha256:..."
  roam_config_commitment:
    method: hmac-sha256
    value: "hmac:..."
    key_ref: protected://ates-source-key
  seed_or_policy_ref: "ROAM-POLICY-7"
```

The exact roam source fields may evolve, but they must let a reviewer distinguish two materially different exploratory sessions without pretending that either was a scripted test case. If the user supplied no explicit objective, ATES may record an explicit `objective_present: false`/equivalent rather than inventing one. Any retained objective text remains subject to the same privacy/redaction policy as other evidence.

Configuration fingerprints, prompt/template identifiers, and other Run-level digests follow the same rule: do not hash raw secret-bearing material into a public always-on record when that digest enables practical offline guessing.

Requirement traceability is conditional in the same way: scripted runs may carry declared requirements and verification links; roam runs may produce Findings and evidence without falsely claiming requirement coverage.

Completion fields that belong to canonical execution evidence are **not required until execution finalization**. These include:

- `ended_at`;
- `final_status`;
- final canonical event/sequence information needed to close the execution stream.

The evidence-manifest revision/digest that binds the finalized stream is **derived from the exact final canonical bytes** and is not embedded back into the same bound Run/`RUN_COMPLETED` bytes. Doing so would create a self-referential digest cycle. The completion/finalization transaction below defines how Argus can prepare the exact candidate final event and manifest together before exposing the run as completed.

A run whose stream has no valid, durably committed `RUN_COMPLETED` event remains a valid **incomplete/recoverable run**, not an invalid ATES record. Renderers and validators must preserve its available evidence and report that final status/end time are unknown rather than inventing them.

A derived `run.json` may expose lifecycle state such as:

```yaml
run_id: RUN-01K...
execution_kind: roam
started_at: "2026-08-11T01:20:00+05:30"
lifecycle_state: incomplete
ended_at: null
final_status: null
last_sequence: 84
```

If Argus later performs recovery/reconciliation, it may append an explicit recovery/terminal event such as `RUN_MARKED_INCOMPLETE`, including the actor/recovery process and reason. Recovery must never synthesize a normal `RUN_COMPLETED` event for execution that was not observed to complete.

#### Canonical final status

`final_status` is a **derived canonical outcome**, not an arbitrary producer label. All ATES producers, Fleet aggregation, Markdown/HTML/JUnit renderers, and audit tooling must apply the same aggregation semantics to the same canonical event history.

A normal `RUN_COMPLETED` event may use these initial terminal values:

- `passed` — execution completed reliably and every pass precondition below is satisfied;
- `failed` — execution completed reliably enough to evaluate the intended test, but one or more required deterministic/declared acceptance conditions failed;
- `error` — infrastructure, runner, adapter/provider, evidence-integrity, or unresolved execution uncertainty prevents a reliable pass/fail evaluation;
- `cancelled` — an explicit cancellation terminated execution before normal completion, with no higher-precedence error condition.

An interrupted stream without a valid `RUN_COMPLETED` keeps `final_status: null` and `lifecycle_state: incomplete`; `RUN_MARKED_INCOMPLETE` is recovery metadata and must not invent a normal terminal result.

Normative aggregation precedence is:

1. **`error`** if a required execution/evidence condition is unreliable or unresolved, including an unreconciled `ACTION_OUTCOME_UNKNOWN`, an unreconciled canonical event gap/conflict, a required environment/adapter failure, or another policy-defined condition that prevents trustworthy evaluation;
2. **`failed`** if evaluation is reliable but any required assertion/acceptance condition is failed, or a deterministic/policy-validated fail condition is present;
3. **`cancelled`** if execution was explicitly cancelled before a normal result and neither `error` nor an already-established `failed` result has higher precedence under the active test policy;
4. **`passed`** only when none of the above apply and every required assertion/step/acceptance condition that must complete has reached a passing/satisfied terminal state.

A producer must **not** emit or expose `passed` when any of these remain true:

- a required assertion is failed, errored, missing, or unevaluated;
- a required Step has no policy-satisfying terminal attempt;
- a non-idempotent/destructive Action has an unresolved post-dispatch outcome;
- canonical evidence has an unreconciled sequence gap/conflict or other integrity condition required for finalization;
- required retained artifacts have not been closed and successfully hashed/validated for the final evidence snapshot;
- the required evidence manifest/finalization transaction for the active profile has not durably committed;
- execution encountered an environment/adapter/runner error that makes the intended result unreliable;
- a declared deterministic or authenticated policy rule marks the run as failed/error.

Model-generated Findings/Interpretations do **not** automatically convert a deterministic pass into failure. A Finding affects canonical `final_status` only when the test specification or an explicit validated policy maps that Finding/classification to a fail/error condition; an unverified model inference alone is not sufficient to override deterministic assertion evidence.

Renderers may present additional labels such as warning, flaky, quarantined, or findings-present, but those are annotations unless a versioned policy explicitly maps them into the canonical status rules above.

### Step

A Step represents stable logical execution intent at a point in the run. In scripted execution it normally corresponds to authored test intent; in roam execution it may represent an exploratory goal/subgoal emitted by the roam engine without implying a pre-authored test case.

A non-sensitive logical Step example is:

```yaml
step_id: STEP-0002
instruction:
  type: natural_language
  text: "Navigate to Network"
```

Step instructions are subject to the **same schema-level secret handling as Actions and Assertions**. ATES Core must not persist an authored instruction such as `Type password123 into the password field` verbatim merely because the later Action payload is redacted.

A sensitive Step can instead be represented as:

```yaml
step_id: STEP-0003
instruction:
  type: natural_language
  text: "Type <redacted> into the password field"
  sensitive_fields:
    - text
  redaction:
    reason: secret_or_user_input
    plaintext_persisted: false
  secret_refs:
    - SECRET-login-password
```

Where possible, authored tests should use explicit secret references/placeholders rather than embedding literal credentials in instruction text. If a literal is present, the canonical Step representation must be transformed/redacted **before** it is appended to always-on evidence. Low-entropy secret text must not be replaced with a public unsalted digest.

#### Step attempts and retries

`step_id` identifies the stable logical intent. Every actual execution of that intent has its own immutable `step_attempt_id` and monotonically increasing attempt ordinal beneath that Step.

Conceptually:

```yaml
step_id: STEP-0003
step_attempt_id: STEPATT-01K-A
attempt: 1
started_at: "..."
ended_at: "..."
status: failed
```

A retry creates a **new** attempt rather than mutating the prior one:

```yaml
step_id: STEP-0003
step_attempt_id: STEPATT-01K-B
attempt: 2
started_at: "..."
status: running
```

Required semantics:

- `step_id` is stable across retries; `step_attempt_id` is globally/run-unique and never reused for another attempt;
- attempt ordinals are consecutive for the logical Step and never renumbered after evidence is appended;
- `STEP_STARTED`, attempt completion/failure/retry events and all Actions, Observations, Assertions, checkpoints, errors, and timing generated inside an attempt reference the exact `step_attempt_id` in addition to the logical `step_id` where applicable;
- a failed/errored/ambiguous first attempt remains immutable evidence after a later retry passes; producers must not overwrite it with the later result;
- each attempt records its own lifecycle, status, start/end/duration, retry reason, and relevant evidence references;
- the logical Step's derived status is computed from its attempt history plus the versioned retry policy (for example, pass-on-any-success versus all-attempts-must-pass), while reports must still expose the complete attempt history;
- an ambiguous Action outcome remains attached to the attempt that dispatched it and may block creation of a new attempt until the action-level reconciliation policy says retry is safe;
- a retry triggered by infrastructure error, assertion failure, policy decision, or human action records that reason rather than presenting the new attempt as the first execution.

This preserves retry chronology, durations, and causal ownership of evidence without conflating a logical Step with one particular execution attempt.

### Action

An Action records what Argus actually attempted to execute, but ATES Core must not blindly persist secret-bearing action payloads.

A safe example is:

```yaml
action_id: ACTION-0041
step_id: STEP-0002
step_attempt_id: STEPATT-01K-A
type: click
target:
  name: "Network & Internet"
  control_type: ListItem
status: succeeded
```

The evidence representation should be based on the normalized action after policy validation, not only the model's raw proposal, **subject to ATES redaction rules**.

#### Sensitive action values

Potentially secret-bearing values require a schema-level representation that can preserve execution meaning without preserving plaintext. ATES Core must support at least:

- explicit `sensitive: true` / secret-reference metadata from the test/action source;
- per-field redaction metadata;
- a redacted canonical value such as `<redacted>`;
- non-secret structural metadata such as target identity, argument count, length, or executable name where safe.

For example:

```yaml
action_id: ACTION-0042
type: type
parameters:
  text: "<redacted>"
sensitive_fields:
  - parameters.text
redaction:
  reason: secret_or_user_input
  plaintext_persisted: false
```

The following classes should be conservative by default because the runtime cannot reliably infer every application secret:

- typed text/password-like input: do not persist plaintext by default;
- URLs: strip userinfo and redact policy-designated sensitive query/fragment values before persistence;
- shell/CLI commands: store a redacted/structured representation when arguments may contain secrets, rather than automatically persisting the raw command string.

An implementation may allow explicitly authorized plaintext evidence for known non-sensitive values, but **always-on ATES Core must not require plaintext secret-bearing payloads**. Low-entropy secrets should not be replaced with an unsalted plain digest that enables offline guessing.

#### Dispatch commitment and ambiguous outcomes

Executing an Action can cause an external side effect that occurs before Argus is able to append the normal outcome event. ATES must therefore distinguish **not dispatched** from **dispatch committed but outcome unconfirmed**.

After policy validation and before invoking an adapter/provider operation that can cause the side effect, the authoritative producer must durably append an `ACTION_DISPATCH_COMMITTED` (or schema-equivalent) event containing a stable `action_operation_id` and a reference to the sanitized Action record. Secret-bearing parameters remain redacted; the dispatch record does not create a second plaintext copy.

Conceptually:

```yaml
event_type: ACTION_DISPATCH_COMMITTED
payload:
  action_id: ACTION-0042
  action_operation_id: ACTOP-01K...
  step_attempt_id: STEPATT-01K-A
  policy_result_ref: POLICY-0091
```

Required semantics:

- the dispatch-commit event is durably appended **before** the adapter call or other start of the external side effect;
- the same stable `action_operation_id` follows the logical operation through adapter/provider dispatch, retries, reconciliation, and final outcome events;
- successful/failed confirmed outcomes are recorded by `ACTION_EXECUTED`/an equivalent terminal action event referencing that operation ID;
- if execution stops after dispatch commitment but before a confirmed terminal outcome is durably recorded, recovery must represent the action as `outcome_unknown_after_dispatch` (for example via `ACTION_OUTCOME_UNKNOWN`) rather than treating it as never executed;
- a recovered unknown outcome is not silently converted to success or failure by inference;
- non-idempotent or destructive actions with an unknown post-dispatch outcome must **not be blindly retried**; Argus must first reconcile via a trusted adapter/provider operation-status mechanism, deterministic observation, explicit policy, or human decision that makes the retry safe;
- where an adapter/provider can deduplicate by operation identity, retries must reuse the same `action_operation_id` rather than minting another logical action;
- where no deduplication/status mechanism exists, the evidence must preserve the uncertainty and the runner may fail/stop rather than risk a duplicate side effect.

The conservative boundary is intentional: a crash after `ACTION_DISPATCH_COMMITTED` but before the actual adapter call can produce a false-positive **possible dispatch**, but it cannot erase a side effect that may already have occurred. This is safer for audit and retry behavior than treating an ambiguous destructive action as definitely unexecuted.

### Observation

An Observation records data directly measured or captured from the execution environment. Target-generated text and values are **untrusted and potentially sensitive by default**; ATES must not assume that accessibility, DOM, CLI, log, URL, form-field, or application text is safe merely because it is directly observed.

A non-sensitive permitted example is:

```yaml
observation_id: OBS-0042
step_attempt_id: STEPATT-01K-A
source: accessibility_tree
captured_at: 2026-08-11T01:21:43.819+05:30
capture_policy: OBS-POLICY-3
sensitivity:
  state: classified_safe
facts:
  visible_text:
    - "Network & Internet"
    - "Wi-Fi"
    - "Ethernet"
```

Observation payloads use a **pre-append capture decision**. A conforming producer must decide, before ordinary canonical persistence, whether each potentially sensitive payload/field is allowed, redacted, structurally summarized, protected, or suppressed. Merely saying that an Observation is "subject to policy" after raw text has already been written is not sufficient.

Required conservative semantics:

- every Observation records the capture/redaction policy or policy version that governed the persisted representation;
- application/target-derived values that may contain credentials, tokens, private URLs, customer data, personal data, form values, headers, environment values, CLI output, or similar secrets are not automatically persisted verbatim;
- known password/secret input values, credential-bearing URL components, authorization headers, environment secrets, clipboard-like secret values, and equivalent high-risk fields default to **redacted or suppressed** in ordinary ATES Core;
- if sensitivity cannot be classified with sufficient confidence for a policy-designated high-risk source/field, the default is omission/redaction/structural metadata rather than plaintext persistence;
- explicit source metadata/sensitivity markers from the adapter/application/test policy are honored and cannot be widened by the model;
- a deterministic assertion may retain a safe boolean/result/reference instead of copying a sensitive observed value into `facts`;
- when a field is redacted/suppressed, the Observation records field-level metadata/reason without preserving a public unsalted digest of a low-entropy secret;
- explicitly authorized unredacted sensitive observations belong only in a protected evidence class with the same access/encryption/retention controls used for protected artifacts; authorization is not implied by `standard` or `forensic` profile alone;
- model instructions, checkpoints, or report configuration cannot override the Observation capture policy and force forbidden plaintext into canonical evidence.

A redacted Observation can therefore look like:

```yaml
observation_id: OBS-0043
source: dom
capture_policy: OBS-POLICY-3
facts:
  username_field_present: true
  password_field_present: true
  password_value: "<redacted>"
sensitive_fields:
  - facts.password_value
redaction:
  reason: secret_input
  plaintext_persisted: false
```

Observations are evidence. They are not AI conclusions, but direct observation does not make the payload non-sensitive.

### Assertion

Assertions are recorded independently from AI interpretation and should reference the exact Step attempt that produced/evaluated them where applicable.

```yaml
assertion_id: ASSERT-0008
step_id: STEP-0003
step_attempt_id: STEPATT-01K-B
kind: text_visible
expected: "Ethernet"
result: passed
method: deterministic
observation_id: OBS-0043
```

Sensitive assertion inputs/expected/actual values must use the same redaction model when retaining plaintext would expose secrets. Required assertions participate in canonical final-status aggregation.

### Interpretation

An Interpretation is an AI-generated conclusion based on cited evidence.

A non-sensitive example is:

```yaml
interpretation_id: INT-0012
source: llm
model: example-model
summary: "The application appears unable to establish the requested connection."
evidence:
  - OBS-0088
```

Interpretation summaries, labels, rationales, and other free-form model output are **untrusted and potentially secret-bearing evidence text**. They must be passed through the same pre-append sensitivity/redaction pipeline as Step/Action/Assertion/Observation fields. A model repeating a password, bearer token, private URL, customer value, or secret from its prompt/observation must not cause ATES Core to persist that plaintext merely because the value appears in generated prose.

When text is sensitive, the canonical Interpretation stores a redacted/structured representation plus sensitivity metadata, for example:

```yaml
interpretation_id: INT-0013
source: llm
model: example-model
summary: "Authentication failed for credential <redacted>."
sensitive_fields:
  - summary
redaction:
  reason: secret_or_sensitive_application_data
  plaintext_persisted: false
evidence:
  - OBS-0090
```

ATES must preserve the distinction between:

- **fact** — directly observed or measured;
- **action** — actually executed;
- **assertion** — deterministic evaluation;
- **interpretation** — inferred by a model or human.

### Finding

A Finding describes a possible defect, anomaly, or noteworthy behavior.

Findings should reference the evidence supporting them and state whether their severity/classification is deterministic, rule-based, human-assigned, or model-inferred.

Finding titles, descriptions, reproduction summaries, suggested remediations, human notes, and other free-form fields are subject to the **same pre-persistence sensitivity/redaction contract** as Interpretations. This applies regardless of whether the text came from a model, a human, an imported tool, or application-derived content. Known secret plaintext must not be persisted in ordinary ATES Core fields, and low-entropy secrets must not be replaced with public unsalted hashes.

A Finding only affects canonical `final_status` through an explicit versioned deterministic/authenticated test or policy rule; a model-inferred Finding alone is not a normative failure result.

### Free-form evidence text safety

The redaction requirement is schema-wide, not limited to a fixed list of record types. Any canonical field that can contain model-generated, human-authored, imported, or application-derived free-form text must support sensitivity classification and a safe persisted representation **before append**. This includes at least:

- Step instructions/goals;
- Action parameters/targets where applicable;
- Observation text/values;
- Assertion expected/actual values;
- Interpretation summaries/rationales;
- Finding titles/descriptions/reproduction/remediation text;
- error messages and exception details;
- logs or log excerpts embedded in JSON evidence;
- model/tool messages that a future schema intentionally retains;
- operator/auditor annotations that become canonical execution evidence.

Redaction metadata must identify which field was transformed and the applicable policy/reason. The runtime may retain additional unredacted material only in an explicitly authorized protected evidence class; it must not write ordinary canonical plaintext first and attempt to redact it afterward.

### Checkpoint

A checkpoint is an explicit documentation/evidence request embedded in the execution intent/specification.

Example future scripted syntax:

```yaml
steps:
  - "Open Network settings"
  - checkpoint:
      name: network-settings-open
  - assert:
      text_visible: "Ethernet"
```

A standard checkpoint should request, where supported and permitted by artifact/Observation policy:

- screenshot or a documented suppressed/redacted substitute;
- accessibility/DOM/CLI observation appropriate to the adapter with the secret-safe Observation capture rules above;
- timestamp;
- current step/attempt/action relationship;
- environment identity;
- integrity metadata for generated artifacts.

Checkpoint content remains subject to privacy/redaction policy. A checkpoint never overrides a rule that forbids persistence of sensitive binary or structured evidence.

### Artifact

Every retained file is represented by an Artifact record. Artifact metadata must describe not only integrity but also sensitivity/protection state.

```yaml
artifact_id: ART-01892
kind: screenshot
path: artifacts/STEP-017-checkpoint.png
sha256: "..."
size_bytes: 381223
source: capsule
sensitivity: internal
protection:
  state: redacted
  policy_id: EVIDENCE-POLICY-3
  plaintext_or_unredacted_persisted: false
```

Artifact paths are **package-relative identifiers, not arbitrary filesystem paths**. A conforming `path` must be a canonical relative path rooted beneath the run's designated artifact directory (for example `artifacts/...`). Absolute paths, drive/UNC prefixes, `..` traversal, empty/ambiguous segments, encoded traversal aliases, or paths that escape the artifact root after normalization are invalid.

Verifiers, renderers, importers, and package extractors must treat artifact paths as untrusted input and use race-safe rooted access. They must reject or safely ignore symlinks, junctions/reparse points, device nodes, FIFOs/sockets, and other non-regular targets where a regular artifact file is expected. Implementations should prefer directory-handle/rooted APIs (`openat`-style or platform equivalents), disable link following where available, and verify the opened object remains beneath the intended root rather than performing a check-then-open sequence vulnerable to races.

Archive/package extraction must apply the same confinement before creating files: no member may overwrite or create content outside the designated evidence root, and link entries must not be used to redirect later extraction or hashing outside that root.

Artifacts collected from Capsules must continue to obey the explicit staging/collection authority already enforced by the Capsule execution boundary.

#### Artifact capture, sensitivity, and pre-write handling

Binary evidence such as screenshots, recordings, logs, memory-adjacent diagnostics, and application-exported files can contain secrets that cannot be made safe by redacting only the surrounding JSON record. ATES therefore requires artifact-level handling before ordinary persistent storage.

Every artifact-capable capture path must support a policy decision that results in one of the following states:

- **persist-safe** — content is classified as allowed for the target evidence store;
- **redacted/masked** — sensitive regions/fields are transformed before the persistent artifact is committed;
- **protected** — unredacted content is retained only when explicitly authorized in an encrypted/access-restricted evidence class with separate access and retention policy;
- **suppressed** — no binary artifact is persisted because it cannot be captured safely under the active policy.

Required semantics:

- sensitivity/classification and the applied capture policy are recorded in Artifact/checkpoint/failure metadata;
- the normal artifact path must not receive an unredacted screenshot first and rely on a later cleanup/redaction pass;
- if redaction requires transient raw pixels/bytes, processing must occur in protected ephemeral memory/storage and the raw form must be destroyed as soon as practical rather than entering canonical ordinary storage;
- a protected unredacted artifact requires explicit authorization, encryption at rest where the deployment supports protected evidence, access-control metadata, and retention policy; merely selecting `forensic` is not authorization;
- when capture is suppressed, ATES records an `ARTIFACT_SUPPRESSED`/equivalent evidence event containing the requested artifact kind, policy reason, timestamp, and related checkpoint/failure IDs without leaking the suppressed content;
- hashes/manifests cover the **persisted representation** (for example the redacted image), while provenance records that transformation occurred;
- renderers must not imply a missing/suppressed screenshot was captured when it was not.

This rule applies before the default standard-profile failure/checkpoint screenshot is committed, so a visible password or token does not become an always-on evidence leak.

### Requirement

ATES should support first-class requirement traceability for executions that declare requirements, but traceability must bind to an **immutable requirement meaning**, not merely a human-friendly ID that may be reused after the requirement changes.

A future scripted test may declare a requirement identity such as:

```yaml
requirements:
  - requirement_id: REQ-NET-042
    version: "3"
    source:
      system: product-requirements
      revision: "release-2026.08"
    commitment:
      method: sha256_redacted_canonical
      value: "sha256:..."
      canonicalization_profile: ates-requirement-v1
    description: "The application shall display the active network connection."
```

The stable display ID (`REQ-NET-042`) is not sufficient by itself. The immutable identity is the tuple of the declared requirement namespace/source plus version/revision and/or a secret-safe canonical commitment that fixes the exact meaning/acceptance criteria reviewed for that run.

If the requirement wording, acceptance criteria, normative source revision, or other meaning-bearing content changes, the new material receives a **new immutable requirement identity** even when the organization keeps the same display ID.

An assertion references the exact immutable requirement identity, for example:

```yaml
verifies:
  - requirement_id: REQ-NET-042
    version: "3"
    commitment: "sha256:..."
```

This creates a traceability chain such as:

```text
REQ-NET-042@v3/sha256:...
  -> TEST-NETWORK-003
  -> RUN-...
  -> STEP-...
  -> STEPATT-...
  -> ASSERT-0007
  -> OBS-0019
  -> ART-0004
```

Required semantics:

- reports may display the friendly requirement ID/description, but joins, coverage calculations, approvals, and cross-run historical comparisons use the immutable identity;
- a historical run that verified `REQ-NET-042@v2` must never be silently presented as evidence for changed `REQ-NET-042@v3`;
- requirement importers/connectors record the external source system and immutable source revision where available;
- requirement commitments follow the same secret-safe commitment rules as test/source identity if requirement text can contain sensitive low-entropy values;
- mapping an old run to a new/changed requirement requires an explicit authenticated/superseding traceability record rather than rewriting the historical assertion;
- a roam run without declared requirements does not fabricate this chain; it records exploratory Findings/evidence and may later be linked through an explicit authenticated mapping record.

## Event stream

The preferred runtime representation is an append-oriented event stream plus versioned manifests.

Representative events:

```text
RUN_STARTED
ENVIRONMENT_PREPARED
CAPSULE_CREATED
TARGET_LAUNCHED
STEP_ATTEMPT_STARTED
OBSERVATION_CAPTURED
ACTION_PROPOSED
ACTION_POLICY_VALIDATED
ACTION_DISPATCH_COMMITTED
ACTION_EXECUTED
ACTION_OUTCOME_UNKNOWN
ASSERTION_EVALUATED
STEP_ATTEMPT_COMPLETED
STEP_RETRY_SCHEDULED
CHECKPOINT_CAPTURED
ARTIFACT_SUPPRESSED
FINDING_RECORDED
ARTIFACT_COLLECTED
TARGET_CLOSED
FAILURE_CAPSULE_RETAINED
ENVIRONMENT_RELEASED
RUN_COMPLETED
RUN_MARKED_INCOMPLETE
```

Not every adapter/environment or execution kind will emit every event.

`RUN_COMPLETED` is the **commit record of successful run finalization**, not the first step of finalization. Its `final_status` must be derived using the canonical aggregation rules above. It does **not** contain the digest of an evidence manifest whose input includes that same event. Instead, the exact candidate `RUN_COMPLETED` bytes and the exact manifest that will bind the resulting final evidence snapshot are prepared together and durably published as one logical finalization transaction as described below.

### Completion and integrity finalization transaction

A run must not become canonically `completed`—and especially must not become `passed`—while required evidence-integrity work is still pending.

Before exposing `RUN_COMPLETED`, the authoritative producer must:

1. stop/close mutable artifact producers for the run and determine the exact persisted artifact representations governed by the capture/privacy policy;
2. durably flush all prior canonical events and verify that required event sequences, action outcomes, assertions, and other completion preconditions are reconciled;
3. hash/validate every retained artifact and canonical input that the active evidence profile requires in the final evidence manifest;
4. derive the candidate canonical `final_status`, where any required integrity/finalization failure has `error` precedence rather than allowing `passed`;
5. construct the **exact next `RUN_COMPLETED` event bytes** (including its final sequence/status/end time) without embedding the manifest digest, then construct the candidate final canonical evidence snapshot and immutable evidence-manifest bytes/digest that bind that event and the retained artifacts;
6. durably commit/publish the final event and its required manifest as one **logical transaction** before readers, Fleet, or renderers are allowed to observe `lifecycle_state: completed`.

A filesystem/database implementation does not need magical multi-file atomicity, but it must provide equivalent crash semantics—for example a staged finalization directory plus fsync/atomic rename, a database transaction, or a durable journal/commit marker. A reader must not treat a staged `RUN_COMPLETED` as authoritative until the corresponding finalization commit marker/manifest state is durable.

Failure semantics are normative:

- if artifact hashing, canonical-evidence validation, manifest construction, or required manifest persistence fails **before commit**, a `passed` completion must not become visible;
- if the failure can itself be safely recorded, the producer recomputes the terminal outcome as `error` and performs a fresh finalization transaction over that exact terminal event;
- if the storage/finalization mechanism is too broken to commit a trustworthy terminal record, the run remains `incomplete/recoverable` rather than claiming `passed` or fabricating success;
- recovery after a crash inspects the durable finalization journal/commit state and either completes the already-prepared identical transaction or rolls it back/retries safely; it must not expose half of the transaction as a completed run;
- optional external signatures/transparency bindings may occur after ordinary native finalization **only when the active profile does not require them**; if a profile requires a tamper-evidence binding as a completion condition, that binding (or durable proof that it committed) is part of the required finalization gate and failure cannot yield `passed` under that profile;
- derived `run.json`, dashboards, JUnit, and Fleet must use the committed finalization state rather than assuming that the presence of candidate event bytes alone means completion.

This avoids both failure modes: the manifest digest is not self-referential, and a later manifest/hash failure cannot leave behind a canonical `passed` run that never satisfied its required evidence-integrity contract.

For Actions that can cause external side effects, `ACTION_DISPATCH_COMMITTED` is the durable pre-side-effect boundary. Once it exists without a confirmed terminal action outcome, recovery treats the operation as potentially executed and must apply the ambiguous-outcome rules above before any retry.

Step-attempt lifecycle events carry both the stable logical `step_id` and immutable `step_attempt_id`, so transport/replay and later renderers cannot conflate retries.

### Event envelope and canonical ordering

Every ATES event must carry a stable event identity and an ordered sequence within its run. The envelope should contain at least:

```yaml
ates_version: "0.1"
run_id: RUN-01K...
event_id: EVT-01K...
sequence: 42
event_type: ACTION_EXECUTED
occurred_at: "2026-08-11T01:21:43.819+05:30"
```

Required semantics:

- `event_id` is stable for the logical event and is reused when the same event is retried in transport;
- the authoritative run producer assigns `sequence` values **consecutively and gap-free** in canonical append order;
- the next sequence number is committed atomically with the durable local event append; implementations should not reserve a number and then abandon it;
- transport retries resend the original `event_id` and `sequence`, not a replacement event;
- consumers deduplicate by event identity and treat byte/logically identical retries as idempotent;
- conflicting payloads for the same `event_id` or `(run_id, sequence)` are integrity/protocol errors and must not be silently reconciled;
- consumers reconstruct canonical execution order from the per-run sequence, not network arrival order.

If an implementation architecture genuinely requires advance reservation and a reserved sequence cannot produce its intended event, the producer must fill that exact sequence with an explicit `SEQUENCE_TOMBSTONE`/skip event before later sequences are considered reconciled. The tombstone must record a reason and must never masquerade as the abandoned event. Consumers can therefore distinguish an intentional omission from transport loss.

A gap with neither the original event nor a valid tombstone remains a missing-event condition. A completed manifest/report must not silently close over such a gap; the run remains incomplete until reconciliation or explicit incomplete finalization.

These rules are required before ATES is used as the canonical vocabulary for Fleet streaming, because Nodes may reconnect, retransmit, or deliver events out of order.

Events should otherwise contain stable entity identifiers and references rather than duplicating large payloads.

## Storage layout

A possible on-disk layout is:

```text
.argus/runs/<run-id>/
  evidence.jsonl
  run.json
  manifests/
    manifest-0001.json
    package-manifest-0001.json
  approvals.jsonl
  artifacts/
  reports/
    report.md
    report.html
    junit.xml
```

`evidence.jsonl` is the canonical execution-event history. Reports are derived views and should be reproducible from canonical evidence where practical. `approvals.jsonl` is a detached audit ledger described below and does not mutate the immutable manifest revision it approves. Detached approval storage still requires authentication/integrity verification before a record is treated as a valid approval.

A convenience/derived `run.json` may include detached references to finalized manifest revisions for navigation, but those references are not part of the evidence bytes bound by the referenced manifest unless a later outer/package revision explicitly binds both. A manifest must never bind a file that embeds that manifest's own digest.

## Integrity model

### Artifact hashing

Retained artifacts should be hashed with SHA-256 and included in the applicable run manifest revision.

A digest stored alongside mutable evidence detects accidental corruption and modification **only while the manifest/digest itself remains trusted**. A process that can replace both an artifact and its stored digest can recompute both, so hashes alone must not be described as tamper-evident.

### Versioned immutable evidence manifests

Each finalized evidence snapshot is represented by an immutable evidence-manifest revision, for example `manifest-0001.json`.

An evidence-manifest revision should bind:

- the canonical execution evidence snapshot/range it covers;
- retained canonical artifacts included in that revision;
- renderer inputs/configuration identifiers where applicable;
- relevant schema/version identifiers;
- previous evidence-manifest revision/digest when a chained revision is created.

For the terminal run snapshot, the manifest publication participates in the completion/finalization transaction above: the exact final event bytes and manifest inputs are prepared before completion becomes visible, and readers consider the run completed only after the logical transaction commits.

Once published/finalized, an evidence-manifest revision and its digest are immutable. Additional audit material never rewrites the revision it references.

The manifest's own digest is metadata **about** that immutable manifest object and is stored/referenced outside the byte range the digest covers. A chained later manifest may reference the previous manifest's digest, but no revision may require its own digest to appear inside its own hashed bytes.

Rendered reports are derived outputs and are **not automatically covered** merely because their inputs are bound by an evidence manifest. Their trust boundary is defined separately below.

### Tamper-evidence boundary

ATES may claim that an evidence package/revision is **tamper-evident** only when its manifest digest is bound outside the mutable evidence set by at least one approved mechanism, for example:

- a cryptographic signature whose verification key is independently trusted;
- a trusted external transparency/audit service that records the manifest digest;
- immutable/WORM storage or another storage boundary that prevents the evidence producer from rewriting both evidence and its recorded digest after finalization.

A native run without such a binding remains useful for integrity/corruption detection, but must not be labeled tamper-evident.

### Signing and audit profiles

ATES should support cryptographic signing of manifest revisions without requiring signatures for ordinary users.

Possible evidence chain:

```text
artifact hashes
    -> immutable evidence-manifest revision
        -> manifest digest
            -> signature / trusted external digest / immutable binding
```

A future regulated/audit profile may require one or more concrete binding mechanisms and must record which mechanism was used, its verifier/reference, and the binding timestamp.

## Audit history and supersession

Corrections should be append-oriented, but append-only storage is not enough by itself: a correction that changes which record is treated as authoritative is a privileged audit action.

A prior Finding, classification, approval, interpretation, traceability mapping, or other auditable record should not silently disappear. A later correction/supersession record should identify at least:

- `correction_id` / new record ID;
- prior record ID and exact revision/manifest context being superseded;
- reason;
- claimed actor identity;
- timestamp;
- required authorization/role or policy scope;
- verification status/method and the authenticated actor/credential reference.

Conceptually:

```yaml
correction_id: CORR-0009
supersedes: FINDING-0042
reason: "Reviewer confirmed this was a test-environment issue"
actor: reviewer@example.invalid
corrected_at: "..."
authorization:
  required_permission: evidence.supersede_finding
verification:
  status: verified
  method: signature
  signer_key_id: "qa-reviewer-key-7"
  binding_ref: "..."
```

Required semantics:

- an actor name/string is descriptive only; it must not authorize a supersession by itself;
- before a correction is applied as authoritative, the actor identity and permission to supersede that class/scope of record must be authenticated under the applicable versioned authorization policy;
- every correction carries a verification state such as `verified`, `unverified`, or `invalid`;
- an `unverified` correction may be retained as submitted audit material, but renderers, status aggregation, approval gates, traceability joins, and other consumers must **not apply it as the authoritative replacement**;
- an `invalid` correction remains visible for audit/investigation where policy permits but must never change derived truth;
- signing or hashing a later manifest that happens to contain a forged correction proves only that those bytes were bound; it does **not** authenticate the claimed actor unless the correction itself is signed/authenticated or an independently trusted append service authenticated and recorded the actor/authorization at submission time;
- a verified correction records exactly what it supersedes and does not erase the prior record; reports can reconstruct the full chain and show verification state at each transition;
- corrections that alter canonical execution evidence produce a **new evidence-manifest revision** rather than rewriting the revision already reviewed or approved;
- corrections to detached approvals/audit records remain detached from the evidence revision they discuss and require their own authenticated/integrity-protected audit binding rather than rewriting the approved evidence manifest;
- a correction cannot grant itself authority by claiming a stronger role; authorization is evaluated against trusted identity/policy state external to the submitted correction payload.

This keeps append-oriented audit history useful without turning `actor: someone@example` into a security boundary.

## Approvals

The schema should support optional human/organizational approvals from v1 even if Argus does not require them by default.

Approvals are **detached audit records over an immutable evidence-manifest revision**. They are not included in the manifest digest they approve, which avoids a self-referential digest cycle. Detached does **not** mean unauthenticated: an `actor` string alone is never sufficient evidence that the named actor actually approved the revision.

Example logical record:

```yaml
approval_id: APPROVAL-0002
role: test_reviewer
actor: reviewer@example.invalid
decision: approved
manifest_revision: 1
manifest_digest: "..."
approved_at: "..."
verification:
  status: verified
  method: signature
  signer_key_id: "qa-reviewer-key-7"
  binding_ref: "..."
```

Required semantics:

- the referenced evidence-manifest revision must already be finalized and immutable;
- appending an approval does not mutate that revision or its digest;
- every approval record carries a verification state such as `verified`, `unverified`, or `invalid`;
- a record is treated as a **valid approval** only after the actor/decision/manifest reference are authenticated and the detached record is integrity-protected by an independently trusted mechanism;
- acceptable verification mechanisms may include a cryptographic signature from a trusted reviewer identity, authenticated append to an independently integrity-protected audit/transparency service, or incorporation into a later independently bound audit/manifest revision where the approving actor was authenticated at append time;
- until such verification/binding succeeds, a syntactically well-formed record remains `unverified` and must not satisfy an approval gate;
- a forged or modified record whose verification fails is `invalid` and must never be presented as an approval;
- the detached approval record may be incorporated into a **later** audit/package-manifest revision without mutating the evidence revision it approved;
- if execution evidence changes, a new evidence-manifest revision is produced and prior approvals remain attached only to the revision they actually reviewed;
- reports must show which evidence-manifest revision each approval applies to **and its verification status/method**;
- regulated/audit profiles that require approval must require `verified` approvals and retain the verification evidence/reference needed to validate them later.

A mutable local `approvals.jsonl` file by itself is therefore only a storage format, not an approval trust boundary. Reports may display unverified records for investigation, but must label them clearly as unverified and must not summarize them as “approved.”

Approval support is intended for auditable workflows. Authentication, electronic-signature requirements, and legal validity depend on the deployment and applicable regulation and must not be implied by the presence of this record alone.

## Reports are renderings, not the truth store

ATES should define standard renderers for:

- Test Execution Report;
- Failure Report;
- Evidence Package;
- Traceability Matrix;
- Audit Report;
- Fleet Execution Report.

The report generator must derive claims from evidence references rather than create unsupported factual statements. Renderers must consume the canonical final-status aggregation rules above rather than independently deciding whether the same run passed or failed.

Interrupted/incomplete runs must remain renderable. Such reports should identify the last canonical event/sequence, missing completion metadata, reconciliation state, and any available crash/failure evidence.

### Renderer output safety

All text/URLs/metadata originating from the application under test, test specification, model output, logs, artifacts, Findings, or imported evidence must be treated as **untrusted data**, not markup or executable report content.

Every renderer must encode/escape values for its destination context at the final output boundary. Required semantics include:

- HTML renderers use context-appropriate HTML/attribute/URL encoding and do not insert evidence through raw/unsafe HTML APIs;
- active HTML such as `<script>`, event-handler attributes, inline executable content, `javascript:`/other unsafe URL schemes, and attacker-supplied embeds are prohibited by default rather than trusted because they came from canonical evidence;
- Markdown renderers escape or structurally isolate untrusted values so embedded HTML, links, images, directives, or renderer-specific extensions cannot silently become active content; if the target Markdown engine permits raw HTML, generated reports must disable/sanitize it or render evidence as escaped text/code;
- XML/JUnit renderers use XML-safe serializers, escape text/attribute values, reject or normalize characters illegal in the target XML version, and never concatenate untrusted strings into markup;
- JSON or other structured renderers use format-native serializers rather than string concatenation;
- URLs displayed as links are scheme-validated and policy-filtered; unsafe schemes are rendered as inert text;
- report templates, syntax highlighting, or rich-text helpers must not introduce a bypass around the same output-context rules.

Where HTML reports are served by the Control Center, defense in depth should include a restrictive Content Security Policy and isolation from privileged application origins/cookies where practical. Downloaded artifacts that may contain active content should not automatically execute in the report origin merely because they are referenced by evidence.

Renderer safety is independent from evidence integrity: a cryptographically verified malicious string is still malicious input to a renderer.

### Rendered-report trust boundary

A verified evidence manifest proves the integrity of the evidence snapshot it binds; it does **not** by itself prove that an arbitrary `report.html`, `report.md`, PDF, or JUnit file next to that evidence was produced faithfully or was not replaced later.

A rendered report must therefore carry an explicit trust state. At minimum:

- **regenerated_verified** — the consumer/verifier regenerated the report from a successfully verified evidence-manifest revision using an identified renderer/version/configuration;
- **bound_verified** — the exact rendered report bytes are hashed and bound by a separately verified outer/package-manifest revision that also references the source evidence-manifest digest and renderer identity;
- **unverified_derived** — the report exists as a convenience rendering but has not been regenerated from verified evidence or independently bound;
- **invalid** — a required digest/binding/renderer verification failed.

For portable/exported evidence packages, the preferred model is:

```text
verified evidence manifest
        |
        +--> render report(s)
        |
        v
package manifest
  - source evidence-manifest digest
  - renderer/version/config digest
  - report.html SHA-256
  - report.md SHA-256
  - junit.xml SHA-256
        |
        v
signature / trusted external digest / immutable binding (when required)
```

Required semantics:

- a UI/report viewer must not show `unverified_derived` output with the same trust indicator as verified evidence;
- if no outer/package binding exists, a security-sensitive consumer should regenerate the report from verified canonical evidence before treating its PASS/FAIL/approval claims as trusted;
- when a package manifest binds rendered outputs, any byte change to a report invalidates that report's binding even if the underlying evidence manifest still verifies;
- package manifests are versioned/immutable and must reference the exact source evidence-manifest revision/digest;
- approvals over evidence remain approvals of the referenced evidence-manifest revision unless an approval explicitly and verifiably covers a package/report revision too;
- renderer defects are not magically eliminated by hashing: binding proves which bytes were distributed, while regeneration/cross-checking against canonical evidence provides semantic validation.

This prevents a forged PASS report from riding alongside an otherwise valid evidence package without a visible trust failure.

## Suggested standard report structure

1. Execution identification and kind
2. Executive summary and canonical final-status basis
3. Execution objective / test objective where applicable
4. Versioned requirements/traceability scope where applicable
5. Test environment
6. Execution configuration
7. Execution intent, logical Steps and complete per-attempt retry history
8. Assertions
9. Checkpoints and evidence
10. Findings/anomalies
11. Failures/errors/ambiguous outcomes
12. Collected artifacts
13. Execution timeline
14. Model/resource usage
15. Failure Capsule information, if retained
16. Approvals/review state, verification status, and evidence-manifest revision, if applicable
17. Evidence manifest and integrity/tamper-evidence status
18. Rendered-report trust status and package-manifest reference, if applicable

## Privacy and secret handling

ATES evidence must not become a new secret-exfiltration path.

The runtime must define explicit redaction/retention rules for:

- API keys;
- bearer tokens;
- passwords;
- TLS private keys;
- session bootstrap secrets;
- authored Step instructions/execution goals;
- typed user input;
- command arguments;
- credential-bearing URLs;
- sensitive application/Observation data;
- model/human-generated Interpretations and Findings;
- error/exception/operator-note free-form text;
- screenshots and recordings;
- binary/log artifacts that can contain application secrets;
- source/configuration/requirement material used to build provenance commitments.

Any canonical free-form or target-generated value field—including Step, Action, Assertion, Observation, Interpretation, Finding, error/log excerpt, and future human/model annotation fields—must support schema-level sensitivity/redaction metadata where applicable rather than relying solely on a best-effort regex pass after plaintext has already been written to canonical evidence.

Observation capture specifically follows a conservative pre-append allow/redact/protect/suppress decision; unknown high-risk target-generated values are not persisted verbatim by default.

Run-level source/configuration and requirement commitments must follow the secret-safe commitment model above. A plain public digest of raw material is not an acceptable substitute for redaction when low-entropy secrets may be present.

Binary Artifact capture must use the pre-write classification/redaction/protected/suppressed model defined above. Persisting raw screenshots to an ordinary artifact directory and redacting them afterward is not a conforming default capture pipeline.

Capsule secrets that are intentionally non-persistent today must remain non-persistent under ATES.

## Fleet integration

ATES is also the planned protocol-level foundation for Argus Fleet observation.

A Node can stream ATES events to the Control Center while retaining canonical local evidence. The read-only Observer UI can then display live progress from the same event vocabulary later used to render final reports.

Fleet transport must preserve the event-envelope semantics above: retries retain identity, out-of-order delivery does not change canonical order, and gaps/conflicts/tombstones are reconciled explicitly rather than hidden. Fleet also consumes the same canonical final-status aggregation and Step-attempt identities rather than deriving incompatible outcomes centrally.

That avoids creating one schema for execution, another for dashboards, and a third for documentation.

## Native standard and external compliance

ATES is an **Argus-native specification** and should remain usable without purchasing external standards.

Future compatibility modules may map ATES records to standards such as the ISO/IEC/IEEE 29119 software-testing family or domain-specific regulatory frameworks. Those mappings must:

- be optional;
- avoid redistributing copyrighted normative text without permission;
- distinguish "mapped" from "certified" or "compliant";
- document gaps that require organizational process rather than software behavior;
- use legitimately obtained standards material when detailed normative mappings are implemented.

Argus should never claim compliance merely because ATES contains similarly named records.

## Versioning

ATES records should carry an explicit schema version, for example:

```yaml
ates_version: "0.1"
```

Schema evolution should favor additive changes. Breaking changes require a new major evidence-schema version and migration/rendering rules for retained historical runs. Final-status aggregation rules, retry/attempt semantics, and requirement-identity canonicalization are versioned parts of the evidence contract; changing their meaning requires explicit schema/policy versioning rather than silent reinterpretation of historical runs.

## Initial implementation sequence

Recommended implementation order:

1. define typed ATES Core IDs, lifecycle states, canonical final-status values/precedence, `execution_kind`/conditional secret-safe source identity, immutable Step-attempt IDs/ordinals, event envelope, schema-wide redacted-value model (including secret-safe Observation/Interpretation/Finding/free-form text), immutable requirement identity/commitment, artifact sensitivity/path-confinement model, and schema version;
2. add append-only local event writer with atomic gap-free sequence assignment and duplicate/conflict invariants;
3. emit run/logical-step/step-attempt/action/observation/assertion/interpretation/finding lifecycle events from both scripted and roam execution paths, including pre-write redaction, durable pre-dispatch `ACTION_DISPATCH_COMMITTED` operation identities, ambiguous-action recovery semantics, retry reasons, and interrupted-run recovery;
4. implement canonical run/Step status aggregation from immutable event/attempt history and ensure all renderers/Fleet consume it rather than recomputing incompatible outcomes;
5. implement adapter/provider operation-id deduplication/status reconciliation where supported and fail safely for unknown non-idempotent action outcomes where it is not;
6. implement Observation pre-append allow/redact/protect/suppress handling and artifact pre-write classification/redaction/protected/suppressed handling plus race-safe evidence-root path confinement **before** enabling default failure/checkpoint evidence persistence;
7. implement failure evidence and explicit checkpoints using those evidence policies;
8. implement the crash-safe completion/finalization transaction: close retained artifact producers, hash/validate required artifacts and canonical inputs, derive the terminal outcome, prepare the exact `RUN_COMPLETED` bytes plus final immutable evidence manifest, and durably commit them as one logical transaction before exposing `completed`/`passed`;
9. implement optional evidence-manifest binding mechanisms before advertising tamper-evident evidence, and make any profile-required binding part of the finalization gate;
10. render the first Markdown/JSON Test Execution Report from verified/canonical evidence using context-safe encoding, canonical final-status rules, complete retry-attempt history, incomplete-run handling, and execution-kind-aware source identity, and mark it `unverified_derived` unless regenerated/verified or separately bound;
11. implement versioned package manifests (or verified regeneration flow) for rendered report outputs before distributing them with a verified trust indicator;
12. add immutable/versioned requirement traceability joins and cross-run coverage for applicable execution kinds;
13. add authenticated detached approval/supersession records tied to immutable evidence-manifest revisions, with actor authorization and verification status enforced by renderers/gates;
14. integrate live event streaming with Argus Fleet using idempotent retry/reconciliation semantics;
15. add external compliance mappings only after the native model is stable.