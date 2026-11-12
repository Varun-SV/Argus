# Installer release publication

Approved scope: prepare a new PR for the next patch release, expected to be
v0.1.8 after v0.1.7. Leave it open; merging triggers the existing release workflow.

## Intent and constraints

Ship the current Python desktop application through the existing Windows
x64/ARM64 ZIP, EXE and MSI, macOS universal2 ZIP and DMG, Linux x86_64/ARM64
AppImage, DEB, RPM and Arch packages, and Python sdist/wheel. Preserve tag-based
version authority, checksums, attestations, existing signing status and PyPI
configuration. The Rust preview remains separate.

## Approaches

The user chose a patch release (v0.1.8 expected) rather than the proposed minor
milestone v0.2.0. Repair the existing native artifact builder/publication path;
replacing working platform builders would add risk without addressing the failure.

## Design

The v0.1.7 job logs show all native builds succeeded. Publication failed because
`gh release` lacked repository context. The merged Linux artifacts also contain
a nested `packages/` directory, which the current checksum/upload globs mishandle.

Both release entry points will explicitly select the repository. A shared staging
script will flatten downloaded assets, validate the complete versioned package
set, and generate checksums before attestation/upload. It will reject missing,
duplicate or unexpected files rather than silently publishing a partial release.
Regression tests will execute staging and publication with controlled local
artifacts and a fake GitHub CLI boundary. Real native builds remain verified by
the PR's existing Package preview workflow.

The implementation may choose script structure and test fixtures. It must not
merge the PR, move existing tags, change signing credentials, or publish a release
from the unmerged branch.
