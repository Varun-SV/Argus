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
- provenance for test definitions, Argus builds, models, execution environments, nodes, Capsules, and artifacts;
- tamper-evident artifact manifests;
- append-oriented audit history rather than silent mutation;
- requirement-to-test-to-evidence traceability;
- a stable machine-readable foundation for reports, dashboards, audit packages, and future compliance mappings.

## Non-goals

ATES does not:

- record or expose private model chain-of-thought;
- make an LLM interpretation equivalent to an observed fact;
- require ISO/IEC material for native Argus operation;
- claim compliance with a regulated standard merely because a report resembles one;
- make screenshots mandatory for every normal action;
- allow report configuration to suppress mandatory core evidence.

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

Every execution has one immutable logical run identity.

Required fields should include at least:

- `run_id`;
- test case identity and digest;
- Argus version/build/commit where available;
- start and end timestamps;
- final status;
- execution environment type;
- adapter type;
- host/node identity where applicable;
- Capsule/provider identity where applicable;
- guest/host operating-system information where applicable;
- model/provider identity;
- effective evidence profile;
- configuration fingerprint;
- evidence-manifest reference.

### Step

A step represents test intent.

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

An Action records what Argus actually attempted to execute.

```yaml
action_id: ACTION-0041
step_id: STEP-0002
type: click
target:
  name: "Network & Internet"
  control_type: ListItem
status: succeeded
```

The evidence record should capture the normalized action after policy validation, not only the model's raw proposal.

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

Observations are evidence. They are not AI conclusions.

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

The preferred runtime representation is an append-oriented event stream plus a final manifest.

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
```

Not every adapter/environment will emit every event.

Events should contain stable identifiers and references rather than duplicating large payloads.

## Storage layout

A possible on-disk layout is:

```text
.argus/runs/<run-id>/
  evidence.jsonl
  run.json
  manifest.json
  artifacts/
  reports/
    report.md
    report.html
    junit.xml
```

`evidence.jsonl` is the canonical event history. Reports are derived views and should be reproducible from canonical evidence where practical.

## Integrity model

### Artifact hashing

Retained artifacts should be hashed with SHA-256 and included in the run manifest.

### Manifest

The manifest should bind:

- canonical evidence files;
- retained artifacts;
- report inputs;
- relevant schema/version identifiers.

### Future signing

ATES should leave room for cryptographic signing of manifests without requiring signatures for ordinary users.

Possible future chain:

```text
artifact hashes
    -> evidence manifest
        -> manifest digest
            -> optional organizational signature
```

## Audit history

Corrections should be append-oriented.

A prior Finding, classification, approval, or interpretation should not silently disappear. A later record should supersede or annotate it with:

- prior record ID;
- new record ID;
- reason;
- actor;
- timestamp.

## Approvals

The schema should support optional human/organizational approvals from v1 even if Argus does not require them by default.

Example logical record:

```yaml
approval_id: APPROVAL-0002
role: test_reviewer
actor: reviewer@example.invalid
decision: approved
manifest_digest: "..."
approved_at: "..."
```

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
16. Approvals/review state, if applicable
17. Evidence manifest

## Privacy and secret handling

ATES evidence must not become a new secret-exfiltration path.

The runtime should define explicit redaction/retention rules for:

- API keys;
- bearer tokens;
- passwords;
- TLS private keys;
- session bootstrap secrets;
- sensitive application data.

Capsule secrets that are intentionally non-persistent today must remain non-persistent under ATES.

## Fleet integration

ATES is also the planned protocol-level foundation for Argus Fleet observation.

A Node can stream ATES events to the Control Center while retaining canonical local evidence. The read-only Observer UI can then display live progress from the same event vocabulary later used to render final reports.

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

1. define typed ATES Core IDs, events, and schema version;
2. add append-only local event writer;
3. emit run/step/action/observation/assertion lifecycle events from the existing runner;
4. implement failure evidence and explicit checkpoints;
5. hash artifacts and generate the run manifest;
6. render the first Markdown/JSON Test Execution Report from canonical evidence;
7. add requirement traceability;
8. add optional approval/supersession records;
9. integrate live event streaming with Argus Fleet;
10. add external compliance mappings only after the native model is stable.
