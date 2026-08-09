# Argus Capsules on Hyper-V

Argus Capsule mode runs the application under test inside a disposable Hyper-V
virtual machine instead of on the user's interactive Windows desktop.

## What PR3 provides

Each Argus session:

1. validates the configured Hyper-V switch and golden VHDX;
2. creates a per-session **differencing VHDX** whose parent is the golden image;
3. creates and boots a Generation-2 VM;
4. discovers the guest IPv4 address and attests any configured address against
   the addresses Hyper-V reports for that exact VM;
5. waits for the Argus guest agent;
6. launches/observes/drives the target through the guest agent;
7. destroys the VM and session disk when the session closes or launch fails.

The golden image is never booted directly by Argus.

## Current MVP boundary

PR3 intentionally does **not** stage an installer/source tree into the VM yet.
Until staging/collection lands, the application or command under test must
already exist in the golden image at the path/name passed to Argus.

The host may be locked while a Capsule runs. Host sleep, hibernate, shutdown, or
loss of Hyper-V still interrupts the VM. The **guest** interactive test user must
remain logged in and unlocked if physical GUI input is used.

## 1. Prepare a Windows golden image

Create a Windows 10/11 or Windows Server Generation-2 VHDX and install:

- Python 3.10+;
- Argus with the dependencies needed by the target adapter, for example
  `pip install -e .[windows]` or an installed Argus package;
- the application(s) you want to test during the PR3 MVP.

For GUI testing, configure a dedicated **non-administrator** test user to log in
automatically or otherwise reach an interactive unlocked desktop. Disable guest
sleep and screen locking for that disposable test account.

Hyper-V **Key-Value Pair Exchange** should remain enabled because PR3 obtains the
control endpoint from `Get-VMNetworkAdapter`. If `guest_address` is configured,
it is only a preferred candidate: Argus refuses it unless Hyper-V reports that
exact IPv4 address for the newly created VM.

## 2. Configure the guest-agent credential safely

Do **not** store `ARGUS_CAPSULE_GUEST_TOKEN` in the interactive test user's
persistent environment. Targets launched by normal Windows/CLI adapters inherit
the guest-agent process environment, so a persistent control-plane credential
would be visible to the application under test.

For the PR3 MVP, place the shared bootstrap token in a one-time file in the
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
Capsule. The target is launched only after the guest agent is ready, so the
ordinary application-under-test cannot read the consumed file through the test
user's normal filesystem view.

`--token-env` remains supported for supervisors that inject a **process-scoped**
environment variable at agent startup. The agent immediately removes that
variable from `os.environ` before any adapter or target can be created. Do not
use a User- or Machine-persistent environment variable for this purpose.

The service refuses a non-loopback bind without a token. The PR3 MVP still uses
a shared host/golden-image secret; a future bootstrap layer should replace this
with a per-session credential delivered over a host-bound channel.

After verifying the agent starts correctly, shut the VM down cleanly. Treat the
resulting VHDX as the golden parent and do not modify it while Capsules use it.

## 3. Use an Internal Hyper-V switch only

PR3's guest-agent protocol uses authenticated **HTTP**, not encrypted transport.
Therefore Argus accepts only a host-reachable **Internal** Hyper-V switch for
Capsule control traffic. Hyper-V's Default Switch is suitable only when it
reports as Internal on the host.

Argus rejects:

- **Private** switches, because the management OS cannot reach the guest agent;
- **External** switches, unconditionally, because sending the bearer credential
  over a LAN-visible plaintext HTTP channel would violate the control-plane
  boundary.

`allow_external_switch` is retained only as a reserved compatibility setting;
setting it to `true` is rejected in PR3. External networking must remain disabled
until Argus has a confidential host↔guest transport such as Hyper-V sockets or
TLS with appropriate authentication.

Network egress policy/isolation beyond this control-plane guard is intentionally
scheduled for a later isolation-policy PR.

## 4. Configure Argus on the host

Set the matching token in the dedicated **host** environment variable before
launching Argus:

```powershell
$env:ARGUS_CAPSULE_GUEST_TOKEN = "replace-with-a-long-random-secret"
```

The project configuration cannot select another host environment variable for
this credential. In particular, `.argus/config.yaml` cannot ask Argus to read
`AWS_*`, `GITHUB_TOKEN`, or any other arbitrary host secret. The only accepted
credential source in PR3 is `ARGUS_CAPSULE_GUEST_TOKEN`.

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
    # guest_address: 172.24.16.10  # optional; must be reported by Hyper-V for this VM
    boot_timeout_seconds: 120
    agent_timeout_seconds: 60
    allow_external_switch: false
```

A legacy `guest_token_env` entry is accepted only when its value is exactly
`ARGUS_CAPSULE_GUEST_TOKEN`; any attempt to select another host variable fails
configuration loading before a Capsule can be created.

`allow_external_switch` is parsed strictly: quoted values such as `"false"` are
normalized to false rather than becoming truthy through Python's normal string
truthiness. Any true value is rejected by the PR3 Hyper-V provider.

`guest_input_mode: physical` is intentional for Capsule mode: any synthetic
mouse/keyboard input occurs inside the VM, not on the host desktop. Set it to
`safe`/`semantic` if the guest application is fully accessible through UIA and
you want PR1's stricter semantic-only Windows behavior inside the guest too.

## Environment overrides

The host supports these overrides:

- `ARGUS_EXECUTION_ENVIRONMENT`
- `ARGUS_CAPSULE_PROVIDER`
- `ARGUS_CAPSULE_IMAGE`
- `ARGUS_CAPSULE_SWITCH`
- `ARGUS_CAPSULE_VM_ROOT`
- `ARGUS_CAPSULE_MEMORY_MB`
- `ARGUS_CAPSULE_CPU_COUNT`
- `ARGUS_CAPSULE_GUEST_PORT`
- `ARGUS_CAPSULE_GUEST_INPUT_MODE`
- `ARGUS_CAPSULE_GUEST_ADDRESS` (still provider-attested before use)
- `ARGUS_CAPSULE_BOOT_TIMEOUT_SECONDS`
- `ARGUS_CAPSULE_AGENT_TIMEOUT_SECONDS`
- `ARGUS_CAPSULE_ALLOW_EXTERNAL_SWITCH` (must remain false in PR3)

The guest control token itself is read directly from
`ARGUS_CAPSULE_GUEST_TOKEN`; there is no indirection variable that chooses a
second environment-variable name.

## Control-endpoint attestation

Project configuration is not trusted to choose the destination that receives a
bearer token. Before `GuestAgentClient` is constructed, `HyperVProvider` queries
`Get-VMNetworkAdapter -VMName <session VM>` and obtains the IPv4 addresses
reported for that exact VM. If a `guest_address` candidate is supplied, it must
match one of those addresses exactly. A mismatched or unrelated address aborts
the Capsule and triggers normal VM cleanup **before any HTTP request or bearer
credential is sent**.

## Cleanup and recovery

Argus removes the registered VM before deleting its per-session storage. If VM
deregistration fails, Argus **preserves the session directory and child VHDX**
and reports their path in the cleanup error. This avoids turning a recoverable
registered/orphaned VM into one whose backing files were already deleted.
Storage is removed only after VM removal succeeds.

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

So model actions are schema/policy/capability checked before crossing into the
VM and are checked again by the actual adapter inside the guest.

A Capsule isolates the host desktop, processes, registry, and writable guest
filesystem from the target. PR3 does **not** yet claim complete network,
clipboard, shared-folder, or secret isolation; those policies are separate
follow-up work.
