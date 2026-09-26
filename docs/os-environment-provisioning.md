# OS environment provisioning

Argus provisioning turns user-supplied installation media into reproducible immutable
base images that feed the existing ExecutionEnvironment -> Capsule -> Adapter runtime.

This is intentionally not a second VM execution stack. Provisioning owns the build-time
installation of an operating system. Capsules still own disposable test execution.

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
- creation timestamp.

Before a derived image is handed to CapsuleSettings, Argus verifies both the manifest
binding and the final image bytes.

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

## Concrete attended providers

The provider-neutral contract is separated from the Hyper-V and libvirt builders.
Both create a temporary installer VM from the verified ISO, wait for an orderly
guest shutdown, destroy the installer VM, hash the output image, and publish the
image and manifest in one cache directory. A per-key kernel lock serializes
cooperating builders. Existing published images are verified and reused, never
overwritten. Failure and ordinary interruption remove temporary files and VM
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
non-forwarding local system network and local `virsh`/`qemu-img` access; the
installer console uses VNC bound to localhost. These providers build images
with an operator at the installer console. Use `unattended: false`,
`update_policy: manual`, and no edition, package list, or credential reference.
Nondefault locale/timezone are rejected because the generic provider cannot
apply them. An operator must verify the installed OS, its Argus guest agent,
licensing, and readiness before using the image in a Capsule run.

For example, from Python after loading a definition with those attended fields:

```python
from argus.provisioning import HyperVProvisioner, build_provisioning_plan

provider = HyperVProvisioner(switch_name="Argus-Internal", on_started=print)
plan = build_provisioning_plan(
    definition, provider.capabilities(), output_format="vhdx", cache_root="./images"
)
result = provider.provision(definition, plan)
```

The `on_started` hook receives only the temporary VM name. Finish installation
and shut the guest down; the provider will remove the temporary VM. For Linux,
use `LibvirtProvisioner(network_name="argus-local")` and a qcow2/raw plan.
Use `capsule_settings_from_derived_image` to verify and bind the published image
to `CapsuleSettings`, then configure the normal secure guest transport. Fleet
can call `derived_image_advertisement` to advertise its verified SHA-256;
aliases remain labels and never become execution identity.

Unattended installation mechanics are OS-specific and are not claimed by these
attended providers. They reject `credential_ref`, packages and unattended mode
instead of storing credentials or claiming to have applied unsupported inputs.
Future OS-specific drivers must inject secrets ephemerally and keep them out of
definitions, manifests, logs, reports, and ATES payloads.

## ATES direction

Provisioning events still need to become canonical ATES evidence: media verification,
provider selection, machine definition, installation start/completion, driver/package
steps, baseline validation, final image hashing, and publication. Provisioning evidence
must follow the existing privacy pipeline rather than creating a parallel evidence format.

## Fleet direction

Fleet should advertise immutable derived-image identities, not mutable filenames. A node
that already has the exact image can launch it immediately. A node that lacks it can
provision or receive the approved derived image according to later Fleet policy.

This makes OS/image matrices reproducible while keeping the existing placement, fencing,
Capsule and ATES trust boundaries intact.
