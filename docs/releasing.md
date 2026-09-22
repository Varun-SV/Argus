# Argus release engineering

Argus uses GitHub Actions for CI, cross-platform packaging, release publication, provenance, and GitHub Pages deployment.

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

`Package preview` invokes the same reusable artifact builder used for real releases. Its artifacts are temporary workflow artifacts; it does not publish a GitHub Release or PyPI version.

CodeQL, dependency review, and Dependabot provide additional automated maintenance/security coverage.

## Automatic release on merge

A PR merged into `main` creates a patch release by default.

Release labels:

- `bump:major` — increment major version;
- `bump:minor` — increment minor version;
- `bump:patch` — explicitly increment patch version;
- `release:none` — skip automatic release for that PR.

The automatic workflow:

1. verifies that it is versioning the exact merged `main` head;
2. updates both `pyproject.toml` and `argus.__version__`;
3. moves the current `CHANGELOG.md` Unreleased content into the new version;
4. updates the release marker in `README.md` and `index.html`;
5. commits the release metadata and creates an annotated SemVer tag;
6. builds all platform artifacts from that exact release commit;
7. publishes a GitHub Release with `SHA256SUMS` and build-provenance attestations;
8. publishes the Python distribution to PyPI when publishing credentials are configured;
9. explicitly deploys the matching hosted page.

The Pages deployment is called explicitly because commits/tags pushed with the workflow's `GITHUB_TOKEN` are intentionally not relied upon to trigger a second workflow.

If `main` advances between the PR merge event and release preparation, the workflow fails closed instead of versioning an unintended source tree. Re-run/release the intended tagged commit after resolving the race.

## Rebuilding an existing tag

`Release existing tag` can be triggered by a SemVer tag push or manually with an existing `vX.Y.Z` tag. It validates that the tag version matches both version declarations before rebuilding.

Published PyPI files are immutable. The tag rebuild path therefore uses PyPI's skip-existing behavior and refreshes downloadable GitHub assets without changing the tag.

## PyPI authentication

Two modes are supported:

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
- AppImage tooling/runtime are version- and checksum-pinned;
- release artifacts receive SHA-256 checksums and GitHub provenance attestations;
- PR code never receives release/PyPI credentials through the package-preview workflow.
