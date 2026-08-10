# Hyper-V Argus Capsules

Argus can execute a test inside a disposable Hyper-V virtual machine instead of on the interactive host desktop. The Capsule execution environment owns VM allocation, guest-agent readiness, test execution, optional failure retention, explicit file staging/collection, and teardown.

## Lifecycle

A normal run uses a per-session differencing VHDX and destroys the VM/session storage after teardown. With `retain_on_failure: true`, a failed/error/budget-exhausted run powers the VM off and preserves its registration, differencing disk, configuration, and `failure-capsule.json`. Guest RAM is **not** serialized; `Save-VM` is intentionally not used while the guest control credential can exist in process memory.

Target-launch failures after a valid guest session has been prepared may also be retained. Preparation failures (VM allocation, guest-agent readiness, transfer-workspace setup, staging-policy/configuration errors, or undeclared `stage://` references) remain infrastructure failures and roll back destructively because no valid application test state necessarily exists yet.

If retention itself fails, Argus fails safe: it does not retry destructive cleanup, preserves the registered VM/storage, and surfaces operator recovery coordinates through `failure_capsule_error`.

## Staging and collection

PR5 adds an explicit deterministic file boundary:

- `staging:` authorizes project files to enter the Capsule.
- `stage://...` may launch only a file successfully committed during the current session.
- `collect:` authorizes only named workspace-relative artifacts to leave the Capsule.
- No shared folders or clipboard transfer are used.
- Transfers are bounded to 256 KiB chunks, 64 MiB per file, and 256 MiB per declared/session set.

### Host → guest staging

Host sources are normalized as project-relative paths, then opened through a race-safe rooted handle before any upload begins. All declared sources stay open through the staging transaction, so replacing a source pathname with a symlink/junction or another file after authorization cannot widen what Argus uploads. The host hashes and streams the same opened object; the guest verifies the declared size/SHA-256 before atomically committing it.

Guest destinations use Windows path semantics because the current Hyper-V guest is Windows: case-only aliases are treated as duplicates, and trailing-dot/space or reserved-device spellings are rejected.

### Guest → host collection

Collection occurs after non-close teardown preparation (so a flush/export step can finish producing output) and immediately before explicit `close`; exception/no-close paths collect before implicit final cleanup.

Guest collection opens the declared workspace artifact through a race-bound handle. On the supported Windows guest, Argus holds an exclusive `LockFileEx` byte-range lock while taking the snapshot, preventing ordinary concurrent writes during the copy. The snapshot is bounded to the preflight size and aggregate quota and is verified with a second bounded digest pass before acceptance. POSIX CI/future-provider behavior uses no-follow rooted handles plus advisory locking and the second verification pass; this is not claimed as equivalent to the Windows mandatory write barrier.

Snapshots are kept in bounded guest-agent memory rather than persisted to the Capsule VHDX. Because Failure Capsules do not persist guest RAM, snapshot preflight does not inflate retained disk images.

On the host, the `.argus/runs/<run>/artifacts` directory tree and all declared artifact parent directories are pinned for the complete collection transaction. On the supported Windows host those handles intentionally omit delete/rename sharing, preventing a same-user process from replacing a verified run/artifact directory with a junction while bytes are being written. A pre-existing redirect is rejected before artifact data is written.

Collection is transactional across the declared set: if a later artifact fails, already committed artifacts from that set are removed so disk state cannot disagree with the result manifest. A collection failure is recorded before environment close so `retain_on_failure` can preserve the Capsule.

## Guest-agent channel

The current guest-agent channel is bearer-token authenticated HTTP on a Hyper-V Internal switch. The token is sourced from `ARGUS_CAPSULE_GUEST_TOKEN`; configuration cannot nominate an arbitrary host environment variable. The Hyper-V provider attests that the configured/discovered guest address belongs to the exact session VM before creating the guest client.

This channel is authenticated but **not confidential**. SHA-256 checks provide byte-consistency verification, not cryptographic transport integrity against an active observer who can access the bearer-token traffic. Do not treat PR5 as a confidential secret-transfer channel. Per-session host-bound credentials and a confidential transport (for example Hyper-V sockets or TLS/mTLS) remain isolation work for a later PR.

## Networking

The current provider accepts only Hyper-V Internal switches. External switches are rejected unconditionally, and Private switches are rejected because the host cannot reach the guest agent through them. This is an MVP confinement rule, not a complete egress policy; default-deny guest egress and explicit allowlists remain future isolation work.

## Validation boundary

Hosted GitHub Actions exercises protocol, policy, lifecycle, path containment, Windows/POSIX handle behavior, quota enforcement, retention integration, and simulated provider/client paths. Hosted CI does **not** provide nested Hyper-V, so a manual/on-prem Hyper-V smoke test remains necessary before claiming hardware-backed host↔Windows-guest validation.
