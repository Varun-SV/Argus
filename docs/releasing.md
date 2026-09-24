# Argus release engineering

Argus uses GitHub Actions for CI, cross-platform packaging, release publication, provenance, and GitHub Pages deployment.

## Version authority

Production versions are derived from immutable Git tags with `setuptools-scm`. The source tree does not need a release bot to edit version declarations on protected `main`.

- a commit tagged `v1.2.3` builds Python/package metadata version `1.2.3`;
- non-tag development commits receive an SCM development version;
- `argus --version` reads installed distribution metadata;
- frozen applications copy that same distribution metadata into the bundle.

This design is compatible with the repository's protected-main rules: automatic releases create a tag on the already-merged commit, but do not push a follow-up release commit to `main`.

## What gets built

A production release builds the same tagged source tree on native GitHub-hosted runners.

| Platform | Architectures | Release formats |
|---|---|---|
| Windows | x64, ARM64 | portable ZIP, Inno Setup EXE, MSI |
| macOS | universal2 (x86_64 + arm64) | app ZIP, DMG |
| Linux | x86_64, ARM64 | AppImage, DEB, RPM, Arch `.pkg.tar.zst` |
| Python | platform-independent source/wheel | sdist, wheel |

The desktop application uses the existing `argus gui` implementation. A frozen CLI is included alongside desktop packages where appropriate.

## Pull-request validation

`CI` runs tests across supported operating systems and architectures plus package-integrity checks.

`Package preview` invokes the same reusable artifact builder used for real releases. Preview artifact names use a non-release numeric version solely so installer compilers such as MSI can validate the PR. Preview jobs never publish a GitHub Release or PyPI version.

GitHub CodeQL default setup, dependency review, and Dependabot provide additional automated maintenance/security coverage.

## Automatic release on merge

A PR merged into `main` creates a patch release by default.

Release labels:

- `bump:major` — increment major version;
- `bump:minor` — increment minor version;
- `bump:patch` — explicitly increment patch version;
- `release:none` — skip automatic release for that PR.

The automatic workflow:

1. checks out the exact merged commit with full tag history;
2. reuses an existing SemVer tag on that commit if a previous release attempt already created one;
3. otherwise computes the next version from the latest `vX.Y.Z` tag and the PR's bump label;
4. creates an annotated release tag on the merged commit without mutating protected `main`;
5. builds all platform artifacts from that exact tag;
6. verifies tagged Python distribution filenames match the release version;
7. publishes a GitHub Release with `SHA256SUMS` and build-provenance attestations;
8. publishes the Python distribution to PyPI when publishing credentials are configured;
9. refreshes GitHub Pages from the same release tag.

The tag step is idempotent: if publication fails after the tag was created, rerunning the workflow reuses that tag instead of incrementing the version again.

### README and hosted-page release status

The README uses GitHub's dynamic release badge and a stable `releases/latest` link, so it reflects the newest published release without an automated commit to protected `main`.

The hosted page queries GitHub's public `releases/latest` API at page load and updates its release badge dynamically. The release workflow also refreshes Pages from the tagged source. If the API is temporarily unavailable or rate-limited, the page falls back to a generic “Latest release” link.

`CHANGELOG.md` keeps an `Unreleased` section for curated project notes. GitHub release notes are generated from the immutable tag comparison; the release workflow does not rewrite the changelog on protected `main`.

## Rebuilding an existing tag

`Release existing tag` is triggered by a SemVer tag push and rebuilds from that exact immutable tag. `setuptools-scm` makes the Python metadata reproduce the tag version. If a release attempt fails after the tag exists, retry the original GitHub Actions run (or its failed jobs) rather than accepting an arbitrary manual source ref.

Published PyPI files are immutable. Both release paths use PyPI's skip-existing behavior so a retry can finish GitHub assets/Pages without moving the tag or republishing an existing file.

## PyPI authentication

Two modes are supported.

### Recommended: Trusted Publishing

Configure PyPI Trusted Publishers for the top-level release workflows and GitHub environment `pypi`, then set repository variable:

```text
PYPI_TRUSTED_PUBLISHING=true
```

Trusted Publishing uses short-lived OIDC credentials and avoids a long-lived PyPI token.

### Compatibility fallback

If Trusted Publishing is not enabled, the workflow looks for repository secret:

```text
PYPI_API_TOKEN
```

If neither authentication path is configured, GitHub binary releases can still be created and the workflow emits a warning instead of exposing or inventing credentials.

## Signing status

### Windows

The MSI and setup EXE are currently unsigned because no code-signing identity has been configured. Release checksums and GitHub provenance attestations still allow artifact verification. Authenticode should be added when a suitable certificate and protected signing mechanism are available.

### macOS

The universal2 application is currently ad-hoc signed by the packaging toolchain, not signed with an Apple Developer ID and not notarized. This is intentional because no Apple Developer signing identity is currently available.

When a Developer ID becomes available, add signing/notarization through protected GitHub secrets or an external signing service without making those credentials available to pull-request workflows.

## Supply-chain notes

- release build jobs have read-only repository permissions;
- publishing authority is isolated in top-level release jobs;
- release builders do not consume dependency caches populated by pull-request code;
- AppImage tooling/runtime are version- and checksum-pinned;
- release artifacts receive SHA-256 checksums and GitHub provenance attestations;
- PR code never receives release/PyPI credentials through the package-preview workflow;
- protected `main` is not bypassed by release automation.
