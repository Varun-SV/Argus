# OS environment provisioning

Argus provisioning turns user-supplied installation media into reproducible immutable
base images that feed the existing ExecutionEnvironment -> Capsule -> Adapter runtime.

This is intentionally not a second VM execution stack. Provisioning owns the build-time
installation of an operating system. Capsules still own disposable test execution.

**Draft status:** The unattended profiles and their static checks are implemented,
but no live Windows 11 or Ubuntu install has passed the required secure Capsule
baseline. The providers do not yet inject a guest-agent bundle or per-session
bootstrap token and TLS private key into the disposable guest. Publication must
continue to fail until those pieces and live host tests are complete. Reusable
guest secrets must not be placed in a base image to make the baseline pass.

## Identity model

An environment identity is derived from:

- installation-media SHA-256 and architecture;
- virtual hardware and firmware requirements;
- non-secret installation inputs.

The host-local ISO path is only a locator and does not affect identity. Secret references
are also excluded from the content identity so secret-store rotation does not silently
create a different OS definition.

A derived image has a separate manifest binding:

- environment ID;
- complete definition SHA-256;
- source-media SHA-256;
- provisioning provider;
- output image format;
- final image SHA-256;
- architecture;
- creation timestamp;
- the finalized ATES provisioning run ID.

The v2 derived-image manifest requires a successful disposable secure Capsule
boot before publication. Older v1 cache entries fail validation; an operator
must quarantine an old entry and rebuild it rather than treating a format-valid
disk as a proven OS image.

Before a derived image is reused by a builder, Argus verifies the manifest binding,
final image bytes, and the passed ATES run. A caller that imports a manifest directly
must also use the same ATES verification path before trusting its provenance.

## Example definition

    schema_version: argus-environment-v1
    name: win11-lab-clean

    source:
      kind: installation_media
      media_type: iso
      path: D:/isos/Win11_English_x64.iso
      sha256: <64-character sha256>
      architecture: x86_64

    machine:
      architecture: x86_64
      cpu_count: 8
      memory_mb: 16384
      firmware: uefi
      secure_boot: true
      tpm_version: "2.0"
      disk_size_gib: 128
      disk_bus: scsi
      network_mode: host_only

    installation:
      unattended: false
      locale: en-US
      timezone: UTC
      packages: []
      update_policy: manual

## Media ownership and licensing

Argus does not fetch, redistribute, or provide proprietary operating-system media.
Installation media is supplied by the operator and is addressed by its expected digest.
Operators remain responsible for media licensing and OS activation requirements.

An ISO filename alone is never trusted as identity.

## Security boundary

The provider re-verifies the operator-supplied ISO, copies it into a private build
directory, hashes that copy against the expected SHA-256, then attaches only the
staged path. The verifier does not claim that a pathname remains pinned after its
verified handle is closed.

Provider selection is fail-closed. A provider must explicitly advertise support for the
requested architecture, media type, output format, firmware, disk bus, network mode,
Secure Boot and TPM version. Argus must not silently downgrade those requirements.

Only isolated and host-only provisioning networks are admitted by the v1 model. Broader
network access needs an explicit later policy rather than becoming an implicit installer
escape path.

## Provisioning flow

    Environment YAML
          |
          v
    strict EnvironmentDefinition
          |
          +--> verify user ISO digest
          |
          v
    provider capability gate
          |
          v
    deterministic ProvisioningPlan
          |
          v
    provider installs OS
          |
          v
    disposable secure Capsule baseline boot
          |
          v
    immutable derived image + manifest
          |
          v
    verify manifest and final image digest
          |
          v
    existing CapsuleSettings.image
          |
          v
    ExecutionEnvironment -> Capsule -> Adapter

## Hyper-V and libvirt providers

The provider-neutral contract is separated from the Hyper-V and libvirt builders.
Both create a temporary installer VM from the verified ISO, wait for an orderly
guest shutdown, destroy the installer VM, then boot a disposable Capsule child
from the candidate disk. The baseline requires a pinned HTTPS secure guest
agent that reports the expected OS family and architecture after per-session
bearer rotation. When a target OS profile is present, the baseline also checks
its reported release and Windows edition or Ubuntu flavor and package set.
Only then does the builder hash and publish the image and manifest in one cache
directory.
A per-key kernel lock serializes cooperating builders. Existing published images
are verified and reused, never overwritten. Failure and ordinary interruption remove temporary files and VM
resources. If VM cleanup cannot be confirmed, the private workspace is retained
for operator recovery rather than deleting a disk still attached to a VM. An
abrupt host crash can also require operator cleanup of an orphan VM.

The initial supported build and Capsule contracts are deliberately narrow:

| Provider | Host | Architecture | Firmware | Disk bus | Output | Network |
| --- | --- | --- | --- | --- | --- | --- |
| Hyper-V | Windows | x86_64 | UEFI, optional Secure Boot and TPM 2.0 | SCSI | VHDX | host_only |
| libvirt/QEMU | Linux | x86_64 | BIOS | virtio | qcow2/raw | host_only |

Hyper-V applies the requested Secure Boot mode and TPM 2.0 to both the installer
VM and disposable Capsule. Its output is a disk image; vTPM state is not a
portable part of the VHDX, so a guest that seals its disk to the installer
VM's TPM (for example, with BitLocker) needs additional recovery handling and
must not be assumed bootable in a fresh Capsule. Libvirt currently rejects
Secure Boot and TPM requests. Unsupported disk buses, firmware, architectures,
and `isolated` networking likewise fail instead of changing the machine
contract. Hyper-V needs an Internal switch. Libvirt needs an active,
non-forwarding local system network and local `virsh`/`qemu-img` access. It
also requires an explicit QEMU group shared with the Argus process and a cache
root whose ancestors the group can traverse. The private build directory and
its ISO/image grant only that group the needed read/write access; the
installer console uses VNC bound to localhost. For an attended build, use `unattended: false`,
`update_policy: manual`, and no edition, package list, or credential reference.
Nondefault locale/timezone are rejected because the generic provider cannot
apply them. An operator must install/configure the secure Argus guest agent,
its dedicated TLS certificate and bootstrap bearer, and the required guest
service policy before shutting down the installer. Installation/licensing and
release/package verification remain the operator's responsibility.

For example, from Python after loading an attended definition:

```python
import os
from argus.capsule.base import CapsuleSettings
from argus.provisioning import HyperVProvisioner, build_provisioning_plan

baseline = CapsuleSettings(
    provider="hyperv", guest_os="windows", switch_name="Argus-Internal",
    cpu_count=definition.machine.cpu_count, memory_mb=definition.machine.memory_mb,
    network_mode="host_only", guest_transport="https",
    guest_ca_cert="C:/Argus/certs/guest-ca.pem",
    guest_token=os.environ["ARGUS_CAPSULE_GUEST_TOKEN"],
)
provider = HyperVProvisioner(
    switch_name="Argus-Internal", on_started=print, baseline_settings=baseline
)
plan = build_provisioning_plan(
    definition, provider.capabilities(), output_format="vhdx", cache_root="./images"
)
result = provider.provision(definition, plan)
```

The `on_started` hook receives only the temporary VM name. Finish installation
and shut the guest down; the provider removes the temporary VM and performs the
baseline Capsule boot. For Linux, use
`LibvirtProvisioner(network_name="argus-local", qemu_group="libvirt-qemu",
baseline_settings=...)` and a qcow2/raw plan. The group name varies by host.
Use `capsule_settings_from_derived_image` to verify and bind the published image
to `CapsuleSettings`, then configure the normal secure guest transport. Fleet
can call `derived_image_advertisement` to advertise its verified SHA-256;
aliases remain labels and never become execution identity.

### Narrow unattended profiles

The Windows 11 Hyper-V profile creates a private `Autounattend.xml` disc alongside
the verified installer ISO. It selects a named Professional, Education, or Enterprise
image, partitions the target disk for UEFI, and shuts down in Audit mode. It accepts
23H2 or 24H2, `en-US`, `UTC`, `update_policy: manual`, and no package or credential
reference. It does not install the Argus guest agent or create a non-admin test
account. A stock Windows ISO alone therefore cannot pass the mandatory secure
Capsule baseline. An approved offline agent/runtime/TLS bundle and session
bootstrap are still required, along with validation of the intended account and
service policy.

The Ubuntu libvirt profile uses the verified ISO's `casper/install-sources.yaml` to
pin a full desktop or server source. It boots the installer kernel with a private
NoCloud seed disc and the `autoinstall` argument. The seed contains a SHA-512 crypt
password hash resolved at build time from `installation.credential_ref`; the secret
reference is excluded from environment identity. The seed is removed after the
installer VM is destroyed. Ubuntu requires `update_policy: latest`, a specific
`target_release` and `target_flavor`, and an explicit `apt_mirror` reachable from
the host-only network. The installer does not bootstrap the Argus guest agent.
Its required baseline therefore also needs approved agent installation and
session bootstrap. The baseline checks the reported Ubuntu release,
desktop/server metapackage, and each requested package.

Both unattended profiles remain subject to a successful live install and secure
Capsule boot on suitable hosts. Static answer generation and mocked provider tests
alone do not establish a usable base image.

### Per-user secret store

`argus secrets set secret://argus/ubuntu/install` prompts without echo and replaces
an existing value. `argus secrets list` displays references only, and
`argus secrets remove secret://argus/ubuntu/install` deletes one. `set --stdin` and
`set --file PATH` support non-interactive input; avoid exposing values in shell
arguments or recorded terminal history. The Ubuntu value must already be a
SHA-512 crypt password hash, not a plaintext password. Configure the guest agent
bootstrap token with another reference and set `execution.capsule.guest_token_ref`
to it. The token is resolved into host Capsule settings when they are created.
Per-session delivery to the guest remains to be implemented. The secure client
rotates the bootstrap bearer after authenticating with the guest agent.

Windows stores encrypted values with user-scoped DPAPI under `%APPDATA%/Argus/Secrets`.
Linux uses `$XDG_DATA_HOME/argus/secrets` or `~/.local/share/argus/secrets`; macOS
uses `~/Library/Application Support/Argus/Secrets`. The latter two encrypt the
database with a local key readable only by the same OS account. This protects a
copied database, while host account permissions protect the key. Do not copy the
key and database together to a less trusted machine.

## ATES provisioning evidence

`AtesProvisioningRecorder` uses the normal ATES event store, run identity, step-attempt
lifecycle, finalization transaction, manifest verification, and derived reports. Its
`PROVISIONING` run source records the requested machine contract, definition/media
digests, provider, architecture, and image format. Fixed ordered stages record media
verification, provider selection, installation start/completion, secure Capsule baseline,
image hashing, and publication. The final image SHA-256 is a validated safe digest in
an ATES observation. On failure, the recorder emits an `error` outcome without copying
hypervisor output or exception text; uncertain cleanup leaves the run incomplete.
ISO locators, operator names, credential references, and guest secrets never enter the
canonical event stream. The derived-image manifest binds the finalized ATES run
identity; cache reuse verifies both the image bytes and that run. Release, edition,
and package checks occur in the secure Capsule baseline before the image is published.

## Fleet direction

Fleet should advertise immutable derived-image identities, not mutable filenames. A node
that already has the exact image can launch it immediately. A node that lacks it can
provision or receive the approved derived image according to later Fleet policy.

This makes OS/image matrices reproducible while keeping the existing placement, fencing,
Capsule and ATES trust boundaries intact.
