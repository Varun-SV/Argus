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
    name: win11-24h2-clean

    source:
      kind: installation_media
      media_type: iso
      path: D:/isos/Win11_24H2_English_x64.iso
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
      disk_bus: nvme
      network_mode: isolated

    installation:
      unattended: true
      edition: professional
      locale: en-US
      timezone: Asia/Kolkata
      packages:
        - python
        - git
      update_policy: frozen
      credential_ref: secret://windows-lab/installer

## Media ownership and licensing

Argus does not fetch, redistribute, or provide proprietary operating-system media.
Installation media is supplied by the operator and is addressed by its expected digest.
Operators remain responsible for media licensing and OS activation requirements.

An ISO filename alone is never trusted as identity.

## Security boundary

The current foundation verifies a regular file and its expected SHA-256. A future concrete
provisioning provider must stage or re-open and re-verify those bytes immediately before
hypervisor attachment. The verifier does not claim that a pathname remains pinned after
its verified handle is closed.

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

## Planned concrete providers

The provider-neutral contract is intentionally separated from concrete installers.

Hyper-V should produce VHDX base images on supported Windows hosts. Libvirt/QEMU should
produce qcow2 or raw base images on supported Linux hosts. Each provider must preserve
the requested firmware/security contract and must not weaken an unsupported machine
definition.

Unattended installation mechanics are OS-specific. Windows may use an operator-controlled
answer-file workflow; Linux distributions may use their supported unattended installer
mechanisms. Secret material must be injected from a secret reference and must not be
persisted in the environment definition, derived manifest, logs, reports, or ATES payloads.

## ATES direction

Provisioning events should eventually become canonical ATES evidence: media verification,
provider selection, machine definition, installation start/completion, driver/package
steps, baseline validation, final image hashing, and publication. Provisioning evidence
must follow the existing privacy pipeline rather than creating a parallel evidence format.

## Fleet direction

Fleet should advertise immutable derived-image identities, not mutable filenames. A node
that already has the exact image can launch it immediately. A node that lacks it can
provision or receive the approved derived image according to later Fleet policy.

This makes OS/image matrices reproducible while keeping the existing placement, fencing,
Capsule and ATES trust boundaries intact.
