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

## Run configuration provenance

A committed scripted test source is not sufficient to identify a run's immutable inputs. `RunRecord.configuration_commitment` separately commits the effective runtime configuration using `SourceCommitment`, allowing callers to use a secret-redacted canonical commitment or a protected keyed commitment as appropriate. This keeps two runs of the same test specification distinguishable when provider, policy, environment, or other material runtime configuration differs without requiring plaintext secrets in ordinary evidence.

## Artifact capture and byte integrity

A retained `ArtifactRecord` records its sensitivity/protection disposition, the applied `capture_policy` identity, a `content_digest` commitment over the final persisted bytes, and `size_bytes`. The commitment may use an appropriate digest/keyed-commitment method represented by `SourceCommitment`; the byte size is a non-negative integer. Together these fields bind the canonical record to the retained representation so replacement, truncation, or same-path mutation is detectable by later manifest/report integrity checks.

`SUPPRESSED` remains invalid for a retained-file record: suppression is represented by the `ARTIFACT_SUPPRESSED` event because no binary artifact exists to reference.

## Post-completion corrections and run outcome revisions

A Codex review submitted after PR #15 had already merged identified an important conflict: a verified correction can supersede status-bearing evidence (for example, a required Assertion can change from passed to failed), while the original immutable `RUN_COMPLETED.final_status` still records the earlier PASS.

ATES resolves this without rewriting history.

1. The original execution finalization is immutable and remains revision 1.
2. A correction that does not affect any canonical status input can create a later evidence/audit revision without changing the run outcome.
3. A **status-bearing** correction cannot become the authoritative current interpretation merely by being appended or included in a later manifest.
4. Applying a verified status-bearing correction requires a **new authenticated run outcome/finalization revision** over a newer evidence revision.
5. That successor names the immediately prior finalization, the typed correction record(s) that caused re-evaluation, the status-policy version, and the newly derived effective status.
6. Unverified or invalid corrections cannot create an authoritative successor outcome. The `verification` field must be an actual validated `Verification` record; an arbitrary object that merely exposes `status=VERIFIED` is not authentication.
7. Outcome revisions form a contiguous chain; a successor may not skip or fork the immediately prior authoritative finalization.
8. Consumers that present the status for a particular evidence revision use the latest verified finalization applicable to that revision. They must not keep displaying revision-1 PASS after a valid successor re-finalizes the run as FAILED/ERROR/CANCELLED.
9. Reports/audit views should preserve both the historical completion outcome and the later effective outcome when they differ.

Conceptually:

```text
execution evidence
    |
    +--> FINAL-1 / evidence revision 1 / PASSED
             |
             | verified CORR-7 changes required assertion
             v
         FINAL-2 / evidence revision 2 / FAILED
```

`FINAL-1` is never edited. `FINAL-2` is the effective outcome for revision 2 and later consumers until another valid successor is created.

The manifest/finalization transaction and authenticated correction storage are implemented in later ATES PRs; PR #16 defines and tests the Core identities and chain invariants needed for those implementations.
