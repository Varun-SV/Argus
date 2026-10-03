# OS environment provisioning

Argus builds immutable OS images from operator-supplied installation media, then
runs the existing Capsule runtime from those images. Provisioning is a host-side
build step; it does not create a second VM execution path. The current providers
are Hyper-V on Windows and system libvirt/QEMU on Linux, both for x86_64 guests.

> **Windows is currently blocked from production publication and Capsule
> startup.** Capsule bootstrap media carries a fresh generation bearer and TLS
> key. A native NTFS VHDX delivery path now replaces the readable bootstrap DVD.
> Its volume root and payload have protected ACLs granting only SYSTEM and
> administrators access, and the guest rejects optical/removable media, broad
> ACLs, and ambiguous media. This boundary has not yet passed the required
> native retained-startup attack test, so the Windows provider's
> `protected_bootstrap_media` capability remains false. Provisioned Windows
> Capsule preparation fails closed before exposing the media, and the Windows
> image build cannot pass its real secure baseline. Do not treat the Windows
> runtime image path as complete or reconnectable. The gate can change only
> after native protected delivery is verified, including the
> retained-startup attack test described below.

An environment definition pins the source ISO, virtual hardware, installation
policy, and offline Argus guest runtime. Its content identity includes the ISO
digest, runtime bundle digest and policy versions, and image-construction policy.
Local file paths and secret references are locators and are excluded from that
identity. The derived-image manifest separately binds the environment and
definition digests, provider, image format and digest, and successful ATES
provisioning evidence. Before reuse, Argus verifies the manifest, image bytes,
and evidence. A stale or invalid cache entry is rejected, not silently reused.

## Build the offline guest runtime

Build on a native Windows or Linux x86_64 host with the target platform's
approved Python dependencies and PyInstaller already installed. The script
does not install dependencies or download browser engines. Linux builds should
use a distribution ABI compatible with the target Ubuntu guest. The output path
must not already exist.

```sh
python -m scripts.build_guest_runtime dist/argus-guest-win.zip --runtime-version 0.1.0
```

Run the same command on Linux to build the Ubuntu runtime. The script freezes
the guest driver and installed Argus adapters into an offline bundle and prints
JSON containing its SHA-256, target OS/architecture, runtime version, and
bootstrap policy versions. Copy the digest and bundle path into the matching
environment definition. Runtime bundles are platform-specific; do not reuse a
Windows bundle for Ubuntu or vice versa.

The current bootstrap-service and runtime-installation policies are v2. Rebuild
older bundles and environments; an explicitly pinned v1 policy is rejected,
including when selecting a previously published image. This prevents an older
Ubuntu image without optical-device isolation from being reused under the new
control contract.

## Define and build an environment

This Windows definition documents the intended contract and offline runtime
bundle inputs. It is not currently publishable or usable for Capsule startup
because of the bootstrap-media block above. Use an ISO digest computed from the
exact licensed media you supply.

```yaml
schema_version: argus-environment-v1
name: win11-lab
source:
  kind: installation_media
  media_type: iso
  path: C:/isos/Win11_English_x64.iso
  sha256: <64-character-iso-sha256>
  architecture: x86_64
machine:
  architecture: x86_64
  cpu_count: 4
  memory_mb: 8192
  firmware: uefi
  secure_boot: true
  tpm_version: "2.0"
  disk_size_gib: 64
  disk_bus: scsi
  network_mode: host_only
installation:
  unattended: true
  edition: professional
  update_policy: manual
  target_os: windows-11
  target_release: 24H2
guest_runtime:
  bundle_path: dist/argus-guest-win.zip
  runtime_bundle_sha256: <digest-from-build-script>
  runtime_version: 0.1.0
  target_os: windows-11
  target_architecture: x86_64
```

Load definitions with `load_environment_definition(path)`. The following
shows the provider and plan API shape for a Windows build; with the current
provider security capability, its real Capsule baseline will fail closed and
no image will be published:

```python
from argus.capsule.base import CapsuleSettings
from argus.provisioning import (
    HyperVProvisioner, build_provisioning_plan, load_environment_definition,
)

definition = load_environment_definition("environments/win11.yaml")
baseline = CapsuleSettings(
    provider="hyperv", guest_os="windows", switch_name="Argus-Internal",
    guest_transport="https", network_mode="host_only",
)
provider = HyperVProvisioner(
    switch_name="Argus-Internal", baseline_settings=baseline,
)
plan = build_provisioning_plan(
    definition, provider.capabilities(), output_format="vhdx",
    cache_root="images", evidence_root="provisioning-evidence",
)
# Currently fails at the real secure Capsule baseline; no image is published.
result = provider.provision(definition, plan)
```

`HyperVProvisioner` accepts an optional `baseline_settings` argument for a real
secure baseline Capsule, but Windows publication remains blocked until the
protected-media path is natively verified.
`LibvirtProvisioner(network_name=..., qemu_group=...)` has the corresponding API
and accepts `baseline_settings`. Libvirt advertises `protected_bootstrap_media=True` after
the Ubuntu seed removes supplementary target groups and installs a polkit rule
denying udisks2 actions to the `argus` user; native startup-attack acceptance is
still required. A udev rule also removes optical and SCSI-passthrough `uaccess`
tags before logind grants desktop access and fixes their ownership/mode to
root-only. Guest intake rejects exposed block devices before mounting them.
The providers need the matching host, hypervisor tools, private
host-only network, and licensed ISO. Libvirt also requires an active non-forwarding system network, local
`virsh`/`qemu-img`, a QEMU group shared with Argus, and a cache root whose
ancestors that group can traverse. Provider capability checks fail closed on
unsupported firmware, disk bus, network, security, or image format.

Attended installation remains available for definitions with
`unattended: false`, manual updates, and no packages or credential reference.
Unattended Windows supports Windows 11 23H2/24H2 Professional, Education, or
Enterprise, UEFI, and the provider's supported secure boot/TPM contract. The
Ubuntu profile selects a pinned server or desktop source, requires a specific
release/flavor, `update_policy: latest`, and an explicit host-reachable apt
mirror. The publishable Ubuntu runtime-backed profile creates a locked target account
and writes a locked password (`!`) in the production seed; it does not require
or embed a reusable account password/hash in the seed or base image.
Compatibility answer-only generation can still use `installation.credential_ref`
with a SHA-512 crypt value. The seed and temporary installer resources are
removed after installation.

The unattended installers prepare the OS-specific runtime and generalization
steps. Windows stages the runtime under ProgramData, configures the bootstrap
service and first-Capsule specialization, and invokes Sysprep. These steps do
not make Windows publication or startup production-ready: the provider still
fails the protected-bootstrap-media capability gate. Ubuntu installs a systemd
bootstrap service, locks the target account, strips supplementary target-user
groups, denies that user udisks2 actions with a polkit rule, and removes
clone-specific machine/cloud-init state. Ubuntu Desktop also configures its
target desktop session for the baseline. Runtime bundle verification,
installation, generalization, baseline, or cleanup failure blocks publication.
If provider cleanup cannot be confirmed, Argus retains the private workspace
for recovery rather than publishing an uncertain image.

## Use a published image from config

Provisioning and runtime image selection are separate steps. The following
config asks Argus to load a previously published image from its verified cache;
it does not start an installation build. The environment definition, image cache,
and evidence paths are resolved from the project directory. `control_root` is
resolved from the process working directory, so use an absolute path when
Argus may be started from different directories.

```yaml
execution:
  environment: capsule
  capsule:
    provider: libvirt
    environment_definition: environments/ubuntu.yaml
    image_cache_root: images
    provisioning_evidence_root: provisioning-evidence
    image_format: qcow2
    control_root: .argus/capsule-control
    default_execution_mode: isolated
    allowed_execution_modes: [isolated, shared_user]
    allow_llm_mode_change: false
    failure_allow_reconnect: true
    failure_allow_llm_reconnect: false
    retain_on_failure: true
```

Argus loads the definition, recreates the deterministic plan, verifies the
published image and its ATES evidence, then binds the verified image, runtime,
and machine contract into `CapsuleSettings`. A missing cache entry or invalid
evidence is an error; run the build API first. In provisioned mode the image is
selected by the definition/cache and cannot be replaced with a per-session
image. `capsule_overrides` in `ArgusConfig.make_execution_environment` permits
only `provider` and `retain_on_failure`; a provider override still has to match
the verified image contract. Keep the host's allowed execution modes and
failure policy authoritative.

This runtime example is for a Linux/libvirt definition and cache. A Windows
definition can be parsed and its runtime bundle verified, but the current
Hyper-V provider's false protected-media capability prevents secure Capsule
preparation and therefore prevents both real baseline publication and normal
Windows provisioned execution.

Provisioned Capsules use pinned HTTPS bootstrap and per-generation credentials;
they do not read the legacy static guest token or CA settings. The host
`control_root` stores durable Capsule ownership and generation state and should
be writable only by the Argus account. If a retained Failure Capsule must be
reconnected after an Argus process restart, create an idle provisioned
controller and call `restore_failure(metadata_path)` with its retained
`failure-capsule.json`. The durable control registry, provider resource, disk,
environment, and image identities must still agree; metadata alone never
grants ownership.

`reconnect_failure(execution_mode=..., requested_by_llm=...)` reconnects the
same retained VM and disk with a new generation and fresh session/TLS/bearer
material only when the selected provider advertises protected bootstrap media.
`transition_execution_mode(execution_mode, requested_by_llm=...)` has the same
provider requirement. Both operations stop the VM before establishing a new
generation, so the old authority is fenced. Neither operation makes the current
Windows provider usable: its capability gate prevents bootstrap preparation.
Linux worker cleanup signals the process group; a daemon that escapes that
group may survive the signal, so provider VM power-off/destroy is the final
fence on reconnect and mode change.

## Baseline and acceptance status

Before publication, ATES boots two separate disposable child Capsules from the
candidate image and validates the runtime identity, OS release/edition or
flavor, requested packages, generation, and fresh TLS/session/bearer digests.
It also invokes `whoami` on Windows or `id -un` on Ubuntu and checks that the
worker is a non-administrator/non-root user. Ubuntu Desktop additionally must
report desktop readiness. The two children are torn down before the image is
published; teardown uncertainty blocks publication.

Static checks and mocked provider tests do not establish live host acceptance.
Release acceptance requires successful installation, generalization, baseline,
and reuse on supported live Hyper-V/Windows and libvirt/QEMU/Ubuntu hosts; a
Windows run remains blocked until protected delivery is natively verified. On both
providers, perform a retained-startup attack test: while bootstrap media is
still attached during startup, execute as the target user and verify it cannot
read the fresh bearer or TLS private key before the host detaches the media.
For Ubuntu, inspect udev tags and effective ACLs after first boot and after media
reattachment: optical `/dev/sr*` and matching SCSI passthrough `/dev/sg*` nodes
must remain root:root with mode 0600 and no active-user read grant. Verify the
root bootstrap service still consumes the media successfully.
Also require Windows foreground-input comparison and CI coverage for the
supported build/runtime paths. Record host/provider, media and runtime digests,
and evidence with each live acceptance run. Operating-system media, licensing,
activation, and network availability remain the operator's responsibility.

### Native Windows media boundary check

On a dedicated elevated Windows host with Hyper-V and the Windows dependencies,
the opt-in test below creates and host-mounts a temporary NTFS VHDX. It checks
privileged access, impersonates an existing dedicated non-admin test account,
and requires file reads/writes and raw volume/disk reads to be denied. It does
not create accounts, persist their passwords, or enable production control.
Supply the account in `ARGUS_BOOTSTRAP_TEST_USER` and its password securely in
`ARGUS_BOOTSTRAP_TEST_PASSWORD`; neither is passed to PowerShell. Set
`ARGUS_NATIVE_BOOTSTRAP_TEST=1`, then run:

```powershell
python -m pytest -q tests/test_windows_bootstrap_media.py -k native_ntfs
```

A passing host-media test is only one gate. Still verify consumption by the
guest's SYSTEM bootstrap service and denial to retained `argus-target` startup
code on the same Hyper-V VM during both initial boot and reconnect, including
fresh credentials, generation fencing, and confirmed disk detachment. Windows
production capability remains disabled until that full native acceptance passes.
