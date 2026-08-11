# ATES durable event store

> **Status:** PR #17 implementation boundary. This layer persists canonical ATES events locally; it does not yet wire events into `argus run` / `argus roam`, generate manifests, or render reports.

PR #16 established the immutable ATES Core schema. PR #17 adds the first persistence primitive: one append-only `evidence.jsonl` stream per ATES `run_id`.

## Layout

```text
.argus/runs/
  .ates-authority-<filesystem-run-key>.lock   # POSIX per-RunId namespace authority
  <filesystem-run-key>/
    .ates-writer.lock
    evidence.jsonl
```

`<filesystem-run-key>` is an injective encoding of the canonical `RunId`, not a second run identity. Ordinary lowercase IDs keep their familiar spelling; uppercase suffix letters and underscores are escaped so distinct valid IDs such as `RUN-abc` and `RUN-ABC` cannot alias on case-insensitive filesystems. The canonical `run_id` remains the typed value recorded in every event and is verified again on reopen.

The parent-scoped `.ates-authority-<filesystem-run-key>.lock` is a POSIX writer-authority anchor. Windows does not create that sidecar because the retained Windows directory handles are opened without delete sharing and already deny the relevant rename/replacement operation.

Each line of `evidence.jsonl` is one canonical UTF-8 JSON object containing the ATES event envelope plus an object-valued `payload`.

## Canonical append contract

The local authoritative writer enforces:

- exactly one writer authority for a run at a time, with an in-process lock plus cross-process authority locking;
- on POSIX, a per-`RunId` authority lock is acquired in the already-pinned `runs/` parent **before** the replaceable run-directory entry is created or opened;
- every POSIX authority proof revalidates the full pinned descendant chain from the project trust root through `.argus` and `runs` to the run directory; the ancestor edges are checked on both sides of the inner per-run authority proof so a still-open hidden descendant cannot be accepted after an ancestor rename/replacement;
- the pinned run-directory inode and `.ates-writer.lock` retain their own exclusive `flock` barriers beneath that parent-scoped authority, so a single namespace-entry replacement does not mint another conforming writer;
- the canonical run-directory name is revalidated against the pinned run-directory inode before use and around durability acknowledgements;
- the canonical `evidence.jsonl` pathname is revalidated against the already-open evidence descriptor before and after durability barriers on POSIX;
- the exact committed byte length of the validated stream is cached after reopen and checked before every new append and around replay/durability acknowledgement;
- stable typed `run_id` / `event_id` identities;
- consecutive, gap-free sequence assignment beginning at `1`;
- canonical JSON serialization using sorted keys, compact separators, UTF-8, and no non-finite numeric values;
- one newline-terminated event per record;
- immutable payload snapshots before persistence;
- `flush` + `fsync` before a successful append is returned;
- on POSIX, every hierarchy traversal re-establishes the parent-directory durability barrier even when `.argus`, `runs`, or the run directory already exists; this recovers a retry where an earlier `mkdir` succeeded but its parent `fsync` failed;
- created authority/lock/evidence entries are synced through their pinned parent directories before durable success is claimed;
- a successful store open re-establishes an evidence-file durability barrier and syncs the run directory before existing history is accepted, including retries after an earlier initialization `fsync` failure;
- canonical authority, evidence, and writer-lock files must have exactly one hard link where those files exist; evidence identity and extent are checked again immediately before write/replay and after their durability barriers;
- replay of a byte-identical already-committed event is acknowledged only after the current evidence handle is successfully flushed and fsynced again and its canonical pathname/extent and directory authority chain are re-proven;
- rejection of conflicting reuse of either an event identity or an existing sequence;
- strict reopen validation before any further event can be appended.

The writer does **not** reserve a sequence number independently from its append. The next sequence becomes part of canonical history only when the corresponding event record is written through the durable append path.

Untyped API inputs also fail closed. A supplied payload is validated even when it is falsy; only `payload=None` selects the empty-object default. Likewise, only `occurred_at=None` selects the current time. `repair_trailing_partial` must be an actual boolean rather than a truthy configuration value.

## Reopen and recovery

Reopening `evidence.jsonl` re-establishes the namespace and durability barriers and then validates the complete existing history before allocating the next sequence number. The read path checks evidence identity before reading, verifies that the descriptor length still matches the bytes that were read, and checks identity/length again before accepting the history as the new cached committed extent. POSIX authority checks also re-prove each canonical directory edge from the pinned project root to the run directory.

A conforming stream must have:

- valid UTF-8;
- strict JSON with no duplicate object keys or `NaN`/infinite constants;
- JSON nesting that can be decoded, converted into immutable canonical ATES payload values, and re-serialized without exceeding recursion limits; recursion failure at any of those persisted-record stages is `AtesStoreCorruption`;
- exactly the current ATES event-envelope fields plus `payload`;
- canonical serialization bytes, not merely semantically equivalent JSON;
- canonical content that can be re-encoded as ATES UTF-8 JSON; an escaped but unencodable lone surrogate is persisted corruption, not a generic serialization exception;
- the same `run_id` on every event;
- unique event IDs;
- sequences exactly `1..N` with no gaps or duplicates.

Malformed **complete** records fail closed and are never repaired automatically. Initialization failures close already-open evidence, lock, authority, and directory handles before propagating the original failure. On POSIX, a failed writer construction releases any parent-scoped per-run authority and run-directory/marker locks that it acquired, so a later valid writer is not left permanently busy.

### Torn trailing write

A final non-newline-terminated tail is treated as an uncommitted/torn record. Normal reopen fails closed.

An operator/recovery path may explicitly request `repair_trailing_partial=True`. That operation truncates **only** bytes after the last committed newline and fsyncs the truncation. It does not skip, rewrite, or synthesize any complete event. Non-boolean repair values are rejected before the store hierarchy is touched, so a string such as `"false"` cannot accidentally authorize destructive repair. Namespace authority plus canonical evidence identity/extent are checked before the truncation and again after its durability barrier.

This keeps repair narrow enough to distinguish an interrupted final write from arbitrary evidence corruption.

## Ambiguous append failures

A storage failure, control-flow interruption, loss of canonical namespace identity, or evidence identity/extent change after append I/O has begun can have an ambiguous outcome: the full line may already be visible/durable even though Argus can no longer safely acknowledge it as the canonical run history.

For ordinary storage/validation/namespace failures, `AtesAppendError` carries the exact `StoredEvent` identity and marks the writer poisoned once append I/O has begun. For control-flow `BaseException` values such as `KeyboardInterrupt` or `SystemExit`, the original exception is preserved rather than wrapped, but the store is still poisoned first once write I/O has begun. The writer cannot allocate later events until it is closed and reopened.

In particular, after the evidence `fsync`, POSIX re-proves the canonical `project -> .argus -> runs -> run` chain as well as the canonical `evidence.jsonl` inode. If `.argus`, `runs`, the run directory, or the evidence entry was renamed/replaced while that append was in flight, Argus does not return success: it poisons the writer and reports the stable event as an ambiguous append requiring reconciliation. The expected post-append byte length is also checked after `fsync`, so a concurrent truncate cannot turn sequence `N+1` into a false success over a shortened stream.

If namespace/evidence identity or committed length has already changed **before** a new append begins, no new append I/O is attempted. The active store is nevertheless invalid for continued writing because its cached history/authority can no longer be trusted; the caller must close and reopen against the canonical tree so the complete stream is validated again.

Recovery then either:

- observes the exact canonical event and re-establishes the complete namespace chain, evidence identity/extent, and durability authority; retrying that exact identity performs another evidence-file `flush` + `fsync` before idempotent success is returned; or
- observes no complete record (or an explicit trailing torn record that must be repaired) and can safely reconcile before retrying the same logical event.

A replay durability, namespace-authority, evidence-identity, or evidence-extent failure is itself ambiguous: it poisons the writer and, for ordinary storage failures, returns `AtesAppendError` with the same stable event identity. Visibility alone is never sufficient to acknowledge an event whose durability or canonical namespace was previously uncertain.

Callers must not respond to an uncertain append by inventing a new event ID for the same logical occurrence.

## Lock failure classification and lifecycle

POSIX non-blocking `flock` acquisition distinguishes actual contention from operational filesystem failures. Only `EACCES` / `EAGAIN` is reported as `AtesStoreBusy`. Unsupported-locking, I/O, descriptor, or other `flock` failures are surfaced as `AtesStoreError` so callers do not misreport them as another writer or retry forever under a false contention diagnosis. This classification applies to the parent-scoped per-run authority, run-directory inode lock, and marker-file lock.

Explicit `close()` remains the preferred lifetime boundary. As a safety net, abandoned stores use best-effort finalization to close the evidence file, writer locks, and raw pinned directory descriptors so a dropped store cannot retain POSIX `flock` authority until process exit. Finalization performs no append, replay, repair, or durability work and suppresses cleanup errors because destructors cannot report a reliable operational result.

The same finalization path preserves fork safety: inherited child resources are closed without issuing explicit `LOCK_UN` calls against the parent's shared open-file descriptions. A child must still reopen a fresh store before using ATES persistence.

## Security / path boundary

The store treats the resolved `project_dir` as its filesystem trust root and pins the project, `.argus`, `runs`, and run-directory chain for the writer lifetime rather than validating path strings and later reopening descendants by name.

On POSIX, child directories and files are created/opened relative to already-open directory descriptors with no-follow semantics where supported. Before the canonical run directory is created or opened, the store acquires an exclusive non-blocking `flock` on `.ates-authority-<filesystem-run-key>.lock` inside the already-pinned `runs/` parent. This authority is scoped to one `RunId`, so unrelated runs are not globally serialized.

Holding descendant descriptors is not by itself enough: POSIX permits a directory such as `.argus/runs` to be renamed while its descriptor and all descendants remain usable. Therefore every authority proof compares the canonical `.argus` entry with the pinned `.argus` inode, the canonical `runs` entry with the pinned `runs` inode, and the canonical run entry with the pinned run inode. The `.argus` and `runs` edges are checked before and after the inner per-run authority/run proof. If an ancestor is renamed/recreated, the old hidden store fails closed instead of continuing to report writes as canonical merely because its descendant descriptors still work.

After the parent-scoped per-run authority is held, the store opens the canonical run-directory entry, verifies that its name still resolves to the pinned inode, locks the run-directory inode, and retains the `.ates-writer.lock` `flock`. Replacing only the run directory therefore cannot give a second conforming writer authority within the same canonical `runs` namespace: the second writer is rejected at the parent-scoped authority before it can create/open a replacement run directory. If a broader ancestor namespace is deliberately replaced, a newly constructed store may exist in that new canonical tree, but the old hidden writer is invalidated by full-chain revalidation and cannot acknowledge subsequent appends as canonical.

The canonical evidence file receives the same pathname-to-descriptor identity treatment on POSIX. Renaming `evidence.jsonl` and creating a replacement after the original descriptor was opened cannot be acknowledged as success merely because the old descriptor still fsyncs: the post-barrier identity check detects that the canonical name points at another inode. On Windows, evidence handles omit delete sharing, so the corresponding rename/replacement is denied while the handle is retained.

The authority sidecar itself is treated as a canonical single-link regular file and its pathname-to-handle identity is revalidated. Replacing only that sidecar does not mint another writer because the existing run-directory inode remains independently locked; the original writer detects the lost authority and fails closed. Likewise, replacing or unlinking only `.ates-writer.lock` cannot mint another writer because the parent-scoped per-run authority and run-directory inode lock remain held.

Every successful hierarchy traversal on POSIX fsyncs the parent directory even when the child already exists. This deliberately re-proves namespace durability after a prior attempt in which `mkdir` succeeded but its parent `fsync` failed; lower-level file fsyncs cannot substitute for durability of an ancestor directory entry.

On Windows, the hierarchy is retained through non-reparse directory handles opened without delete sharing, preventing rename/replacement while the store is active. Evidence and lock files are opened through validated Windows handles and reparse points are rejected. The POSIX parent authority sidecar and repeated inode-edge checks are therefore not needed on Windows. Files on all platforms must resolve as regular files, not devices/FIFOs/sockets/link targets, and canonical store files with multiple hard links are rejected so separate authorities cannot target one underlying file through aliases. External write/truncate sharing is still guarded by the cached committed-length checks before and after append/replay barriers.

A store object is also process-owned. On POSIX, an instance inherited through `fork()` is invalid in the child and must be closed/reopened there. The child cleanup path closes only its inherited descriptors and deliberately does not issue an explicit unlock for the parent-scoped authority, marker-file, or run-directory `flock`, which would release the parent's shared open-file-description authority.

These mechanisms protect Argus against accidental cleanup/replacement races and the reviewed namespace/alias/extent failure modes. The resolved `project_dir` itself is the trust root for this layer; PR #17 does not attempt to recursively anchor every ancestor above that project directory. The store is also **not** a cryptographic tamper-detection mechanism for an actor that can deliberately rewrite bytes in place while preserving the same file identity and length, nor is it a substitute for OS permissions against an actor with unrestricted ability to rewrite the project tree. Strong adversarial tamper evidence belongs in a later manifest/integrity layer.

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
