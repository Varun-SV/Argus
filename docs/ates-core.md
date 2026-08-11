# ATES Core implementation notes

> **Status:** implementation companion to `docs/ates.md` for the ATES Core work beginning in PR #16.

This document records implementation-level invariants that are intentionally kept smaller than the full ATES design specification.

## PR #16 boundary

PR #16 establishes dependency-free Core schema primitives only:

- typed stable IDs;
- ATES schema version and event envelope;
- scripted versus roam execution-source identity;
- a secret-safe run-level configuration commitment so equivalent test sources executed under materially different runtime configuration remain distinguishable;
- immutable logical Step and Step-attempt identity;
- canonical run-status values and precedence;
- secret-safe evidence value dispositions;
- Action, Observation, Assertion, Finding, Artifact, and Requirement identity records;
- retained Artifact records that identify the capture policy applied to persisted bytes and bind to those final bytes by content commitment and byte size;
- artifact-path schema confinement;
- revisioned run outcome/finalization identity;
- runtime normalization/validation for typed IDs, enum-backed fields, mutable provenance, timestamps, and integer sequence/revision positions when Core records are constructed from decoded or otherwise untyped input.

It does **not** yet persist `evidence.jsonl`, wire ATES into `argus run`/`argus roam`, capture screenshots, build manifests, render reports, or implement Fleet.

## Serialization and immutability boundary

Python type annotations are not treated as an input-validation boundary. Core records may eventually be reconstructed from JSON, Fleet messages, plugins, or other untyped sources, so typed identifiers are reconstructed through their concrete `AtesId` subclasses, enum-backed fields are normalized to their canonical enum values (or rejected), secret-bearing text positions require `EvidenceValue` at runtime, mutable provenance collections are snapshotted before validation, and sequence/revision counters use explicitly validated integer domains.

This applies in particular to Run/Event/Step/Attempt/Action/Observation/Assertion/Artifact/Finding/finalization/correction identifiers, execution kinds, Step-attempt statuses, assertion results, evidence dispositions, verification statuses, run outcomes, and event types. A value with the wrong typed prefix is rejected even when it is otherwise a syntactically valid ATES identifier; for example an `EVT-*` value cannot become a `run_id`. The same rule prevents a serialized string such as `"failed"` from bypassing canonical status aggregation merely because identity comparisons expect `AssertionResult.FAILED`.

Declared sequence fields reject scalar strings/bytes, mappings, sets, and other non-sequence impostors before snapshotting. This applies not only to finalization/correction collections but also to `StatusInputs.required_assertion_results`, secret references, Finding evidence references, Step/attempt/action/observation/assertion collections, and other Core collection boundaries. In particular, `correction_ids="CORR-..."` is not treated as a sequence of characters, `{}` is not silently interpreted as an empty evidence collection, and every supplied correction is normalized as an actual `CorrectionId` before it can participate in an authoritative re-finalization.

Structured safe JSON is snapshotted into deeply immutable containers. Mapping snapshots use `FrozenDict`, which implements the read-only `Mapping` interface over immutable tuple storage and deliberately does **not** inherit from `dict`; builtin mutators such as `dict.__setitem__`, `dict.update`, or `dict.clear` therefore have no mutable dictionary storage to bypass. Nested sequences become tuples and nested mappings become `FrozenDict` recursively.

`to_json_compatible()` is the explicit serialization boundary for ATES dataclasses, enums, aware datetimes, mappings, and sequences. It returns ordinary detached JSON containers, so mutating serialized output cannot mutate canonical evidence. `dataclasses.asdict()` may also obtain detached copies for convenience, but callers must never treat such detached mutable copies as canonical evidence state.

Safe structured `EvidenceValue` mappings are snapshotted and frozen in one traversal: Core does not validate one view of an untrusted/custom `Mapping` and then read it a second time to create the retained value. Action parameter and Observation fact mappings follow the same rule. Core enumerates the supplied mapping exactly once, validates the resulting snapshot, and stores that exact immutable snapshot. This prevents custom mappings or concurrent mutation from presenting safe values during validation and different plaintext values during copying.

## Canonical timestamps

An aware Python `datetime` may itself reference a mutable custom `tzinfo` object. Merely freezing the enclosing dataclass therefore does not make the represented instant immutable.

All canonical Core timestamps are normalized at construction to an immutable UTC `datetime` after reading the supplied timezone offset once. This applies to run start time, Step-attempt start/end times, Observation capture time, run finalization time, and Event occurrence time. Later mutation of a caller-owned/custom timezone object therefore cannot change serialized evidence or chronology comparisons. `to_json_compatible()` applies the same normalization rule when directly serializing an aware datetime.

## Secret-safe disposition reasons

The `reason` attached to `redacted`, `suppressed`, and `protected_ref` evidence is a **reason/policy code**, not a free-form diagnostic string. Core validation restricts it to a short lowercase code vocabulary (`a-z`, digits, `.`, `_`, `-`). Exception messages, model prose, credentials, tokens, and other free-form text must not be copied into this field; if explanatory evidence is needed it must be represented through its own secret-safe evidence channel.

This prevents an otherwise correctly redacted value from leaking the secret again through metadata such as `reason="redacted because token=..."`.

## Run configuration provenance

A committed scripted test source is not sufficient to identify a run's immutable inputs. `RunRecord.configuration_commitment` separately commits the effective runtime configuration using `SourceCommitment`, allowing callers to use a secret-redacted canonical commitment or a protected keyed commitment as appropriate. This keeps two runs of the same test specification distinguishable when provider, policy, environment, or other material runtime configuration differs without requiring plaintext secrets in ordinary evidence.

Optional run identity metadata (`provider`, `model_provider`, and `model`) is validated at the same decoded-input boundary. When present, each value must be a nonempty string; lists, objects, empty strings, and other untyped values cannot enter canonical run provenance merely because the Python annotation is optional.

## Requirement traceability

`RequirementIdentity` is the immutable meaning of a requirement reference: source system, display ID, version/source revision, and optional complete `SourceCommitment` semantics participate in its identity. Two values such as `REQ-42@v2` and `REQ-42@v3` therefore remain distinct even if their display ID is the same.

`AssertionRecord.requirement` optionally binds an assertion directly to that frozen `RequirementIdentity`. Requirement-backed scripted assertions can therefore prove exactly which requirement meaning was evaluated, while runtime assertions that are not requirement coverage do not have to fabricate a requirement association. Coverage/reporting consumers should use the associated `RequirementIdentity.identity_key` rather than grouping only by display ID.

## Step-attempt history and evidence relationships

Individual `StepAttemptRecord` values enforce lifecycle consistency, while `validate_step_attempt_history()` enforces the cross-record retry invariants that cannot be decided by one record alone.

For every logical `step_id`:

- attempt IDs are unique;
- ordinals are unique and contiguous from 1;
- a later retry cannot begin while the prior attempt is still `RUNNING`;
- the prior attempt must have a terminal `ended_at` no later than the next attempt's `started_at`.

This rejects histories such as attempts `1, 1, 3`, retries launched before their cause has finished, overlapping attempts, and time-reversed retry sequences.

Opaque `StepAttemptId` values intentionally do not encode their parent Step, so canonical multi-record evidence must validate both the logical execution intent and its derived records. `validate_step_evidence_relationships()` snapshots the supplied logical `StepRecord`s together with attempts/actions/observations/assertions and enforces that:

- logical Step IDs are unique;
- every Step attempt resolves to exactly one supplied logical Step;
- every Action and Assertion references a known Step attempt and its declared `step_id` matches that attempt's owning Step;
- every Observation references a known Step attempt;
- an Assertion's optional `observation_id` names a known Observation from the same Step attempt;
- Action, Observation, and Assertion identities are unique within the canonical collection;
- every non-null `ActionOperationId` is unique across distinct canonical Action records, so provider/adapter deduplication cannot collapse two different side effects onto one operation identity. Transport retries reuse the original Action record and operation identity instead of creating a second distinct Action with that ID.

The aggregate validator returns immutable snapshots of the logical Steps and every validated related collection. Callers that persist, package, or report multi-record evidence should consume those returned snapshots rather than validating one list and later consuming separately mutable input collections.

## Finding classification

`FindingRecord` stores both the machine-readable `classification` and `classification_source`. A producer can therefore preserve values such as `critical`, `informational`, or a project-specific category together with whether that value came from a model, deterministic policy engine, human reviewer, or imported tool. Consumers do not need to parse secret-safe Finding prose to recover severity/category semantics. The default `unclassified` value represents the absence of a stronger assigned classification; both fields must be nonempty strings.

## Artifact capture and byte integrity

A retained `ArtifactRecord` records its sensitivity/protection disposition, the applied `capture_policy` identity, a `content_digest` commitment over the final persisted bytes, and `size_bytes`. The commitment may use an appropriate digest/keyed-commitment method represented by `SourceCommitment`; the byte size is a non-negative integer. Together these fields bind the canonical record to the retained representation so replacement, truncation, or same-path mutation is detectable by later manifest/report integrity checks.

Artifact paths must already be canonical package-relative POSIX paths rooted below `artifacts/`. Encoded/traversal aliases, Windows separators, drive-like syntax, NULs, and other control characters are rejected before any later filesystem operation. Because the evidence package is expected to work on both Windows and POSIX systems, Core also rejects Windows device-name components (for example `CON`, `AUX`, `COM1`, `LPT1`, including extension aliases), components ending in a dot or space, and Win32-invalid component characters. A path that is distinct as POSIX text must not silently normalize/collide or fail extraction when consumed on Windows.

`SUPPRESSED` remains invalid for a retained-file record: suppression is represented by the `ARTIFACT_SUPPRESSED` event because no binary artifact exists to reference.

A retained artifact marked `protected_ref` additionally carries explicit protected-storage controls: `protected_ref`, `access_policy`, `retention_policy`, and `authorization_ref`. These fields identify the protected evidence boundary and the policy/authorization context needed to access or retain it. Conversely, ordinary/redacted artifact records cannot carry protected-store metadata accidentally.

## Status derivation boundary

`StatusInputs` is itself the normalization/validation boundary for canonical status derivation. `derive_run_status()` requires an actual validated `StatusInputs` instance rather than accepting an attribute-shaped plugin/Fleet object. `required_assertion_results` must be a real sequence and is snapshotted once before every entry is normalized to `AssertionResult`; malformed empty strings, bytes, mappings, or other collection impostors therefore cannot collapse into an apparently empty successful assertion set.

## Post-completion corrections and run outcome revisions

A Codex review submitted after PR #15 had already merged identified an important conflict: a verified correction can supersede status-bearing evidence (for example, a required Assertion can change from passed to failed), while the original immutable `RUN_COMPLETED.final_status` still records the earlier PASS.

ATES resolves this without rewriting history.

1. The original execution finalization is immutable and remains revision 1.
2. A correction that does not affect any canonical status input can create a later evidence/audit revision without changing the run outcome.
3. A **status-bearing** correction cannot become the authoritative current interpretation merely by being appended or included in a later manifest.
4. Applying a verified status-bearing correction requires a **new authenticated run outcome/finalization revision** over a newer evidence revision.
5. That successor names the immediately prior finalization, the typed correction record(s) that caused re-evaluation, the status-policy version, and the newly derived effective status.
6. Unverified or invalid corrections cannot create an authoritative successor outcome. The `verification` field must be an actual validated `Verification` record; an arbitrary object that merely exposes `status=VERIFIED` is not authentication.
7. Authentication is not authorization. A status-bearing successor additionally requires a verified `AuthorizationDecision` under an explicit versioned policy and the `run_outcome.refinalize` scope.
8. Authorization decisions are not reusable bearer approvals. The decision names the authenticated subject and binds the exact proposed run ID, successor finalization ID, superseded finalization ID, correction IDs, effective status, evidence revision, and status-policy version. The subject must equal the actor authenticated by the successor's `Verification` record. A decision for an administrator therefore cannot be replayed by an unprivileged authenticated actor or onto a different run/outcome payload.
9. Outcome revisions form a contiguous chain; a successor may not skip or fork the immediately prior authoritative finalization.
10. Successor finalization timestamps are monotonic: a revision may not claim a `finalized_at` earlier than the immediately prior authoritative revision.
11. `effective_outcome()` snapshots the supplied history, rejects unsupported collection types, and requires **every** item to be an actual validated `RunOutcomeRevision` before sorting, chaining, or selection. A plugin/Fleet object that merely exposes similarly named attributes cannot bypass the authentication/authorization invariants enforced during `RunOutcomeRevision` construction.
12. Consumers that present the status for a particular evidence revision use the latest verified and authorized finalization applicable to that revision. They must not keep displaying revision-1 PASS after a valid successor re-finalizes the run as FAILED/ERROR/CANCELLED.
13. Reports/audit views should preserve both the historical completion outcome and the later effective outcome when they differ.

Conceptually:

```text
execution evidence
    |
    +--> FINAL-1 / evidence revision 1 / PASSED
             |
             | verified CORR-7 changes required assertion
             | authenticated subject + exact authorization binding
             v
         FINAL-2 / evidence revision 2 / FAILED
```

`FINAL-1` is never edited. `FINAL-2` is the effective outcome for revision 2 and later consumers until another valid successor is created.

The manifest/finalization transaction and authenticated correction storage are implemented in later ATES PRs; PR #16 defines and tests the Core identities and chain invariants needed for those implementations.
