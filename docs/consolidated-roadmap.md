# Consolidated Argus roadmap after ATES artifact capture

Status: implementation roadmap after merged PR #21.

The earlier roadmap split the remaining ATES and Fleet work across PRs #22–#34. That sequencing was useful while the evidence model, durable store, runtime lifecycle, action reconciliation, privacy boundary, and artifact boundary were still being established independently. Those foundations are now merged.

From this point, the remaining work is intentionally consolidated into three integration PRs. Each PR is larger, but is divided into ordered commit groups with explicit internal contracts and end-to-end acceptance tests. The goal is to reduce branch/rebase/review-round overhead without collapsing unrelated trust boundaries into one unreviewable change.

## PR #22 — Complete ATES v0.1

Working title: `feat: complete ATES v0.1 finalization, reports, and audit`

This replaces the old PRs #22, #23, and #24.

### Commit group A — transactional finalization

- derive the canonical terminal status from ATES evidence rather than trusting legacy producer status;
- close mutable artifact producers before finalization;
- reconcile step attempts, required assertions, unresolved action outcomes, event integrity, environment/runtime failures, and required artifact state;
- create immutable final evidence/artifact manifests over exact persisted bytes;
- publish `RUN_COMPLETED` only as part of a durable logical finalization transaction;
- prevent a visible `passed` result if manifest/event publication is ambiguous or incomplete;
- preserve incomplete/recoverable semantics when storage cannot safely finalize;
- establish revision 1 `RunOutcomeRevision` and keep the existing authenticated re-finalization model for later corrections.

### Commit group B — manifest verification and trust state

- add deterministic manifest verification APIs;
- bind evidence stream, retained artifacts, final sequence/event identity, status-policy version, and finalization identity;
- distinguish `regenerated_verified`, `bound_verified`, `unverified_derived`, and `invalid` consumer states;
- never describe local hashes stored beside mutable files as external tamper evidence.

### Commit group C — safe report renderers

- generate Markdown and JSON reports first, then HTML and JUnit;
- render only from validated ATES/finalization state, not directly from live runtime objects;
- context-escape all untrusted target/test/model/log/finding/imported text;
- sanitize HTML and validate URL schemes;
- use an XML serializer and legal-character filtering for JUnit;
- never auto-execute active retained artifacts from the report origin;
- display trust state and incomplete/invalid evidence prominently.

### Commit group D — traceability, approvals, and audit

- materialize exact immutable requirement identities through test → run → step → attempt → assertion → observation/artifact links;
- keep changed requirement revisions distinct even when display IDs are reused;
- add detached authenticated approval records that are not included in the digest they approve;
- support approval supersession/revocation without rewriting historical evidence;
- record audit events for finalization/re-finalization/approval changes.

### PR #22 acceptance gate

A successful scripted run must be able to produce a self-consistent package:

```text
.argus/runs/<run-id>/
├── evidence.jsonl
├── run.json
├── manifests/
│   ├── manifest-0001.json
│   └── package-manifest-0001.json
├── approvals.jsonl
├── artifacts/
└── reports/
    ├── report.json
    ├── report.md
    ├── report.html
    └── junit.xml
```

The run may be called `passed` only when the canonical status policy says passed and the required finalization transaction is durable. Roam runs use Findings and runtime-derived phases without fabricating scripted coverage.

## PR #23 — Fleet execution plane

Working title: `feat: add secure Argus Fleet execution plane`

This replaces the old PRs #25–#29.

### Commit group A — Node identity and enrollment

- Control Center registry and durable Node identity;
- Node keypair/proof-of-possession enrollment;
- short-lived single-use bootstrap credentials supplied outside argv;
- lost-response idempotency with stable enrollment request identity;
- authenticated administrative recovery/revocation;
- mTLS or an equivalent authenticated encrypted production channel.

### Commit group B — heartbeats and capabilities

- Node health, provider/image/capacity advertisement;
- immutable Capsule image identities instead of mutable aliases as execution identity;
- trusted Control Center receipt time plus Node clock offset/uncertainty assessment;
- explicit degraded/disconnected reconciliation state.

### Commit group C — durable placement transaction

- stable logical session request IDs and request digests;
- globally authoritative placement ownership/generation/fencing;
- durable cancellation state and terminal allocation tombstones;
- Node-side atomic admission/capacity reservation;
- idempotent ambiguous dispatch recovery;
- no implicit fallback to Control Center-local execution.

### Commit group D — remote Capsule execution and ATES streaming

- Node Agent translates authorized Fleet sessions into the existing `ExecutionEnvironment` / Capsule APIs;
- immutable staged-input identities and image identity are checked before start;
- stream canonical ATES records without rewriting producer facts;
- securely transfer allowed artifacts while preserving protected-artifact policy;
- reconcile running sessions after Control Center/Node reconnects.

### PR #23 acceptance gate

One Control Center must be able to enroll at least two Nodes, place a Capsule-backed run on a matching Node, survive a lost allocation response without duplicate execution, stream ATES evidence centrally, and reconcile the final session state after reconnect.

## PR #24 — Fleet operations, Observer, and matrix execution

Working title: `feat: add Argus Fleet operations and read-only Observer`

This replaces the old PRs #30–#34.

### Commit group A — structurally read-only Observer

- separate Observer authorization surface from privileged Control Center APIs;
- GET-only fleet/node/session/evidence/log/artifact metadata operations;
- no mouse, keyboard, command execution, cancellation, restart, configuration mutation, clipboard, or interactive console authority;
- show the complete guest screen only, never the host desktop.

### Commit group B — live view

- begin with bandwidth-aware low-FPS one-way snapshots;
- correlate frames with ATES/session timing metadata;
- keep the transport authenticated/encrypted;
- leave WebRTC as a later optimization unless needed for the v0.1 acceptance gate.

### Commit group C — scheduler and queue

- capability/label/image/capacity-aware queue;
- backpressure and placement retry/reconciliation;
- explicit unsatisfied-requirement state rather than silent local fallback;
- cancellation and generation fencing remain delegated to the #23 execution-plane contract.

### Commit group D — matrix, timeline, and indexes

- expand test plans across OS/image/application matrices;
- aggregate result/finding/artifact indexes;
- render cross-Node timelines without inventing precise global ordering when clock uncertainty overlaps;
- preserve per-run ATES sequence as exact ordering authority.

### PR #24 acceptance gate

The UI/API must be able to show multiple queued/running/completed sessions across Nodes, schedule a matrix, display read-only guest views and ATES timelines, and prove that Observer credentials cannot mutate execution.

## Why three PRs instead of two

Two PRs would force either all ATES plus all Fleet execution into one change, or all Fleet execution plus scheduling/Observer/UI into one change. Both combinations mix security-critical control-plane mutation with high-volume operational/UI code and create an unnecessarily large review blast radius.

Three PRs preserve the three natural trust boundaries:

1. **truth and evidence authority** — ATES;
2. **remote mutation/execution authority** — Fleet execution plane;
3. **operations and read-only consumption** — Fleet operations/Observer.

That is the smallest PR count that still keeps a meaningful security boundary between changes.
