# Multi-OS Argus Capsules

PR7 extends the Capsule provider boundary beyond Windows/Hyper-V while keeping the security and transfer invariants established by PR5 and PR6.

## Provider matrix

| Provider | Host | Guest | Isolation | Egress allowlist | Failure retention |
|---|---|---|---|---|---|
| `hyperv` | Windows | Windows | Hyper-V extended ACL + integration-service policy | Yes | Forensic disk/config |
| `libvirt` | Linux | Linux | isolated libvirt network + per-session nwfilter | No (fail closed) | Forensic disk/config |
| `auto` | Windows/Linux | same family as host | selects the matching provider | provider-specific | provider-specific |

For backwards compatibility, omitting `provider` still means `hyperv`. Use `provider: auto` when the same project configuration should choose Hyper-V on Windows and libvirt/QEMU on Linux.

macOS/Apple Virtualization is intentionally **not** implemented in this PR. Unsupported host/provider/guest combinations fail closed rather than silently falling back to local execution.

## Linux provider prerequisites

The first Linux Capsule provider targets local KVM/QEMU managed through libvirt:

- Linux host with hardware virtualization/KVM available;
- `virsh` and `qemu-img` installed;
- access to the local `qemu:///system` libvirt connection;
- a Linux qcow2 or raw golden image;
- a system-accessible Capsule storage root usable by both the Argus host process and the system QEMU account;
- the secure Argus guest agent configured to start automatically in the test-user session;
- a dedicated guest TLS certificate/private key and one-time bootstrap bearer, as with the Windows secure guest-agent design.

PR7 does not use the Python `libvirt` binding. Host commands are argv-based `virsh`/`qemu-img` invocations and are never composed through a shell.

## Linux configuration

```yaml
execution:
  environment: capsule
  capsule:
    provider: libvirt
    guest_os: linux
    image: /var/lib/libvirt/images/ubuntu-24.04.qcow2
    # Optional. If omitted, libvirt uses:
    # /var/lib/libvirt/images/argus-capsules
    vm_root: /var/lib/libvirt/images/argus-capsules

    guest_transport: https
    guest_ca_cert: /etc/argus/certs/linux-capsule-ca.pem
    guest_port: 8765
    rotate_session_token: true

    network_mode: host_only
    allow_dhcp: true

    libvirt_uri: qemu:///system
    # Optional RFC1918 allocation pool. Argus allocates one free /24 per session.
    # A /24 supports one concurrent Capsule; use a larger pool for concurrency.
    # libvirt_network_cidr: 10.250.64.0/20
    # Optional architecture/machine pins.
    # libvirt_arch: x86_64
    # libvirt_machine: pc-q35-9.0
    # For aarch64, Argus defaults the machine to virt and requests EFI firmware.
```

`guest_os`, `libvirt_uri`, `libvirt_network_cidr`, `libvirt_arch`, and `libvirt_machine` are propagated through the normal `.argus/config.yaml` → `load_config()` → execution-environment path. Matching `ARGUS_CAPSULE_GUEST_OS`, `ARGUS_CAPSULE_LIBVIRT_URI`, `ARGUS_CAPSULE_LIBVIRT_NETWORK_CIDR`, `ARGUS_CAPSULE_LIBVIRT_ARCH`, and `ARGUS_CAPSULE_LIBVIRT_MACHINE` environment overrides are also supported.

The bootstrap bearer still comes from `ARGUS_CAPSULE_GUEST_TOKEN`; it should not be stored in project YAML.

### System libvirt storage

`qemu:///system` normally starts QEMU under a dedicated service account such as `qemu` or `libvirt-qemu`. A user-home default like `~/.argus/capsules` is therefore unsafe as a default because home-directory permissions commonly prevent that account from traversing to the overlay.

When `vm_root` is omitted, PR7 uses:

```text
/var/lib/libvirt/images/argus-capsules
```

This keeps Capsule overlays under the system libvirt image hierarchy. The host must provision that directory so the Argus invoking process can create session files and the system QEMU process can traverse/read/write the disk through the host's libvirt ownership, DAC/ACL, and MAC policy. If Argus cannot create the root/session directory it fails before defining a domain and reports a setup error rather than falling back to the user's home directory.

A custom `vm_root` is allowed for sites that intentionally provision equivalent access. In particular, do not point it beneath a mode-0700 user home unless the system QEMU identity has an explicit safe access path.

## Disk lifecycle

Argus first asks `qemu-img info --output=json` for the golden image format and accepts `qcow2` or `raw`. Every session then creates:

```text
golden.qcow2 / golden.raw       (read-only authority)
           ↓ backing file
session.qcow2                   (all guest writes)
```

The session overlay, provider XML, and retained forensic metadata live beneath `vm_root/<session-id>`.

Before allocating that session directory, PR7 validates the requested RFC1918 network pool, guest address, architecture, golden-image format, current libvirt subnet occupancy, and provider resource names. Invalid configuration is therefore retryable with the same session id and cannot leave an empty session directory that blocks the next attempt.

The golden image is never attached as the writable domain disk. Normal teardown undefines the libvirt domain/network filter, destroys the transient network, and removes the session directory. Failure retention powers the domain off, destroys the transient network, and preserves the defined domain, qcow2 overlay, nwfilter definition, XML files, and `failure-capsule.json` for forensic inspection.

As with secure Hyper-V retention, no new restart bearer or TLS private key is persisted merely to make a failed guest remotely restartable.

### aarch64 firmware

When `libvirt_arch: aarch64` is selected, PR7 generates a libvirt domain with EFI firmware autoselection (`<os firmware="efi">`). If no machine is explicitly configured, the provider also selects the QEMU ARM `virt` machine. This avoids pretending that an ARM guest can fall back to PC-style firmware and avoids hard-coding distribution-specific AAVMF/UEFI loader paths into Argus configuration.

The host still needs a libvirt/QEMU installation whose domain capabilities provide a suitable EFI firmware implementation for the requested architecture. If firmware cannot be selected, libvirt fails the domain definition/start rather than Argus silently booting with a weaker or different architecture contract.

## Provider-resource ownership and rollback

Libvirt resource names are derived from a strong hash of the complete Capsule session id rather than a short shared prefix. Before storage allocation, Argus checks that the generated domain, transient network, and nwfilter names do not already exist. A collision is a hard failure; Argus never redefines the existing object.

Creation rollback additionally records ownership incrementally. A filter is considered owned only after `nwfilter-define` succeeds, a network only after `net-create` succeeds, and a domain only after `define` succeeds. If a later step fails, rollback removes only the objects created by that attempt. Pre-existing or concurrently appearing resources are never selected merely because their names match.

## Network boundary

PR7 creates one transient libvirt virtual network per Capsule. The network XML contains **no `<forward>` element**, which makes it isolated from the physical LAN. Each session receives its own non-overlapping `/24`; inside that `/24`, a fixed DHCP lease uses `.1` for the host bridge and `.2` for the guest:

```text
Linux host
   │
   │ host bridge address (.1)
   ▼
per-session isolated libvirt /24
   │
   ├─ ARP
   ├─ DHCP only
   └─ Argus HTTPS control only
          │
          ▼
      Linux guest (.2)
```

`libvirt_network_cidr` is an **allocation pool**, not one shared per-project guest subnet. The pool must be canonical IPv4 and wholly contained in RFC1918 address space (`10.0.0.0/8`, `172.16.0.0/12`, or `192.168.0.0/16`). Loopback, unspecified, link-local, multicast, carrier-grade NAT, reserved, and other special-use ranges are rejected even when Python's broad `is_private` property would classify some of them as non-global.

Argus divides the pool into `/24` session networks. A configured `/24` therefore allows exactly one concurrent Capsule; a `/23` provides two slots, a `/20` provides sixteen, and the default `10.240.0.0/12` provides 4096. Candidate selection starts at a deterministic session-hash position and probes the rest of the pool if that slot is occupied.

Before allocation, Argus enumerates every defined libvirt network with `virsh net-list --all --name` and reads each network's XML with `virsh net-dumpxml`. Any candidate `/24` that overlaps an existing IPv4 libvirt network is skipped. The chosen subnet is checked again immediately before provider resources are defined, closing the ordinary race window created while the overlay/XML are prepared. A full pool fails closed rather than reusing an endpoint.

A per-session libvirt `nwfilter` is attached directly to the guest interface. It allows:

1. ARP in both directions so the isolated bridge can resolve peers;
2. DHCP client/server UDP traffic;
3. host → guest TCP on the configured Argus control port (`NEW,ESTABLISHED`);
4. only the corresponding established guest → host TCP response flow;
5. drops everything else in both directions.

No VNC or SPICE graphics channel is defined by PR7, avoiding a second host→guest control surface.

### Egress

`network_mode: allowlist` and `egress_allowlist` are **not yet supported by the libvirt provider**. Requesting either fails before VM allocation.

This is deliberate: an isolated network plus nwfilter has a clear default-deny contract. PR7 does not approximate Hyper-V's CIDR egress policy with a weaker NAT configuration.

## Guest address attestation

The allocated `/24` gives the guest a fixed DHCP reservation at `.2`. After the domain starts, Argus queries `virsh domifaddr --source lease --full` and waits until libvirt reports that exact reserved IPv4 address before exposing the secure guest client.

A configured `guest_address` is accepted only if it exactly matches the provider's allocated reservation and must be IPv4. It cannot redirect Argus to an unrelated host.

## Linux GUI testing

Argus's Linux GUI adapter is X11-based. A Linux Capsule intended for desktop GUI tests should run its interactive test session under **Xvfb** (or another intentionally configured local X server) inside the guest.

The libvirt domain itself does not expose VNC/SPICE. The guest agent and the application-under-test share the guest's local display, so screenshots and input stay inside the Capsule.

A golden image can, for example, start an Xvfb-backed user service and then launch:

```bash
python -m argus.capsule.secure_guest_agent \
  --host 0.0.0.0 \
  --port 8765 \
  --token-file /var/lib/argus/guest-token.once \
  --tls-cert /var/lib/argus/tls/guest-cert.pem \
  --tls-key /var/lib/argus/tls/guest-key.pem
```

The bootstrap token and TLS private-key file are consumed from the writable session layer when the secure guest agent starts, matching the PR6 control-plane model.

## Transfer semantics

The PR5 `staging:` and `collect:` protocol remains provider-neutral. Specs continue to use forward-slash relative paths.

Declarative specs use a conservative portable path subset so a test can move between Windows and Linux without becoming ambiguous. Runtime path helpers also expose explicit Linux/POSIX semantics for provider/guest code where case-sensitive names are required.

All existing transfer properties remain in force:

- project-rooted race-safe source handles;
- bounded 256 KiB chunks;
- 64 MiB per file;
- 256 MiB aggregate transfer/snapshot limit;
- stable guest-side collection snapshots;
- host output-tree pinning;
- transactional collection rollback.

## Provider capabilities

`CapsuleProviderCapabilities` makes provider differences explicit rather than encoding them in scattered platform conditionals. A provider advertises:

- supported host platforms;
- supported guest OS families;
- secure transport support;
- network-isolation support;
- explicit staging/collection support;
- failure-retention support;
- whether destination-CIDR egress allowlists are implemented.

`SecureCapsuleExecutionEnvironment` treats this descriptor as a security contract, including for an injected extension provider. Before the provider can allocate a VM, secure transport, network isolation, and explicit staging/collection must all be advertised. If the run requests failure retention, the provider must advertise retention. If the configuration requests egress allowlisting, the provider must advertise egress-allowlist support. A provider with missing or mismatched capabilities is rejected before `create()` is called.

The secure environment also uses the provider contract to resolve `guest_os: auto` and rejects explicit unsupported guest OS selections.

## Validation boundary

Hosted CI can validate provider selection, XML generation, ordering, rollback behavior, path semantics, network-pool allocation, capability enforcement, and simulated libvirt command flows on Ubuntu and Windows runners. It cannot create a real nested KVM/libvirt Capsule in GitHub-hosted CI.

Before claiming hardware-backed Linux isolation, run a manual/on-prem smoke test that verifies:

1. the qcow2 overlay boots and the golden image remains unchanged;
2. the system QEMU account can access the configured/default Capsule storage without broadening access to the user's home;
3. two concurrent Capsules receive non-overlapping `/24`s and distinct guest endpoints;
4. special-use/non-RFC1918 pools are rejected before network creation;
5. the isolated network has no physical forwarding;
6. the nwfilter blocks arbitrary guest → host and guest → LAN traffic;
7. only the host can reach the secure guest-agent control port;
8. staging/collection work through the existing guest protocol;
9. Linux CLI and Xvfb desktop tests execute inside the guest;
10. an aarch64 golden image boots through libvirt-selected EFI firmware on an ARM64 host;
11. normal teardown removes domain/network/filter/session storage;
12. a forced mid-create failure never deletes a pre-existing domain/network/filter;
13. failure retention powers off and preserves forensic disk/config state.
