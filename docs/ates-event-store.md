# ATES durable event store

> **Status:** PR #17 implementation boundary. This layer persists canonical ATES events locally; it does not yet wire events into `argus run` / `argus roam`, generate manifests, or render reports.

PR #16 established the immutable ATES Core schema. PR #17 adds the first persistence primitive: one append-only `evidence.jsonl` stream per ATES `run_id`.

## Layout

```text
.argus/runs/<filesystem-run-key>/
  .ates-writer.lock
  evidence.jsonl
```

`<filesystem-run-key>` is an injective encoding of the canonical `RunId`, not a second run identity. Ordinary lowercase IDs keep their familiar spelling; uppercase suffix letters and underscores are escaped so distinct valid IDs such as `RUN-abc` and `RUN-ABC` cannot alias on case-insensitive filesystems. The canonical `run_id` remains the typed value recorded in every event and is verified again on reopen.

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
- parent-directory durability for newly created `.argus`, `runs`, run-directory, lock-file, and evidence-file entries where POSIX directory syncing is available;
- replay of byte-identical already-committed events as an idempotent success;
- rejection of conflicting reuse of either an event identity or an existing sequence;
- strict reopen validation before any further event can be appended.

The writer does **not** reserve a sequence number independently from its append. The next sequence becomes part of canonical history only when the corresponding event record is written through the durable append path.

Untyped API inputs also fail closed. A supplied payload is validated even when it is falsy; only `payload=None` selects the empty-object default. Likewise, only `occurred_at=None` selects the current time. `repair_trailing_partial` must be an actual boolean rather than a truthy configuration value.

## Reopen and recovery

Reopening `evidence.jsonl` validates the complete existing history before allocating the next sequence number.

A conforming stream must have:

- valid UTF-8;
- strict JSON with no duplicate object keys or `NaN`/infinite constants;
- exactly the current ATES event-envelope fields plus `payload`;
- canonical serialization bytes, not merely semantically equivalent JSON;
- canonical content that can be re-encoded as ATES UTF-8 JSON; an escaped but unencodable lone surrogate is persisted corruption, not a generic serialization exception;
- the same `run_id` on every event;
- unique event IDs;
- sequences exactly `1..N` with no gaps or duplicates.

Malformed **complete** records fail closed and are never repaired automatically. Initialization failures close already-open evidence, lock, and directory handles before propagating the original failure; partial lock-file initialization follows the same cleanup rule.

### Torn trailing write

A final non-newline-terminated tail is treated as an uncommitted/torn record. Normal reopen fails closed.

An operator/recovery path may explicitly request `repair_trailing_partial=True`. That operation truncates **only** bytes after the last committed newline and fsyncs the truncation. It does not skip, rewrite, or synthesize any complete event. Non-boolean repair values are rejected before the store hierarchy is touched, so a string such as `"false"` cannot accidentally authorize destructive repair.

This keeps repair narrow enough to distinguish an interrupted final write from arbitrary evidence corruption.

## Ambiguous append failures

A storage failure after an event identity and sequence have been selected can have an ambiguous outcome: the full line may already be visible/durable even though the final `fsync` reported an error.

`AtesAppendError` therefore carries the exact `StoredEvent` identity and marks the writer poisoned. The writer cannot allocate later events until it is closed and reopened. Recovery then either:

- observes the exact canonical event and treats a retry of that identity as idempotent; or
- observes no complete record (or an explicit trailing torn record that must be repaired) and can safely reconcile before retrying the same logical event.

Callers must not respond to an uncertain append by inventing a new event ID for the same logical occurrence.

## Security / path boundary

The store pins the project, `.argus`, `runs`, and run-directory chain for the writer lifetime rather than validating path strings and later reopening them by name.

On POSIX, child directories and files are created/opened relative to already-open directory descriptors with no-follow semantics where supported. This keeps the lock and `evidence.jsonl` bound to the validated run directory even if an attacker renames the visible path and replaces it with a symlink between validation and file open.

On Windows, the hierarchy is retained through non-reparse directory handles opened without delete sharing, preventing rename/replacement while the store is active. Evidence and lock files are opened through validated Windows handles and reparse points are rejected. Files on all platforms must resolve as regular files, not devices/FIFOs/sockets/link targets.

A store object is also process-owned. On POSIX, an instance inherited through `fork()` is invalid in the child and must be closed/reopened there. The child cleanup path closes only its inherited descriptors and deliberately does not issue an explicit unlock that could release the parent writer's shared `flock` open-file description.

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
