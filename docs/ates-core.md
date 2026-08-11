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
- runtime normalization/validation for typed IDs, enum-backed fields, mutable provenance, and integer sequence/revision positions when Core records are constructed from decoded or otherwise untyped input.

It does **not** yet persist `evidence.jsonl`, wire ATES into `argus run`/`argus roam`, capture screenshots, build manifests, render reports, or implement Fleet.

## Serialization boundary

Python type annotations are not treated as an input-validation boundary. Core records may eventually be reconstructed from JSON, Fleet messages, or other untyped sources, so typed identifiers are reconstructed through their concrete `AtesId` subclasses, enum-backed fields are normalized to their canonical enum values (or rejected), secret-bearing text positions require `EvidenceValue` at runtime, mutable provenance sequences are snapshotted before validation, and sequence/revision counters use explicitly validated integer domains.

This applies in particular to Run/Event/Step/Attempt/Action/Observation/Assertion/Artifact/Finding/finalization/correction identifiers, execution kinds, Step-attempt statuses, assertion results, evidence dispositions, verification statuses, run outcomes, and event types. A value with the wrong typed prefix is rejected even when it is otherwise a syntactically valid ATES identifier; for example an `EVT-*` value cannot become a `run_id`. The same rule prevents a serialized string such as `"failed"` from bypassing canonical status aggregation merely because identity comparisons expect `AssertionResult.FAILED`.

Collection-valued provenance also rejects scalar strings before snapshotting. In particular, `correction_ids="CORR-..."` is not treated as a sequence of characters, and every supplied correction is normalized as an actual `CorrectionId` before it can participate in an authoritative re-finalization.

Structured safe JSON is snapshotted into immutable-but-JSON-compatible containers, and `to_json_compatible()` provides an explicit conversion path for ATES dataclasses, enums, aware datetimes, mappings, and sequences. The Core therefore does not require callers to choose between immutable evidence snapshots and a working JSON serialization path.

Action parameter and Observation fact mappings follow a stricter snapshot rule: Core enumerates the supplied mapping exactly once, validates the resulting snapshot, and stores that same immutable snapshot. Validation and storage never make separate reads from an untrusted or concurrently mutable `Mapping`, preventing a custom mapping from presenting safe `EvidenceValue` entries during validation and different plaintext entries during copying.

## Secret-safe disposition reasons

The `reason` attached to `redacted`, `suppressed`, and `protected_ref` evidence is a **reason/policy code**, not a free-form diagnostic string. Core validation restricts it to a short lowercase code vocabulary (`a-z`, digits, `.`, `_`, `-`). Exception messages, model prose, credentials, tokens, and other free-form text must not be copied into this field; if explanatory evidence is needed it must be represented through its own secret-safe evidence channel.

This prevents an otherwise correctly redacted value from leaking the secret again through metadata such as `reason="redacted because token=..."`.

## Run configuration provenance

A committed scripted test source is not sufficient to identify a run's immutable inputs. `RunRecord.configuration_commitment` separately commits the effective runtime configuration using `SourceCommitment`, allowing callers to use a secret-redacted canonical commitment or a protected keyed commitment as appropriate. This keeps two runs of the same test specification distinguishable when provider, policy, environment, or other material runtime configuration differs without requiring plaintext secrets in ordinary evidence.

## Step-attempt history and evidence relationships

Individual `StepAttemptRecord` values enforce lifecycle consistency, while `validate_step_attempt_history()` enforces the cross-record retry invariants that cannot be decided by one record alone.

For every logical `step_id`:

- attempt IDs are unique;
- ordinals are unique and contiguous from 1;
- a later retry cannot begin while the prior attempt is still `RUNNING`;
- the prior attempt must have a terminal `ended_at` no later than the next attempt's `started_at`.

This rejects histories such as attempts `1, 1, 3`, retries launched before their cause has finished, overlapping attempts, and time-reversed retry sequences.

Opaque `StepAttemptId` values intentionally do not encode their parent step, so an isolated Action or Assertion cannot prove its foreign-key relationship by construction alone. Before a collection is treated as canonical evidence, `validate_step_evidence_relationships()` joins it against the validated Step-attempt history and returns immutable snapshots of the validated collection. It enforces that:

- every Action and Assertion references a known Step attempt and its declared `step_id` matches that attempt's owning Step;
- every Observation references a known Step attempt;
- an Assertion's optional `observation_id` names a known Observation from the same Step attempt;
- Action, Observation, and Assertion identities are unique within the canonical collection.

Callers that persist, package, or report multi-record evidence should use the snapshots returned by this aggregate validator rather than validating one list and later consuming a separately mutable collection.

## Artifact capture and byte integrity

A retained `ArtifactRecord` records its sensitivity/protection disposition, the applied `capture_policy` identity, a `content_digest` commitment over the final persisted bytes, and `size_bytes`. The commitment may use an appropriate digest/keyed-commitment method represented by `SourceCommitment`; the byte size is a non-negative integer. Together these fields bind the canonical record to the retained representation so replacement, truncation, or same-path mutation is detectable by later manifest/report integrity checks.

Artifact paths must already be canonical package-relative POSIX paths rooted below `artifacts/`. Encoded/traversal aliases, Windows separators, drive-like syntax, NULs, and other control characters are rejected before any later filesystem operation.

`SUPPRESSED` remains invalid for a retained-file record: suppression is represented by the `ARTIFACT_SUPPRESSED` event because no binary artifact exists to reference.

A retained artifact marked `protected_ref` additionally carries explicit protected-storage controls: `protected_ref`, `access_policy`, `retention_policy`, and `authorization_ref`. These fields identify the protected evidence boundary and the policy/authorization context needed to access or retain it. Conversely, ordinary/redacted artifact records cannot carry protected-store metadata accidentally.

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
10. Consumers that present the status for a particular evidence revision use the latest verified and authorized finalization applicable to that revision. They must not keep displaying revision-1 PASS after a valid successor re-finalizes the run as FAILED/ERROR/CANCELLED.
11. Reports/audit views should preserve both the historical completion outcome and the later effective outcome when they differ.

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
