# Hyper-V Argus Capsules

Argus can execute a test inside a disposable Hyper-V virtual machine instead of on the interactive host desktop. The Capsule execution environment owns VM allocation, guest-agent readiness, network isolation, secure control authentication, test execution, optional failure retention, explicit file staging/collection, and teardown.

## Lifecycle

A normal run uses a per-session differencing VHDX and destroys the VM/session storage after teardown. With `retain_on_failure: true`, a failed/error/budget-exhausted run powers the VM off and preserves its registration, differencing disk, configuration, and `failure-capsule.json`. Guest RAM is **not** serialized; `Save-VM` is intentionally not used.

Target-launch failures after a valid guest session has been prepared may also be retained. Preparation failures (VM allocation, isolation setup, secure guest-agent readiness, transfer-workspace setup, staging-policy/configuration errors, or undeclared `stage://` references) remain infrastructure failures and roll back destructively because no valid application test state necessarily exists yet.

If retention itself fails, Argus fails safe: it does not retry destructive cleanup, preserves the registered VM/storage, and surfaces operator recovery coordinates through `failure_capsule_error`.

## PR6 isolation boundary

Production Capsule creation now routes through `SecureCapsuleExecutionEnvironment` and `IsolatedHyperVProvider`.

The order is intentional:

1. create the differencing VHDX and powered-off VM;
2. configure CPU/lifecycle settings;
3. disable and verify the Hyper-V **Guest Service Interface** file-copy integration service;
4. install Hyper-V virtual-switch ACLs;
5. only then start the VM;
6. attest the VM's reported guest IPv4 address;
7. connect over pinned HTTPS using the bootstrap bearer;
8. rotate to a cryptographically random per-session bearer;
9. expose the guest adapter to the test runner.

A failure in steps 1–8 is a preparation failure and rolls the disposable Capsule back. The application under test is never launched into a partially isolated environment.

## Secure guest-agent channel

The production control channel defaults to HTTPS. The reusable bootstrap token still comes only from `ARGUS_CAPSULE_GUEST_TOKEN`; project configuration cannot select an arbitrary host environment variable. The host also pins a dedicated guest CA/self-signed certificate via `guest_ca_cert`, while Hyper-V attests that the endpoint address belongs to the exact session VM.

After the pinned HTTPS connection is healthy, Argus generates a random session bearer and calls `/v1/auth/rotate`. The guest replaces the reusable bootstrap bearer with that session token, binds it to the Capsule session ID, and rejects reuse for a different file workspace. If the rotation response is lost, the host can probe the authenticated `/v1/health` endpoint with the proposed token and recover without falling back to the reusable credential.

The TLS certificate/CA file kept on the host is **public trust material**, not a secret. The corresponding TLS private key stays in the golden guest image. The active bearer remains secret. This is server-authenticated TLS plus bearer client authentication; PR6 does not claim mTLS.

Plain HTTP is rejected by default. `allow_insecure_http: true` is an explicit compatibility escape hatch for disposable development and does **not** meet the production isolation boundary.

### Golden-image guest setup

Provision a dedicated certificate/key and the one-time bootstrap token into the golden image, then start the secure guest agent in the interactive test-user session. For example:

```powershell
python -m argus.capsule.secure_guest_agent `
  --host 0.0.0.0 `
  --port 8765 `
  --token-file C:\ProgramData\Argus\guest-token.once `
  --tls-cert C:\ProgramData\Argus\tls\guest-cert.pem `
  --tls-key C:\ProgramData\Argus\tls\guest-key.pem
```

The host must trust only the dedicated Argus guest CA/self-signed certificate used for that service, for example:

```yaml
execution:
  environment: capsule
  capsule:
    provider: hyperv
    image: C:\Argus\images\windows-11-clean.vhdx
    switch_name: Argus Internal
    guest_transport: https
    guest_ca_cert: C:\Argus\certs\argus-guest-ca.pem
    rotate_session_token: true
```

The bootstrap token file is consumed/deleted from each session's differencing disk during guest-agent startup. It remains present only in the immutable/golden source image used to create fresh sessions.

## Network isolation

PR6 installs Hyper-V extended ACLs **before the VM boots**. The default is:

```yaml
network_mode: host_only
egress_allowlist: []
allow_dhcp: true
```

The policy permits:

- management host → guest TCP control port, statefully;
- DHCP bootstrap when `allow_dhcp: true`;
- explicit outbound IPv4 CIDRs only when `network_mode: allowlist`.

Then low-priority catch-all rules deny every other inbound and outbound packet. External Hyper-V switches remain rejected even though the control plane is encrypted; Argus intentionally prefers an Internal switch plus narrowly declared egress over broad physical-network attachment.

Example target-network access:

```yaml
network_mode: allowlist
egress_allowlist:
  - 10.20.30.0/24
  - 203.0.113.8/32
```

Allowlist entries are CIDRs, not DNS names. If a tested application needs DNS or another supporting service, the required resolver/service address must itself be explicitly authorized. Host-side LLM provider/API calls do not require guest egress because those calls are made by Argus on the host.

If the Internal switch has no usable management-OS IPv4 address, or if ACL installation fails, VM creation fails closed and rolls back before `Start-VM`.

## Host/guest file-copy isolation

PR6 disables and re-verifies Hyper-V **Guest Service Interface** before boot. This removes Hyper-V host-initiated guest file-copy as an alternate transfer path. If Argus cannot identify the service or Hyper-V reports it still enabled, Capsule creation fails closed.

PR5 staging/collection is therefore the sanctioned host↔guest file path:

- `staging:` authorizes project files to enter the Capsule;
- `stage://...` may launch only a file successfully committed during the current session;
- `collect:` authorizes only named workspace-relative artifacts to leave the Capsule;
- no shared folders or clipboard transfer are used;
- transfers are bounded to 256 KiB chunks, 64 MiB per file, and 256 MiB per declared/session set.

### Host → guest staging

Host sources are normalized as project-relative paths, then opened through a race-safe rooted handle before any upload begins. All declared sources stay open through the staging transaction, so replacing a source pathname with a symlink/junction or another file after authorization cannot widen what Argus uploads. The host hashes and streams the same opened object; the guest verifies the declared size/SHA-256 before atomically committing it.

Guest destinations use Windows path semantics because the current Hyper-V guest is Windows: case-only aliases are treated as duplicates, and trailing-dot/space or reserved-device spellings are rejected.

### Guest → host collection

Collection occurs after non-close teardown preparation (so a flush/export step can finish producing output) and immediately before explicit `close`; exception/no-close paths collect before implicit final cleanup.

Guest collection opens the declared workspace artifact through a race-bound handle. On the supported Windows guest, Argus holds an exclusive `LockFileEx` byte-range lock while taking the snapshot, preventing ordinary concurrent writes during the copy. The snapshot is bounded to the preflight size and aggregate quota and is verified with a second bounded digest pass before acceptance. POSIX CI/future-provider behavior uses no-follow rooted handles plus advisory locking and the second verification pass; this is not claimed as equivalent to the Windows mandatory write barrier.

Snapshots are kept in bounded guest-agent memory rather than persisted to the Capsule VHDX. Because Failure Capsules do not persist guest RAM, snapshot preflight does not inflate retained disk images.

On the host, the `.argus/runs/<run>/artifacts` directory tree and all declared artifact parent directories are pinned for the complete collection transaction. On the supported Windows host those handles intentionally omit delete/rename sharing, preventing a same-user process from replacing a verified run/artifact directory with a junction while bytes are being written. A pre-existing redirect is rejected before artifact data is written.

Collection is transactional across the declared set: if a later artifact fails, already committed artifacts from that set are removed so disk state cannot disagree with the result manifest. A collection failure is recorded before environment close so `retain_on_failure` can preserve the Capsule.

## Validation boundary

Hosted GitHub Actions can exercise TLS/auth policy, bearer rotation, network-policy generation/order, rollback behavior, integration-service policy, protocol/lifecycle code, path containment, Windows/POSIX handle behavior, quotas, and simulated provider/client paths. Hosted CI still does **not** provide nested Hyper-V, so a manual/on-prem Hyper-V smoke test remains necessary before claiming hardware-backed host↔Windows-guest isolation validation.

PR6 also does not automate certificate issuance/rotation for the golden image and does not use Hyper-V sockets. Those can be future hardening layers without weakening the current pinned-HTTPS, per-session-bearer, default-deny network boundary.
