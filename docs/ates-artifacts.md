# ATES protected artifact capture

PR #21 adds the binary-evidence boundary that follows the text/JSON privacy pipeline from PR #20.

## Core rule

An artifact privacy decision happens **before the first persistent payload byte is written**.

Argus never writes an original screenshot/file to an ordinary path and then attempts to redact, mask, encrypt, move, or delete it afterwards. The artifact repository supports four outcomes:

- `safe` — the snapshotted bytes are permitted in the ordinary retained artifact namespace;
- `redacted` — a trusted sanitizer produces the complete retained representation in memory first, and only those transformed bytes are persisted;
- `protected_ref` — the bytes are written only to the protected run namespace and canonical ATES receives opaque protected metadata;
- `suppressed` — no payload file is created and `ARTIFACT_SUPPRESSED` records a machine-readable reason code.

The integrated Runner/Roam path in PR #21 deliberately uses one fixed profile, `ates-artifact-v1`, which treats supported screenshots and declared guest files as protected evidence. The lower-level repository has primitives for safe/redacted/suppressed policies, but non-standard runtime policy selection remains gated until the selected profile can be bound into authoritative run provenance/finalization in PR #22.

## Storage authority

Artifact storage reuses the open `AtesEventStore` run authority established by PR #17.

- directories are created beneath the already-pinned `.argus/runs/<run>/` namespace;
- artifact paths are canonical package-relative `artifacts/...` paths;
- POSIX traversal and mutation stay descriptor-relative with no-follow semantics;
- Windows retains non-reparse directory handles without delete sharing;
- retained artifacts must be regular, singly-linked files;
- opaque `ArtifactId`-derived host filenames prevent guest/user filenames from becoming a disclosure channel;
- unbuffered writes use explicit write-all loops so short writes cannot silently truncate evidence;
- publication is no-overwrite and includes a directory durability/authority barrier; a failure after the final name appears triggers a durable rollback or an explicit ambiguous-cleanup error.

`protected_ref` in PR #21 means a separate non-report evidence class, an opaque reference, and confinement under the protected run namespace. It is **not** a claim of encryption at rest, WORM storage, digital signing, tamper evidence, or a guaranteed per-user Windows ACL. Windows access remains governed by the filesystem ACL inherited from the project/run namespace.

## Content commitments

ATES commits to the exact final representation that was retained, but the commitment form depends on the evidence class.

### Safe and redacted artifacts

`safe` and `redacted` `ArtifactRecord.content_digest` values use SHA-256 over the exact persisted bytes. For redacted/masked artifacts this is the transformed representation, never the discarded source bytes.

### Protected artifacts

Canonical evidence **does not expose the raw SHA-256 of a protected artifact**. A raw digest can be a guessing oracle for a low-entropy or secret-capable payload.

For `protected_ref` artifacts Argus therefore:

1. computes SHA-256 over the exact retained bytes for local transfer/integrity verification;
2. keeps that raw digest transient and out of canonical/legacy metadata;
3. stores a run-local 256-bit secret in `.ates-artifact-hmac-key` under the pinned run namespace;
4. records `HMAC-SHA256(key, SHA256(final-bytes))` as the canonical `SourceCommitment`;
5. identifies the commitment with `ates-artifact-sha256-hmac-v1` and `secret://ates/run-artifact-hmac-key`.

The key is created with the same pinned run-directory authority. Partial/new-key initialization failure removes the incomplete key and durably syncs that cleanup; an inability to prove cleanup is surfaced as an error.

These commitments become inputs to the authoritative run finalization/integrity transaction in PR #22. A local commitment stored beside mutable evidence is not by itself tamper evidence.

## Screenshots

The standard runtime profile is intentionally sparse:

- failed scripted assertions: capture the screenshot from the exact Observation that decided the assertion when available;
- failed natural-language attempts: take one explicit failure-evidence Observation while the attempt is active;
- roam Findings: capture the available Finding screenshot through the artifact boundary;
- successful ordinary steps: do **not** create automatic ATES screenshots;
- missing/failed screenshot acquisition is represented by `ARTIFACT_SUPPRESSED` with a safe reason code rather than silently disappearing.

The legacy `shots_dir` parameter remains for API compatibility, but normal ATES execution does not write raw PNGs there. Public roam also withholds screenshot bytes from the legacy `_add_finding()` writer, so protected screenshots are not linked into ordinary roam reports.

Report rendering and trust-state handling remain PR #23.

## Capsule collected files

Guest collection keeps the declared guest path as the read authority, but the ATES layer selects an independent opaque host destination before transfer. The guest filename therefore does not appear in canonical artifact metadata, the protected host filename, or legacy `RunResult.artifacts` metadata.

The mapped collection path:

1. preflights each declared guest source and the total transfer size;
2. assigns an independent protected `ArtifactId` destination;
3. streams authenticated guest chunks to the pinned destination using write-all + file fsync;
4. verifies the guest SHA-256 and rechecks that the guest file did not change during transfer;
5. rereads the retained host file through the pinned namespace and validates size/hash/regular-file identity;
6. creates the secret-safe protected content commitment;
7. emits `ARTIFACT_COLLECTED` only after every file in the transaction has passed registration validation.

If transfer or pre-event registration validation fails, payloads committed by that transaction are rolled back. Once a verified artifact event append becomes ambiguous, the file is not silently deleted because the event itself may already be durable; PR #22 reconciles/finalizes that state.

`ARTIFACT_COLLECTED` includes a safe `collection_ordinal` so a consumer can correlate artifacts to the committed Test Spec collection order without exposing the guest filename.

Legacy result metadata contains only opaque `artifact_id`, byte size, the secret-safe `content_commitment`, and the protected flag. It does not contain the protected host path or raw guest SHA-256.

## Failure semantics

- oversize or policy-suppressed payloads create no artifact payload file;
- sanitizer failure creates no artifact payload file;
- mutable input buffers are snapshotted before sanitizer/storage code runs;
- a temporary write is not an Artifact until durable no-overwrite publication succeeds;
- short/zero-progress writes fail closed;
- post-publication verification failure removes the still-unregistered payload;
- forged or already-finalized collection reservations cannot register an artifact;
- free-form suppression reasons are rejected so metadata cannot become a plaintext leak channel;
- path/namespace identity loss fails closed;
- a persisted artifact whose canonical event append becomes ambiguous is retained for later reconciliation rather than fabricating absence.

## Deliberately deferred

PR #21 does not implement:

- evidence/artifact manifests;
- transactional `RUN_COMPLETED` publication;
- externally bound signatures, transparency logs, or WORM retention;
- encrypted external protected-artifact stores;
- trusted Markdown/HTML/JUnit rendering;
- Fleet artifact transport or central retention.

Those remain later ATES/Fleet layers, starting with transactional finalization in PR #22.
