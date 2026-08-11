# ATES — Argus Test Evidence Specification

> **Status:** design specification. ATES is not yet implemented by the Argus runtime.

ATES defines the canonical evidence contract for Argus test execution. It is intentionally separate from Markdown, HTML, PDF, JUnit, dashboards, or any other presentation format.

The central rule is:

> **Execution produces evidence. Monitoring visualizes evidence. Documentation renders evidence.**

Every Argus run should eventually produce an ATES Core record automatically, even when the user does not explicitly request a report.

## Goals

ATES is designed to provide:

- consistent documentation across all Argus runs;
- step-by-step traceability from test intent to observed evidence;
- clear separation between observed facts, deterministic assertions, executed actions, and AI interpretation;
- failure and checkpoint evidence without capturing screenshots for every action by default;
- provenance for test definitions, Argus builds, models, execution environments, Nodes, Capsules, and artifacts;
- cryptographic artifact digests for corruption/integrity detection;
- a manifest model that becomes tamper-evident only when bound to a trusted external, immutable, or cryptographic trust boundary;
- append-oriented audit history rather than silent mutation;
- requirement-to-test-to-evidence traceability;
- recoverable evidence from interrupted/crashed runs;
- a stable machine-readable foundation for reports, dashboards, audit packages, and future compliance mappings.

## Non-goals

ATES does not:

- record or expose private model chain-of-thought;
- make an LLM interpretation equivalent to an observed fact;
- require ISO/IEC material for native Argus operation;
- claim compliance with a regulated standard merely because a report resembles one;
- make screenshots mandatory for every normal action;
- allow report configuration to suppress mandatory core evidence;
- claim that a locally stored hash and locally stored manifest are tamper-evident without an independent trust boundary;
- require an interrupted run to fabricate an end timestamp or successful finalization event.

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

**Default screenshot policy:** screenshots are captured for failures and explicit checkpoints, not for every successful action.

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

Secrets, live bearer tokens, TLS private keys, and other credentials must never be included merely because forensic capture is enabled.

### `custom`

A custom profile may add evidence but cannot remove mandatory ATES Core records.

## Canonical data model

The exact serialization may evolve, but the logical model should remain stable.

### Run

Every execution has one immutable logical `run_id`.

Fields required from run creation include at least:

- `run_id`;
- test case identity and digest;
- Argus version/build/commit where available;
- `started_at`;
- execution environment type;
- adapter type;
- host/Node identity where applicable;
- Capsule/provider identity where applicable;
- guest/host operating-system information where applicable;
- model/provider identity;
- effective evidence profile;
- configuration fingerprint.

Completion fields are **not required until successful finalization**. These include:

- `ended_at`;
- `final_status`;
- evidence-manifest revision/reference;
- final manifest digest/binding status.

A run whose stream has no valid `RUN_COMPLETED` event remains a valid **incomplete/recoverable run**, not an invalid ATES record. Renderers and validators must preserve its available evidence and report that final status/end time are unknown rather than inventing them.

A derived `run.json` may expose lifecycle state such as:

```yaml
run_id: RUN-01K...
started_at: "2026-08-11T01:20:00+05:30"
lifecycle_state: incomplete
ended_at: null
final_status: null
last_sequence: 84
```

If Argus later performs recovery/reconciliation, it may append an explicit recovery/terminal event such as `RUN_MARKED_INCOMPLETE`, including the actor/recovery process and reason. Recovery must never synthesize a normal `RUN_COMPLETED` event for execution that was not observed to complete.

### Step

A Step represents test intent.

```yaml
step_id: STEP-0002
sequence: 2
instruction:
  type: natural_language
  text: "Navigate to Network"
status: passed
```

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

```yaml
interpretation_id: INT-0012
source: llm
model: example-model
summary: "The application appears unable to establish the requested connection."
evidence:
  - OBS-0088
```

ATES must preserve the distinction between:

- **fact** — directly observed or measured;
- **action** — actually executed;
- **assertion** — deterministic evaluation;
- **interpretation** — inferred by a model or human.

### Finding

A Finding describes a possible defect, anomaly, or noteworthy behavior.

Findings should reference the evidence supporting them and state whether their severity/classification is deterministic, rule-based, human-assigned, or model-inferred.

### Checkpoint

A checkpoint is an explicit documentation/evidence request embedded in the test specification.

Example future syntax:

```yaml
steps:
  - "Open Network settings"
  - checkpoint:
      name: network-settings-open
  - assert:
      text_visible: "Ethernet"
```

A standard checkpoint should capture, where supported:

- screenshot;
- accessibility/DOM/CLI observation appropriate to the adapter;
- timestamp;
- current step/action relationship;
- environment identity;
- integrity metadata for generated artifacts.

Checkpoint content remains subject to privacy/redaction policy.

### Artifact

Every retained file is represented by an Artifact record.

```yaml
artifact_id: ART-01892
kind: screenshot
path: artifacts/STEP-017-checkpoint.png
sha256: "..."
size_bytes: 381223
source: capsule
```

Artifacts collected from Capsules must continue to obey the explicit staging/collection authority already enforced by the Capsule execution boundary.

### Requirement

ATES should support first-class requirement traceability.

A future test may declare:

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
ACTION_EXECUTED
ASSERTION_EVALUATED
CHECKPOINT_CAPTURED
FINDING_RECORDED
ARTIFACT_COLLECTED
TARGET_CLOSED
FAILURE_CAPSULE_RETAINED
ENVIRONMENT_RELEASED
RUN_COMPLETED
RUN_MARKED_INCOMPLETE
```

Not every adapter/environment will emit every event.

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
  approvals.jsonl
  artifacts/
  reports/
    report.md
    report.html
    junit.xml
```

`evidence.jsonl` is the canonical execution-event history. Reports are derived views and should be reproducible from canonical evidence where practical. `approvals.jsonl` is a detached audit ledger described below and does not mutate the immutable manifest revision it approves.

## Integrity model

### Artifact hashing

Retained artifacts should be hashed with SHA-256 and included in the applicable run manifest revision.

A digest stored alongside mutable evidence detects accidental corruption and modification **only while the manifest/digest itself remains trusted**. A process that can replace both an artifact and its stored digest can recompute both, so hashes alone must not be described as tamper-evident.

### Versioned immutable manifests

Each finalized evidence snapshot is represented by an immutable manifest revision, for example `manifest-0001.json`.

A manifest revision should bind:

- the canonical execution evidence snapshot/range it covers;
- retained artifacts included in that revision;
- report inputs where applicable;
- relevant schema/version identifiers;
- previous manifest revision/digest when a chained revision is created.

Once published/finalized, a manifest revision and its digest are immutable. Additional audit material never rewrites the revision it references.

### Tamper-evidence boundary

ATES may claim that an evidence package/revision is **tamper-evident** only when its manifest digest is bound outside the mutable evidence set by at least one approved mechanism, for example:

- a cryptographic signature whose verification key is independently trusted;
- a trusted external transparency/audit service that records the manifest digest;
- immutable/WORM storage or another storage boundary that prevents the evidence producer from rewriting both evidence and its recorded digest after finalization.

A native run without such a binding remains useful for integrity/corruption detection, but must not be labeled tamper-evident.

### Signing and audit profiles

ATES should support cryptographic signing of manifest revisions without requiring signatures for ordinary users.

Possible chain:

```text
artifact hashes
    -> immutable manifest revision
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

Corrections that alter execution evidence produce a **new manifest revision** rather than rewriting the revision already reviewed or approved.

## Approvals

The schema should support optional human/organizational approvals from v1 even if Argus does not require them by default.

Approvals are **detached audit records over an immutable manifest revision**. They are not included in the manifest digest they approve, which avoids a self-referential digest cycle.

Example logical record:

```yaml
approval_id: APPROVAL-0002
role: test_reviewer
actor: reviewer@example.invalid
decision: approved
manifest_revision: 1
manifest_digest: "..."
approved_at: "..."
```

Required semantics:

- the referenced manifest revision must already be finalized and immutable;
- appending an approval does not mutate that revision or its digest;
- the detached approval record may itself be signed or incorporated into a **later** audit/manifest revision if required;
- if execution evidence changes, a new manifest revision is produced and prior approvals remain attached only to the revision they actually reviewed;
- reports must show which manifest revision each approval applies to.

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

## Suggested standard report structure

1. Test identification
2. Executive summary
3. Test objective
4. Requirements/traceability scope
5. Test environment
6. Execution configuration
7. Test case and steps
8. Assertions
9. Checkpoints and evidence
10. Findings/anomalies
11. Failures
12. Collected artifacts
13. Execution timeline
14. Model/resource usage
15. Failure Capsule information, if retained
16. Approvals/review state and manifest revision, if applicable
17. Evidence manifest and integrity/tamper-evidence status

## Privacy and secret handling

ATES evidence must not become a new secret-exfiltration path.

The runtime must define explicit redaction/retention rules for:

- API keys;
- bearer tokens;
- passwords;
- TLS private keys;
- session bootstrap secrets;
- typed user input;
- command arguments;
- credential-bearing URLs;
- sensitive application data.

The Action/Assertion/Observation schemas must carry redaction metadata rather than relying solely on a best-effort regex pass after plaintext has already been written to canonical evidence.

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

1. define typed ATES Core IDs, lifecycle states, event envelope, redacted value model, and schema version;
2. add append-only local event writer with atomic gap-free sequence assignment and duplicate/conflict invariants;
3. emit run/step/action/observation/assertion lifecycle events from the existing runner, including interrupted-run recovery semantics;
4. implement failure evidence and explicit checkpoints;
5. hash artifacts and generate immutable versioned manifests;
6. implement optional manifest binding mechanisms before advertising tamper-evident evidence;
7. render the first Markdown/JSON Test Execution Report from canonical evidence, including incomplete runs;
8. add requirement traceability;
9. add detached approval/supersession records tied to manifest revisions;
10. integrate live event streaming with Argus Fleet using idempotent retry/reconciliation semantics;
11. add external compliance mappings only after the native model is stable.
