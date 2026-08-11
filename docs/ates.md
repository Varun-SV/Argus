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
- requirement-to-test-to-evidence traceability where a run is requirement-driven;
- recoverable evidence from interrupted/crashed runs, including ambiguous action dispatch outcomes;
- schema-level pre-persistence redaction for all free-form evidence text, including model/human interpretations and findings;
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
- require every execution mode to invent a pre-authored scripted test-case identity.

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

The evidence-manifest revision/digest that binds the finalized stream is **derived after the canonical execution bytes are closed** and is not embedded back into the same bound Run/`RUN_COMPLETED` bytes. Doing so would create a self-referential digest cycle.

A run whose stream has no valid `RUN_COMPLETED` event remains a valid **incomplete/recoverable run**, not an invalid ATES record. Renderers and validators must preserve its available evidence and report that final status/end time are unknown rather than inventing them.

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

### Step

A Step represents execution intent at a point in the run. In scripted execution it normally corresponds to authored test intent; in roam execution it may represent an exploratory goal/subgoal emitted by the roam engine without implying a pre-authored test case.

A non-sensitive example is:

```yaml
step_id: STEP-0002
sequence: 2
instruction:
  type: natural_language
  text: "Navigate to Network"
status: passed
```

Step instructions are subject to the **same schema-level secret handling as Actions and Assertions**. ATES Core must not persist an authored instruction such as `Type password123 into the password field` verbatim merely because the later Action payload is redacted.

A sensitive Step can instead be represented as:

```yaml
step_id: STEP-0003
sequence: 3
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
status: passed
```

Where possible, authored tests should use explicit secret references/placeholders rather than embedding literal credentials in instruction text. If a literal is present, the canonical Step representation must be transformed/redacted **before** it is appended to always-on evidence. Low-entropy secret text must not be replaced with a public unsalted digest.

A step may contain multiple actions and observations.

### Action

An Action records what Argus actually attempted to execute, but ATES Core must not blindly persist secret-bearing action payloads.

A safe example is:

```yaml
action_id: ACTION-0041
step_id: STEP-0002
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

An Observation records data directly measured or captured from the execution environment.

```yaml
observation_id: OBS-0042
source: accessibility_tree
captured_at: 2026-08-11T01:21:43.819+05:30
facts:
  visible_text:
    - "Network & Internet"
    - "Wi-Fi"
    - "Ethernet"
```

Observations are evidence. They are not AI conclusions. Observation capture is also subject to privacy/redaction policy when an application surface itself contains secrets.

### Assertion

Assertions are recorded independently from AI interpretation.

```yaml
assertion_id: ASSERT-0008
step_id: STEP-0003
kind: text_visible
expected: "Ethernet"
result: passed
method: deterministic
observation_id: OBS-0043
```

Sensitive assertion inputs/expected values must use the same redaction model when retaining plaintext would expose secrets.

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

A standard checkpoint should request, where supported and permitted by artifact policy:

- screenshot or a documented suppressed/redacted substitute;
- accessibility/DOM/CLI observation appropriate to the adapter;
- timestamp;
- current step/action relationship;
- environment identity;
- integrity metadata for generated artifacts.

Checkpoint content remains subject to privacy/redaction policy. A checkpoint never overrides a rule that forbids persistence of sensitive binary evidence.

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

ATES should support first-class requirement traceability for executions that declare requirements.

A future scripted test may declare:

```yaml
requirements:
  - id: REQ-NET-042
    description: "The application shall display the active network connection."
```

An assertion may then record:

```yaml
verifies:
  - REQ-NET-042
```

This creates a traceability chain such as:

```text
REQ-NET-042
  -> TEST-NETWORK-003
  -> RUN-...
  -> ASSERT-0007
  -> OBS-0019
  -> ART-0004
```

A roam run without declared requirements does not fabricate this chain; it records exploratory Findings/evidence and may later be linked to requirements through an explicit authenticated/superseding record if a reviewer performs that mapping.

## Event stream

The preferred runtime representation is an append-oriented event stream plus versioned manifests.

Representative events:

```text
RUN_STARTED
ENVIRONMENT_PREPARED
CAPSULE_CREATED
TARGET_LAUNCHED
STEP_STARTED
OBSERVATION_CAPTURED
ACTION_PROPOSED
ACTION_POLICY_VALIDATED
ACTION_DISPATCH_COMMITTED
ACTION_EXECUTED
ACTION_OUTCOME_UNKNOWN
ASSERTION_EVALUATED
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

`RUN_COMPLETED` closes execution semantics (for example final status/end time/final sequence). It does **not** contain the digest of an evidence manifest whose input includes that same event. Evidence-manifest construction occurs after the canonical event range is closed.

For Actions that can cause external side effects, `ACTION_DISPATCH_COMMITTED` is the durable pre-side-effect boundary. Once it exists without a confirmed terminal action outcome, recovery treats the operation as potentially executed and must apply the ambiguous-outcome rules above before any retry.

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

Corrections should be append-oriented.

A prior Finding, classification, approval, or interpretation should not silently disappear. A later record should supersede or annotate it with:

- prior record ID;
- new record ID;
- reason;
- actor;
- timestamp.

Corrections that alter execution evidence produce a **new evidence-manifest revision** rather than rewriting the revision already reviewed or approved.

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

The report generator must derive claims from evidence references rather than create unsupported factual statements.

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
2. Executive summary
3. Execution objective / test objective where applicable
4. Requirements/traceability scope where applicable
5. Test environment
6. Execution configuration
7. Execution intent, scripted test case/steps or roam goals/subgoals
8. Assertions
9. Checkpoints and evidence
10. Findings/anomalies
11. Failures
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
- sensitive application data;
- model/human-generated Interpretations and Findings;
- error/exception/operator-note free-form text;
- screenshots and recordings;
- binary/log artifacts that can contain application secrets;
- source/configuration material used to build Run-level provenance commitments.

Any canonical free-form text field—including Step, Action, Assertion, Observation, Interpretation, Finding, error/log excerpt, and future human/model annotation fields—must support schema-level sensitivity/redaction metadata where applicable rather than relying solely on a best-effort regex pass after plaintext has already been written to canonical evidence.

Run-level source/configuration commitments must follow the secret-safe commitment model above. A plain public digest of raw material is not an acceptable substitute for redaction when low-entropy secrets may be present.

Binary Artifact capture must use the pre-write classification/redaction/protected/suppressed model defined above. Persisting raw screenshots to an ordinary artifact directory and redacting them afterward is not a conforming default capture pipeline.

Capsule secrets that are intentionally non-persistent today must remain non-persistent under ATES.

## Fleet integration

ATES is also the planned protocol-level foundation for Argus Fleet observation.

A Node can stream ATES events to the Control Center while retaining canonical local evidence. The read-only Observer UI can then display live progress from the same event vocabulary later used to render final reports.

Fleet transport must preserve the event-envelope semantics above: retries retain identity, out-of-order delivery does not change canonical order, and gaps/conflicts/tombstones are reconciled explicitly rather than hidden.

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

Schema evolution should favor additive changes. Breaking changes require a new major evidence-schema version and migration/rendering rules for retained historical runs.

## Initial implementation sequence

Recommended implementation order:

1. define typed ATES Core IDs, lifecycle states, `execution_kind`/conditional secret-safe source identity, event envelope, schema-wide redacted-value model (including Interpretation/Finding/free-form text), artifact sensitivity/path-confinement model, and schema version;
2. add append-only local event writer with atomic gap-free sequence assignment and duplicate/conflict invariants;
3. emit run/step/action/observation/assertion/interpretation/finding lifecycle events from both scripted and roam execution paths, including pre-write redaction, durable pre-dispatch `ACTION_DISPATCH_COMMITTED` operation identities, ambiguous-action recovery semantics, and interrupted-run recovery;
4. implement adapter/provider operation-id deduplication/status reconciliation where supported and fail safely for unknown non-idempotent action outcomes where it is not;
5. implement artifact pre-write classification/redaction/protected/suppressed handling and race-safe evidence-root path confinement **before** enabling default failure/checkpoint screenshot persistence;
6. implement failure evidence and explicit checkpoints using that artifact policy;
7. hash canonical artifacts and generate immutable versioned evidence manifests after the canonical event range is closed, keeping each manifest's own digest detached from the bytes it hashes;
8. implement optional evidence-manifest binding mechanisms before advertising tamper-evident evidence;
9. render the first Markdown/JSON Test Execution Report from verified/canonical evidence using context-safe encoding, including incomplete runs and execution-kind-aware source identity, and mark it `unverified_derived` unless regenerated/verified or separately bound;
10. implement versioned package manifests (or verified regeneration flow) for rendered report outputs before distributing them with a verified trust indicator;
11. add requirement traceability for applicable execution kinds;
12. add authenticated detached approval/supersession records tied to immutable evidence-manifest revisions, with verification status enforced by renderers/gates;
13. integrate live event streaming with Argus Fleet using idempotent retry/reconciliation semantics;
14. add external compliance mappings only after the native model is stable.
