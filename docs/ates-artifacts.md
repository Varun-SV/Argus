# ATES protected artifact capture

PR #21 adds the binary-evidence boundary that follows the text/JSON privacy pipeline from PR #20.

## Core rule

An artifact privacy decision happens **before the first persistent payload byte is written**.

Argus never writes an original screenshot/file to an ordinary path and then attempts to redact, mask, encrypt, move, or delete it afterwards. The allowed outcomes are:

- `safe` — the snapshotted bytes are permitted in the ordinary retained artifact namespace;
- `redacted` — a trusted sanitizer produces the complete retained representation in memory first, and only those transformed bytes are persisted;
- `protected_ref` — the bytes are written only to the protected run namespace and canonical ATES receives opaque protected metadata;
- `suppressed` — no payload file is created and `ARTIFACT_SUPPRESSED` records the decision.

The standard `ates-artifact-v1` policy is deliberately conservative: screenshots, finding evidence, explicit checkpoints, and collected guest files are protected unless a deployment explicitly configures another disposition.

## Storage authority

Artifact storage reuses the open `AtesEventStore` run authority established by PR #17.

- directories are created beneath the already-pinned `.argus/runs/<run>/` namespace;
- artifact paths are canonical package-relative `artifacts/...` paths;
- POSIX traversal and mutation stay descriptor-relative with no-follow semantics;
- Windows retains non-reparse directory handles without delete sharing;
- retained artifacts must be regular, singly-linked files;
- opaque `ArtifactId`-derived host filenames prevent guest/user filenames from becoming a disclosure channel.

This is filesystem confinement and local protected storage. It is **not** a claim of encryption at rest, WORM storage, digital signing, or tamper-evident finalization.

## Hashes

`ArtifactRecord.content_digest` is SHA-256 over the exact final representation that Argus retained. For redacted/masked artifacts this means the transformed bytes, never the discarded source bytes.

Artifact hashes become part of the run finalization/integrity transaction in PR #22. A hash stored locally beside mutable evidence is not by itself tamper evidence.

## Screenshots

The target policy for the standard profile is:

- failures: capture when a screenshot is available and policy permits;
- roam Findings: capture when evidence is available and policy permits;
- explicit checkpoints: supported by the artifact API;
- successful ordinary steps: do not create automatic ATES screenshots.

Protected screenshots are not automatically embedded into legacy Markdown reports. Report rendering and trust-state handling remain PR #23.

## Capsule collected files

Guest collection keeps the declared guest path as the read authority, but the ATES layer selects an independent opaque host destination before transfer. The guest filename therefore does not need to appear in canonical artifact metadata or the protected host filename.

A mapped collection transaction must complete all requested guest transfers before `ARTIFACT_COLLECTED` events are emitted. If a later transfer fails, the execution environment rolls back the files committed by that same transaction.

## Failure semantics

- oversize or policy-suppressed payloads create no artifact file;
- sanitizer failure creates no artifact file;
- a temporary write is not an Artifact until durable publication succeeds;
- a persisted artifact whose canonical event append becomes ambiguous is not silently deleted, because the event may already be durable; later reconciliation/finalization must treat the run as incomplete/error rather than fabricate absence;
- path/namespace identity loss fails closed.

## Deliberately deferred

PR #21 does not implement:

- evidence/artifact manifests;
- transactional `RUN_COMPLETED` publication;
- cryptographic signatures, transparency logs, or WORM retention;
- trusted Markdown/HTML/JUnit rendering;
- Fleet artifact transport or central retention.

Those remain later ATES/Fleet layers.
