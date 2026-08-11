# ATES durable event store

> **Status:** PR #17 implementation boundary. This layer persists canonical ATES events locally; it does not yet wire events into `argus run` / `argus roam`, generate manifests, or render reports.

PR #16 established the immutable ATES Core schema. PR #17 adds the first persistence primitive: one append-only `evidence.jsonl` stream per ATES `run_id`.

## Layout

```text
.argus/runs/<run-id>/
  .ates-writer.lock
  evidence.jsonl
```

Each line of `evidence.jsonl` is one canonical UTF-8 JSON object containing the ATES event envelope plus an object-valued `payload`.

## Canonical append contract

The local authoritative writer enforces:

- exactly one writer authority for a run at a time, with an in-process lock plus a cross-process lock;
- stable typed `run_id` / `event_id` identities;
- consecutive, gap-free sequence assignment beginning at `1`;
- canonical JSON serialization using sorted keys, compact separators, UTF-8, and no non-finite numeric values;
- one newline-terminated event per record;
- immutable payload snapshots before persistence;
- `flush` + `fsync` before a successful append is returned;
- replay of byte-identical already-committed events as an idempotent success;
- rejection of conflicting reuse of either an event identity or an existing sequence;
- strict reopen validation before any further event can be appended.

The writer does **not** reserve a sequence number independently from its append. The next sequence becomes part of canonical history only when the corresponding event record is written through the durable append path.

## Reopen and recovery

Reopening `evidence.jsonl` validates the complete existing history before allocating the next sequence number.

A conforming stream must have:

- valid UTF-8;
- strict JSON with no duplicate object keys or `NaN`/infinite constants;
- exactly the current ATES event-envelope fields plus `payload`;
- canonical serialization bytes, not merely semantically equivalent JSON;
- the same `run_id` on every event;
- unique event IDs;
- sequences exactly `1..N` with no gaps or duplicates.

Malformed **complete** records fail closed and are never repaired automatically.

### Torn trailing write

A final non-newline-terminated tail is treated as an uncommitted/torn record. Normal reopen fails closed.

An operator/recovery path may explicitly request `repair_trailing_partial=True`. That operation truncates **only** bytes after the last committed newline and fsyncs the truncation. It does not skip, rewrite, or synthesize any complete event.

This keeps repair narrow enough to distinguish an interrupted final write from arbitrary evidence corruption.

## Ambiguous append failures

A storage failure after an event identity and sequence have been selected can have an ambiguous outcome: the full line may already be visible/durable even though the final `fsync` reported an error.

`AtesAppendError` therefore carries the exact `StoredEvent` identity and marks the writer poisoned. The writer cannot allocate later events until it is closed and reopened. Recovery then either:

- observes the exact canonical event and treats a retry of that identity as idempotent; or
- observes no complete record (or an explicit trailing torn record that must be repaired) and can safely reconcile before retrying the same logical event.

Callers must not respond to an uncertain append by inventing a new event ID for the same logical occurrence.

## Security / path boundary

The store validates the project, `.argus`, `runs`, and `<run-id>` directory chain before opening evidence. Symlink/reparse redirects are rejected where exposed by the platform, the run directory must remain contained beneath `.argus/runs`, and evidence/lock files must be regular files. POSIX file opens use `O_NOFOLLOW` where available.

This is the local evidence path boundary only. Artifact path confinement remains part of ATES Core/artifact handling.

## Deliberately not in PR #17

PR #17 does **not**:

- emit runtime lifecycle events from the runner or roam engine;
- implement `ACTION_DISPATCH_COMMITTED` runtime placement before side effects;
- create screenshots/checkpoints or other binary artifacts;
- compute evidence/artifact manifests;
- expose a run as canonically completed;
- implement the `RUN_COMPLETED` + evidence-manifest logical finalization transaction;
- render Markdown/JSON/HTML/JUnit reports;
- stream evidence through Fleet.

In particular, although the store can persist any currently defined `EventType`, runtime code should not begin treating a persisted `RUN_COMPLETED` line alone as proof of canonical completion. The ATES specification requires the later finalization layer to publish the exact final event and required evidence manifest as one logical crash-safe transaction before `completed` / `passed` becomes visible.

## Next layer

The next implementation PR can wire scripted and roam execution lifecycle events into this writer while preserving the separation between:

```text
ATES Core schema
    ↓
Durable canonical event store   ← PR #17
    ↓
Runner / roam event emission
    ↓
Privacy + artifact capture
    ↓
Transactional finalization / manifests
    ↓
Reports + Fleet streaming
```
