# Argus Capsules on Hyper-V

Argus Capsule mode runs the application under test inside a disposable Hyper-V
virtual machine instead of on the user's interactive Windows desktop.

## What PR3 provides

Each Argus session:

1. validates the configured Hyper-V switch and golden VHDX;
2. creates a per-session **differencing VHDX** whose parent is the golden image;
3. creates and boots a Generation-2 VM;
4. discovers the guest IPv4 address (or uses a configured static address);
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

For GUI testing, configure a dedicated test user to log in automatically or
otherwise reach an interactive unlocked desktop. Disable guest sleep and screen
locking for that disposable test account.

Hyper-V **Key-Value Pair Exchange** should remain enabled if Argus is expected
to discover the guest IP automatically. You may instead configure a fixed
`guest_address`.

## 2. Configure the guest agent

Set a strong token in the guest test-user environment, for example:

```powershell
[Environment]::SetEnvironmentVariable(
  "ARGUS_CAPSULE_GUEST_TOKEN",
  "replace-with-a-long-random-secret",
  "User"
)
```

Start the guest agent in that same interactive user session at logon:

```powershell
python -m argus.capsule.guest_agent `
  --host 0.0.0.0 `
  --port 8765 `
  --token-env ARGUS_CAPSULE_GUEST_TOKEN
```

The service refuses a non-loopback bind without a token. For the MVP the host
and golden image share this token; later bootstrap work can replace that with a
per-session credential.

After verifying the agent starts correctly, shut the VM down cleanly. Treat the
resulting VHDX as the golden parent and do not modify it while Capsules use it.

## 3. Use a host-reachable Hyper-V switch

An **Internal** Hyper-V switch is recommended. It lets the Windows management OS
reach the guest agent without directly bridging the guest onto the physical LAN.
Hyper-V's Default Switch is normally suitable when present.

Argus rejects a Private switch because the host cannot reach the guest agent.
It also rejects an External switch by default. External networking requires
`allow_external_switch: true` and should be an explicit decision.

Network egress policy/isolation beyond this guard is intentionally scheduled for
a later isolation-policy PR.

## 4. Configure Argus on the host

Set the same token in the host environment:

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
    guest_token_env: ARGUS_CAPSULE_GUEST_TOKEN
    guest_input_mode: physical
    boot_timeout_seconds: 120
    agent_timeout_seconds: 60
    allow_external_switch: false
```

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
- `ARGUS_CAPSULE_GUEST_TOKEN_ENV`
- `ARGUS_CAPSULE_GUEST_INPUT_MODE`
- `ARGUS_CAPSULE_GUEST_ADDRESS`
- `ARGUS_CAPSULE_BOOT_TIMEOUT_SECONDS`
- `ARGUS_CAPSULE_AGENT_TIMEOUT_SECONDS`
- `ARGUS_CAPSULE_ALLOW_EXTERNAL_SWITCH`

## Safety model

The execution path is:

```text
Agent / Runner / Roam
        ↓
CapsuleExecutionEnvironment
        ↓
host PolicyAdapter
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
