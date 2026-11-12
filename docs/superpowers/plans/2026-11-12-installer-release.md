# Installer Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair installer publication for the next patch release, expected v0.1.8.

**Architecture:** Keep the working native builders and tag-based version authority.
Stage a complete flat payload before checksumming, attesting and publishing. Use
explicit GitHub repository selection in both publication paths.

**Tech Stack:** GitHub Actions, Python standard library, pytest, Bash, GitHub CLI.

**Spec:** `docs/superpowers/specs/2026-11-12-installer-release-design.md`

## Global Constraints

- Preserve every existing platform/architecture/format and signing status.
- Python desktop app remains the shipped product; Rust remains a preview.
- Leave the new PR open; merging triggers the existing patch release mechanism.
- Existing tags are immutable; expected next tag is v0.1.8, subject to intervening releases.

## Review Focus

- Nested Linux packages must reach the flat upload directory and checksum manifest.
- Missing/duplicate/wrong-version payloads must fail before publication.
- Publication must work from a directory without `.git` metadata.
- Both new-release and existing-release upload paths must select the repository.
- The retry path must preserve immutable tags and include every installer.

### Task 1: Stage complete release payloads

**Files:** create `.github/scripts/prepare_release_assets.py`,
`tests/test_release_publication.py`.

**Interfaces:** CLI `prepare_release_assets.py SOURCE DESTINATION --version X.Y.Z`
produces 18 flat package files plus `SHA256SUMS`; invalid sets exit nonzero.

- [ ] Write fixtures for all 18 packages, nested Linux paths, missing packages,
  duplicate basenames and incorrect versions; assert actual copied bytes and hashes.
- [ ] Run `python -m pytest tests/test_release_publication.py -q`; observe failure
  because staging does not exist.
- [ ] Implement staging with stdlib validation, copying and SHA-256 hashing.
- [ ] Run the same tests; expect all staging cases to pass.
- [ ] Commit staging and tests.

### Task 2: Wire both publishing paths and document release

**Files:** modify `.github/workflows/release-on-merge.yml`,
`.github/workflows/release.yml`, `docs/releasing.md`, `CHANGELOG.md`, and extend
`tests/test_release_publication.py`.

**Interfaces:** Both jobs check out their immutable tag, invoke Task 1's CLI,
attest/upload its destination and use explicit repository selection for `gh`.

- [ ] Execute each workflow's real publish shell against a fake external `gh`
  boundary from a directory with no git checkout; cover create and retry branches.
- [ ] Run those cases and observe the old repository selection failure.
- [ ] Wire checkout, staging, attestation and explicit repository selection in both jobs.
- [ ] Document complete payload validation and retry behavior; add changelog fix.
- [ ] Run focused regressions, full pytest, compile/lint and Python build checks;
  report environmental/baseline failures accurately.
- [ ] Commit, obtain a fresh whole-branch review, push the new branch, open a PR
  and inspect native preview/CI results. Do not merge or publish from this branch.
