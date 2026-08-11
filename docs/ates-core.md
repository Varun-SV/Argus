# ATES Core implementation notes

> **Status:** implementation companion to `docs/ates.md` for the ATES Core work beginning in PR #16.

This document records implementation-level invariants that are intentionally kept smaller than the full ATES design specification.

## PR #16 boundary

PR #16 establishes dependency-free Core schema primitives only:

- typed stable IDs;
- ATES schema version and event envelope;
- scripted versus roam execution-source identity;
- immutable logical Step and Step-attempt identity;
- canonical run-status values and precedence;
- secret-safe evidence value dispositions;
- Action, Observation, Assertion, Finding, Artifact, and Requirement identity records;
- artifact-path schema confinement;
- revisioned run outcome/finalization identity.

It does **not** yet persist `evidence.jsonl`, wire ATES into `argus run`/`argus roam`, capture screenshots, build manifests, render reports, or implement Fleet.

## Post-completion corrections and run outcome revisions

A Codex review submitted after PR #15 had already merged identified an important conflict: a verified correction can supersede status-bearing evidence (for example, a required Assertion can change from passed to failed), while the original immutable `RUN_COMPLETED.final_status` still records the earlier PASS.

ATES resolves this without rewriting history.

1. The original execution finalization is immutable and remains revision 1.
2. A correction that does not affect any canonical status input can create a later evidence/audit revision without changing the run outcome.
3. A **status-bearing** correction cannot become the authoritative current interpretation merely by being appended or included in a later manifest.
4. Applying a verified status-bearing correction requires a **new authenticated run outcome/finalization revision** over a newer evidence revision.
5. That successor names the immediately prior finalization, the correction record(s) that caused re-evaluation, the status-policy version, and the newly derived effective status.
6. Unverified or invalid corrections cannot create an authoritative successor outcome.
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
