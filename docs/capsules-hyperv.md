# Hyper-V Argus Capsules

Argus can execute a test inside a disposable Hyper-V virtual machine instead of on the interactive host desktop. The Capsule execution environment owns VM allocation, guest-agent readiness, network isolation, secure control authentication, test execution, optional failure retention, explicit file staging/collection, and teardown.

## Lifecycle

A normal run uses a per-session differencing VHDX and destroys the VM/session storage after teardown. With `retain_on_failure: true`, a failed/error/budget-exhausted run powers the VM off and preserves its registration, differencing disk, configuration, and `failure-capsule.json`. Guest RAM is **not** serialized; `Save-VM` is intentionally not used.

Target-launch failures after a valid guest session has been prepared may also be retained. Preparation failures (VM allocation, isolation setup, secure guest-agent readiness, transfer-workspace setup, staging-policy/configuration errors, or undeclared `stage://` references) remain infrastructure failures and roll back destructively because no valid application test state necessarily exists yet.

If retention itself fails, Argus fails safe: it does not retry destructive cleanup, preserves the registered VM/storage, and surfaces operator recovery coordinates through `failure_capsule_error`.

## PR6 isolation boundary

Production Capsule creation routes through `SecureCapsuleExecutionEnvironment` and `IsolatedHyperVProvider`.

The order is intentional:

1. create the differencing VHDX and powered-off VM;
2. configure CPU/lifecycle settings;
3. disable and verify Hyper-V **Guest Service Interface** file copy;
4. install Hyper-V virtual-switch ACLs;
5. only then start the VM;
6. use Hyper-V KVP long enough to attest the VM's reported IPv4 address;
7. disable and verify **Key-Value Pair Exchange** so custom KVP data cannot become a second transfer channel;
8. connect over pinned HTTPS using the bootstrap bearer;
9. rotate to a cryptographically random per-session bearer;
10. expose the guest adapter to the test runner.

Inside a production Windows golden image, `vmicvmsession` (Hyper-V PowerShell Direct) must also be disabled. The secure guest agent attests that service policy before accepting a non-loopback binding, because PowerShell Direct runs over VMbus and is not constrained by virtual-switch ACLs.

A failure anywhere before step 10 is a preparation failure and rolls the disposable Capsule back. The application under test is never launched into a partially isolated environment.

## Secure guest-agent channel

The production control channel defaults to HTTPS. The reusable bootstrap token comes only from `ARGUS_CAPSULE_GUEST_TOKEN`; project configuration cannot select an arbitrary host environment variable. The host pins a dedicated guest CA/self-signed certificate via `guest_ca_cert`, while Hyper-V attests that the endpoint address belongs to the exact session VM.

After the HTTPS connection is healthy, Argus generates a random session bearer and calls `/v1/auth/rotate`. The guest replaces the reusable bootstrap bearer with that session token, binds it to the Capsule session ID, and rejects reuse for a different file workspace. Secure Capsule creation refuses `rotate_session_token: false`.

If the rotation response is lost after the guest commits the new bearer, the host probes the authenticated `/v1/health` endpoint with the proposed token and recovers only when the returned non-secret session binding matches.

The TLS certificate/CA file kept on the host is **public trust material**, not a secret. The corresponding TLS private key is provisioned in the golden guest image. Each session loads that key into the TLS context and then deletes the key file from its differencing disk before serving the target; the immutable golden parent remains unchanged. The active bearer remains secret. This is server-authenticated TLS plus bearer client authentication; PR6 does not claim mTLS.

Plain HTTP is rejected by default. `allow_insecure_http: true` is an explicit compatibility escape hatch for disposable development and does **not** meet the production isolation boundary.

### Golden-image guest setup

Before capturing the Windows golden image, disable PowerShell Direct:

```powershell
Stop-Service -Name vmicvmsession -ErrorAction SilentlyContinue
Set-Service -Name vmicvmsession -StartupType Disabled
```

Then provision the dedicated certificate/private key and one-time bootstrap token and configure the secure guest agent to start in the interactive test-user session. For example:

```powershell
python -m argus.capsule.secure_guest_agent `
  --host 0.0.0.0 `
  --port 8765 `
  --token-file C:\ProgramData\Argus\guest-token.once `
  --tls-cert C:\ProgramData\Argus\tls\guest-cert.pem `
  --tls-key C:\ProgramData\Argus\tls\guest-key.pem
```

The host trusts only the dedicated Argus guest CA/self-signed certificate used for that service:

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
    disable_guest_file_copy: true
```

Both the bootstrap-token file and TLS private-key file are consumed/deleted from the session differencing disk during secure guest-agent startup. They remain available only through the immutable/golden parent for fresh sessions. The target should run as a non-administrative test user so it cannot re-enable disabled guest services.

## Secure Failure Capsules are forensic-only

A secure Failure Capsule preserves the registered VM, differencing disk, configuration, and failure metadata, but **does not persist or restore guest control credentials for a later boot**.

This is deliberate. Once an application under test has run, Argus cannot assume that closing the tracked root process removes every target-controlled child, scheduled task, startup entry, or same-user persistence mechanism. Writing a fresh bearer or TLS private key back into documented guest paths would therefore hand future control authority to potentially target-controlled code.

The secure retention sequence remains:

```text
run failure
  ↓
record failure reason
  ↓
Hyper-V TurnOff (no Save-VM / no RAM serialization)
  ↓
retain VM registration + differencing disk + failure-capsule.json
```

No `/v1/recovery/arm` endpoint exists, no `recovery-control.json` is created, and `FailureCapsule` contains no recovery credential field. The live rotated bearer, bootstrap token, and session TLS private-key file are not made durable.

Consequently a retained secure Capsule is intended for offline/forensic inspection of its disk and VM configuration. Re-entering it through Argus would require a future **host-bound recovery mechanism** with a control identity unavailable to the application-under-test (for example a dedicated privileged control service or an appropriately designed Hyper-V socket channel). PR6 intentionally does not fake restartability by putting secrets back into a potentially compromised guest.

This also means a DHCP address changing on a hypothetical later reboot is not part of the supported Failure Capsule contract: PR6 does not advertise secure remote restart of retained Capsules.

## Network isolation

PR6 installs Hyper-V extended ACLs **before the VM boots**. The default is:

```yaml
network_mode: host_only
egress_allowlist: []
allow_dhcp: true
```

The policy permits:

- management host → guest TCP control port, using a **stateful inbound** Hyper-V extended ACL so only the matching outbound return flow is dynamically permitted;
- DHCP bootstrap when `allow_dhcp: true`;
- explicit outbound IPv4 CIDRs only when `network_mode: allowlist`.

Then low-priority catch-all rules deny every other inbound and outbound packet. External Hyper-V switches remain rejected even though the control plane is encrypted; Argus intentionally prefers an Internal switch plus narrowly declared egress over broad physical-network attachment.

The management-host IPv4 address is resolved from the Hyper-V **ManagementOS virtual network adapter attached to the named switch**, not by guessing the default `vEthernet (<switch>)` Windows adapter display name. Renaming the Windows network adapter therefore does not invalidate Capsule isolation setup.

Example target-network access:

```yaml
network_mode: allowlist
egress_allowlist:
  - 10.20.30.0/24
  - 203.0.113.8/32
```

Allowlist entries are CIDRs, not DNS names. If a tested application needs DNS or another supporting service, the required resolver/service address must itself be explicitly authorized. Host-side LLM provider/API calls do not require guest egress because those calls are made by Argus on the host.

If the Internal switch has no usable management-OS IPv4 address, or if ACL installation fails, VM creation fails closed and rolls back before `Start-VM`.

## Hyper-V integration-service isolation

PR6 treats host/guest integration mechanisms as part of the authority boundary rather than assuming the virtual switch is the only channel.

- **Guest Service Interface** is disabled and re-verified before boot. Secure Hyper-V creation rejects `disable_guest_file_copy: false`.
- **Key-Value Pair Exchange (KVP)** is used only for guest-address discovery, then disabled and re-verified before the provider returns the Capsule.
- **PowerShell Direct (`vmicvmsession`)** must be disabled in the Windows golden image and is attested by the secure guest agent before non-loopback startup.

This removes `Copy-VMFile`, custom KVP strings, and network-bypassing PowerShell Direct as alternate data/control paths for the normal Argus Capsule lifecycle.

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

Collection occurs after non-close teardown preparation and immediately before explicit `close`; exception/no-close paths collect before implicit final cleanup.

Guest collection opens the declared workspace artifact through a race-bound handle. On the supported Windows guest, Argus holds an exclusive `LockFileEx` byte-range lock while taking the snapshot, preventing ordinary concurrent writes during the copy. The snapshot is bounded to the preflight size and aggregate quota and is verified with a second bounded digest pass before acceptance. POSIX CI/future-provider behavior uses no-follow rooted handles plus advisory locking and the second verification pass; this is not claimed as equivalent to the Windows mandatory write barrier.

Snapshots are kept in bounded guest-agent memory rather than persisted to the Capsule VHDX. Because Failure Capsules do not persist guest RAM, snapshot preflight does not inflate retained disk images.

On the host, the `.argus/runs/<run>/artifacts` directory tree and all declared artifact parent directories are pinned for the complete collection transaction. On the supported Windows host those handles intentionally omit delete/rename sharing, preventing a same-user process from replacing a verified run/artifact directory with a junction while bytes are being written. A pre-existing redirect is rejected before artifact data is written.

Collection is transactional across the declared set: if a later artifact fails, already committed artifacts from that set are removed so disk state cannot disagree with the result manifest. A collection failure is recorded before environment close so `retain_on_failure` can preserve the Capsule.

## Validation boundary

Hosted GitHub Actions exercises TLS/auth policy, bearer rotation and lost-response recovery, credential-file consumption, network-policy generation/order, management-vNIC resolution, forensic-only retention, rollback behavior, integration-service policy, protocol/lifecycle code, path containment, Windows/POSIX handle behavior, quotas, and simulated provider/client paths. Hosted CI still does **not** provide nested Hyper-V, so a manual/on-prem Hyper-V smoke test remains necessary before claiming hardware-backed host↔Windows-guest isolation validation.

PR6 does not automate certificate issuance/rotation for golden images and does not use Hyper-V sockets or mTLS. Those can be future hardening layers without weakening the current pinned-HTTPS, per-session-bearer, default-deny network boundary.
