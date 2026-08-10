# Argus Capsules on Hyper-V

Argus Capsule mode runs the application under test inside a disposable Hyper-V
virtual machine instead of on the user's interactive Windows desktop.

## Capsule lifecycle

Each Argus session:

1. validates the configured Hyper-V switch and golden VHDX;
2. creates a per-session **differencing VHDX** whose parent is the golden image;
3. creates and boots a Generation-2 VM;
4. discovers the guest IPv4 address and attests any configured address against
   the addresses Hyper-V reports for that exact VM;
5. waits for the Argus guest agent;
6. optionally initializes a per-session transfer workspace and stages explicitly
   declared project files into it;
7. launches/observes/drives the target through the guest agent;
8. runs any non-close teardown preparation, then explicitly collects declared
   workspace artifacts immediately before a declared close (or before implicit
   final cleanup when no close is reached);
9. normally destroys the VM and session disk when the session closes;
10. optionally powers off and retains the failed VM's writable disk/configuration
    when `retain_on_failure: true` is configured.

The golden image is never booted directly by Argus.

## Capsule staging and artifact collection

PR5 adds an explicit file boundary. There are no shared folders and no automatic
guest→host writes. Every path crossing the boundary comes from the test spec.

Example:

```yaml
name: Test staged application

staging:
  - source: dist/app.exe
    destination: app/app.exe
    # sha256: optional 64-character digest to pin the host source
  - source: fixtures/input.json
    destination: fixtures/input.json

target:
  adapter: desktop-gui
  launch: stage://app/app.exe

steps:
  - assert:
      process_running: true

collect:
  - logs/test.log
  - output/result.json

teardown:
  - close
```

### Staging rules

- `source` is relative to the Argus project root. Absolute paths, drive-qualified
  paths, `..`, and sources that resolve outside the project root are rejected.
- `destination` is relative to a fresh per-session guest workspace. Absolute,
  drive-qualified, traversal, and filesystem-link escapes are rejected.
- Staging is file-by-file and explicit; PR5 does not recursively copy directories
  or expand globs.
- Argus computes SHA-256 before upload. If `sha256` is supplied in the spec, the
  source must match it before any bytes are sent.
- The guest receives bounded sequential chunks, recomputes SHA-256, and atomically
  commits the file only after the declared size and digest match.
- `stage://path` can launch only a destination that was successfully committed in
  the current session. An undeclared or failed staged target causes pre-launch
  rollback rather than Failure Capsule retention.

The guest transfer workspace becomes the target process's inherited working
directory. Applications that write relative paths therefore naturally produce
outputs inside the only guest tree Argus is willing to collect.

### Collection rules

- `collect` paths are relative to the same per-session workspace and are fixed by
  the test spec, not by the model or application under test.
- Each artifact must resolve to a regular file inside the workspace. Symlink or
  reparse/junction escapes are rejected by resolving containment before reads.
- Argus first obtains size/SHA-256 metadata for every requested artifact and
  enforces the aggregate limit before downloading any of them.
- Downloads use bounded chunks into a temporary host file under that run's
  `.argus/runs/.../artifacts/` directory. The host recomputes SHA-256 and uses an
  atomic replace only after the digest matches.
- Non-close teardown steps run first so they may flush/export requested output.
  Collection then runs at the last safe point immediately before a declared
  `close`; if execution raises or no close is reached, collection runs before
  implicit final cleanup. A collection failure is a run error. When
  `retain_on_failure: true`, that error is recorded before close so PR4 can
  preserve the Capsule instead of deleting the evidence.

Current hard limits are:

```text
256 KiB  maximum transfer chunk
64 MiB   maximum individual staged/collected file
256 MiB  maximum declared transfer set per direction/session
```

These are safety limits, not performance targets. Large source trees should be
reduced to the explicit files needed by the test rather than using the guest
agent as a general-purpose file-sharing service.

### Transfer confidentiality

The current guest-agent channel is authenticated HTTP on an Internal Hyper-V
switch. It provides request authentication and transfer integrity, but **not
transport confidentiality**. Staging/collection therefore does not make the
channel suitable for secrets that must be protected from a sufficiently
privileged host/local-network observer. Confidential host↔guest transport and
per-session host-bound credentials remain follow-up isolation work.

The host may be locked while a Capsule runs. Host sleep, hibernate, shutdown, or
loss of Hyper-V still interrupts the VM. The **guest** interactive test user must
remain logged in and unlocked if physical GUI input is used.

## 1. Prepare a Windows golden image

Create a Windows 10/11 or Windows Server Generation-2 VHDX and install:

- Python 3.10+;
- Argus with the dependencies needed by the target adapter, for example
  `pip install -e .[windows]` or an installed Argus package.

The application under test no longer has to be baked into the golden image when
it can be staged explicitly. Runtime dependencies that are expensive or unsafe
to transfer may still be preinstalled in the image.

For GUI testing, configure a dedicated **non-administrator** test user to log in
automatically or otherwise reach an interactive unlocked desktop. Disable guest
sleep and screen locking for that disposable test account.

Hyper-V **Key-Value Pair Exchange** should remain enabled because Argus obtains
the control endpoint from `Get-VMNetworkAdapter`. If `guest_address` is
configured, it is only a preferred candidate: Argus refuses it unless Hyper-V
reports that exact IPv4 address for the newly created VM.

## 2. Configure the guest-agent credential safely

Do **not** store `ARGUS_CAPSULE_GUEST_TOKEN` in the interactive test user's
persistent environment. Targets launched by normal Windows/CLI adapters inherit
the guest-agent process environment, so a persistent control-plane credential
would be visible to the application under test.

For the current MVP, place the shared bootstrap token in a one-time file in the
golden image, for example:

```powershell
New-Item -ItemType Directory -Force C:\ProgramData\Argus | Out-Null
Set-Content -NoNewline `
  -Path C:\ProgramData\Argus\guest-token.once `
  -Value "replace-with-a-long-random-secret"
```

Start the guest agent in the interactive test-user session at logon:

```powershell
python -m argus.capsule.guest_agent `
  --host 0.0.0.0 `
  --port 8765 `
  --token-file C:\ProgramData\Argus\guest-token.once
```

The agent reads and **deletes the token file before it starts serving requests**.
Because Argus boots a differencing child disk, that deletion occurs only in the
per-session child; the golden parent retains its bootstrap file for the next
Capsule. The target is launched only after the guest agent is ready.

`--token-env` remains supported for supervisors that inject a **process-scoped**
environment variable at agent startup. The agent immediately removes that
variable from `os.environ` before any adapter or target can be created. Do not
use a User- or Machine-persistent environment variable for this purpose.

### Retained-memory credential boundary

The current guest agent still keeps the authenticated control token in live
process memory while a Capsule is running. For that reason PR4 deliberately
**does not use Hyper-V `Save-VM` for Failure Capsules**. Saving guest RAM would
serialize that shared cross-session credential to disk and make compromise of a
retained Capsule relevant to other sessions.

Until Argus has per-session host-bound guest credentials, retention is therefore
**disk/configuration-only**: Hyper-V powers the failed VM off without saving RAM
and preserves the registered VM plus its differencing VHDX. A later transport/
credential phase can safely reintroduce memory-preserving checkpoints once one
Capsule's retained memory cannot reveal credentials usable by another Capsule.

## 3. Use an Internal Hyper-V switch only

The guest-agent protocol uses authenticated **HTTP**, not encrypted transport.
Therefore Argus accepts only a host-reachable **Internal** Hyper-V switch for
Capsule control traffic. Hyper-V's Default Switch is suitable only when it
reports as Internal on the host.

Argus rejects Private and External switches. `allow_external_switch` is retained
only as a reserved compatibility setting; setting it to `true` is rejected until
Argus has a confidential host↔guest transport.

## 4. Configure Argus on the host

Set the matching token in the dedicated **host** environment variable before
launching Argus:

```powershell
$env:ARGUS_CAPSULE_GUEST_TOKEN = "replace-with-a-long-random-secret"
```

Then configure `.argus/config.yaml`:

```yaml
execution:
  environment: capsule
  capsule:
    provider: hyperv
    image: C:\Argus\images\windows-11-clean.vhdx
    switch_name: Default Switch
    vm_root: C:\Argus\capsules
    memory_mb: 4096
    cpu_count: 2
    guest_port: 8765
    guest_input_mode: physical
    # guest_address: 172.24.16.10
    boot_timeout_seconds: 120
    agent_timeout_seconds: 60
    allow_external_switch: false
    retain_on_failure: false
```

The project configuration cannot select another host environment variable for
the guest credential. A legacy `guest_token_env` entry is accepted only when its
value is exactly `ARGUS_CAPSULE_GUEST_TOKEN`.

## Failure Capsules

`retain_on_failure` is **off by default**. With the default setting, failed and
successful tests both keep the disposable behavior from PR3: the VM is removed
and the per-session differencing disk is deleted during teardown.

When `retain_on_failure: true` is enabled, the runner records the first condition
that makes the run fail **before teardown**. That includes normal failed/error
steps, budget exhaustion, unexpected execution errors, teardown errors, and PR5
artifact-collection errors.

When the Capsule closes after a recorded failure, Argus powers the VM off with
`Stop-VM -TurnOff` and keeps the registered VM plus the per-session differencing
VHDX. It does not serialize guest RAM. The resulting disk state is
**crash-consistent**, not guaranteed application-consistent: buffered or
unflushed application/filesystem writes may be absent after the hard power-off.

A retained session stays registered in Hyper-V and its session directory stays
under `vm_root/<session-id>`. Argus writes:

```text
vm_root/<session-id>/failure-capsule.json
```

The manifest contains the failure/session id, Hyper-V VM name, retained state,
reason, timestamp, provider, and storage path. The same metadata is copied into
the normal Argus `RunResult` JSON and Markdown report under `failure_capsule`.

### Retention failures and operator recovery

Retention is fail-safe. If powering off the VM or writing the retention manifest
fails, Argus does **not** fall through to destructive guest teardown or VM
deletion. The provider handle and session storage remain preserved.

The run keeps its original test status (`fail`/`error`) and adds structured
`failure_capsule_error` metadata containing the VM name, session storage path,
provider, underlying retention error, and recovery guidance. The Markdown report
surfaces the same information.

Retained Capsules consume disk space and remain registered VMs. Automatic
retention expiry, a resume UI, and export/import tooling remain follow-up work.

### Environment override

Failure retention can also be controlled by:

```text
ARGUS_CAPSULE_RETAIN_ON_FAILURE=true|false
```

It uses the same strict boolean parsing as other security-sensitive Capsule
settings.

## Other environment overrides

- `ARGUS_EXECUTION_ENVIRONMENT`
- `ARGUS_CAPSULE_PROVIDER`
- `ARGUS_CAPSULE_IMAGE`
- `ARGUS_CAPSULE_SWITCH`
- `ARGUS_CAPSULE_VM_ROOT`
- `ARGUS_CAPSULE_MEMORY_MB`
- `ARGUS_CAPSULE_CPU_COUNT`
- `ARGUS_CAPSULE_GUEST_PORT`
- `ARGUS_CAPSULE_GUEST_INPUT_MODE`
- `ARGUS_CAPSULE_GUEST_ADDRESS` (provider-attested before use)
- `ARGUS_CAPSULE_BOOT_TIMEOUT_SECONDS`
- `ARGUS_CAPSULE_AGENT_TIMEOUT_SECONDS`
- `ARGUS_CAPSULE_ALLOW_EXTERNAL_SWITCH` (must remain false)
- `ARGUS_CAPSULE_RETAIN_ON_FAILURE`

The guest control token itself is read directly from
`ARGUS_CAPSULE_GUEST_TOKEN`; there is no indirection variable that chooses a
second environment-variable name.

## Control-endpoint attestation

Project configuration is not trusted to choose the destination that receives a
bearer token. Before `GuestAgentClient` is constructed, `HyperVProvider` queries
`Get-VMNetworkAdapter -VMName <session VM>` and obtains the IPv4 addresses
reported for that exact VM. If a `guest_address` candidate is supplied, it must
match one of those addresses exactly.

## Cleanup and recovery

Argus removes the registered VM before deleting its per-session storage. If VM
deregistration fails, Argus **preserves the session directory and child VHDX**
and reports their path in the cleanup error. Storage is removed only after VM
removal succeeds.

A Failure Capsule is different: its VM/storage is retained deliberately, and
`failure-capsule.json` is the marker that distinguishes intentional retention
from a cleanup failure. A failed retention attempt may not have that manifest;
in that case `failure_capsule_error` in the run result is the recovery record.

## Safety model

The execution path is:

```text
Agent / Runner / Roam
        ↓
CapsuleExecutionEnvironment
        ↓
host PolicyAdapter
        ↓
provider-attested VM endpoint
        ↓
authenticated guest-agent request
        ↓
guest PolicyAdapter
        ↓
guest platform adapter
```

File transfer runs alongside that action path but is not an agent action:

```text
Test spec staging/collect declarations
        ↓
host path + size + SHA-256 policy
        ↓
authenticated bounded transfer
        ↓
per-session guest workspace
```

Model actions are schema/policy/capability checked before crossing into the VM
and are checked again by the actual adapter inside the guest. The model cannot
add staging sources, collection paths, or host destinations at runtime.

A Capsule isolates the host desktop, processes, registry, and writable guest
filesystem from the target. Argus does **not** yet claim complete network,
clipboard, shared-folder, transport-confidentiality, or secret isolation; those
policies are separate follow-up work.