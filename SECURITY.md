# Security Policy

Argus can drive applications, execute tests, manage isolated Capsules, and coordinate remote Fleet execution. Security reports that affect those trust boundaries should be handled carefully.

## Supported versions

Security fixes target the latest released Argus version and the current `main` branch. Older releases may not receive backports while the project remains pre-1.0.

## Reporting a vulnerability

Prefer **GitHub Private Vulnerability Reporting** for this repository when the option is available on the repository's **Security** tab.

Please include:

- the affected Argus version or commit;
- the platform and execution mode involved;
- the trust boundary that is crossed;
- reproduction steps or a minimal proof of concept;
- the realistic impact;
- any suggested mitigation, if known.

Do **not** publish working exploit details, credentials, private keys, tokens, customer data, or sensitive infrastructure information in a public issue. If private vulnerability reporting is unavailable, open a public issue that contains only a non-sensitive summary and asks the maintainer for a private reporting channel.

## Scope

Security-sensitive areas include, but are not limited to:

- local input/action policy and target confinement;
- Capsule isolation, image verification, guest control, and file transfer;
- Fleet enrollment, node identity, placement, fencing, and remote execution authority;
- ATES integrity, provenance, protected artifacts, manifests, and approvals;
- credential handling and provider API keys;
- release packaging, artifact provenance, and update/distribution workflows.

A crash, ordinary functional bug, or unsupported configuration is not automatically a security vulnerability unless it crosses or weakens a trust boundary.

## Release authenticity

Official release artifacts are published through GitHub Actions. Releases include a `SHA256SUMS` manifest and GitHub build-provenance attestations when the release workflow succeeds.

Windows packages are currently **not Authenticode-signed**, and the macOS application is currently **not Developer ID signed or notarized**. Those limitations are documented rather than hidden; signing can be added when project signing identities are available.
