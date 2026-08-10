# Argus Documentation

Argus is evolving from a single-host autonomous testing tool into a production-oriented testing platform with explicit execution boundaries, disposable virtual-machine Capsules, auditable evidence, and distributed execution.

This directory separates **implemented behavior** from **planned architecture** so users can tell what is available today and what is being designed next.

## Start here

### Execution environments

Argus separates **how a target is driven** from **where the test executes**.

| Layer | Purpose | Current options |
|---|---|---|
| Adapter | How Argus interacts with the target | `desktop-gui`, `browser`, `cli` |
| Execution environment | Where the adapter and target execute | `local`, `capsule` |

### Recommended production mode: Capsules

For production, destructive, exploratory, or otherwise high-risk testing, use an **Argus Capsule**. A Capsule runs the test inside a disposable VM rather than directly on the operator's interactive host.

Current providers:

| Provider | Host | Guest | Status |
|---|---|---|---|
| Hyper-V | Windows | Windows | Implemented |
| libvirt/QEMU/KVM | Linux | Linux | Implemented |
| `auto` | Windows or Linux | matching supported guest | Implemented |
| Apple Virtualization | macOS | macOS | Not implemented |

Read:

- [Hyper-V Capsules](capsules-hyperv.md)
- [Multi-OS Capsules and Linux libvirt](capsules-multi-os.md)

Capsules provide the production isolation boundary. Their design includes secure guest control, explicit staging and artifact collection, provider capability checks, isolated networking, and optional forensic Failure Capsule retention.

### Lightweight mode: local execution

`local` remains the compatibility default and is useful for development, quick checks, and environments where virtualization is unavailable.

Local execution is **shared and non-isolated**. On Windows desktop tests, Argus defaults to target-constrained semantic UI Automation rather than intentional host-wide physical mouse/keyboard injection. Legacy physical input exists only as an explicit opt-in and should be treated as a higher-risk mode.

## Architecture shipped through PRs #8–#14

The current execution architecture includes:

1. a centralized action schema and global execution policy;
2. safe semantic Windows desktop input by default;
3. an `ExecutionEnvironment` boundary separating placement from adapters;
4. disposable Hyper-V Capsules;
5. optional forensic Failure Capsule retention;
6. explicit host-to-guest staging and guest-to-host artifact collection;
7. pinned HTTPS control, per-session bearer rotation, network isolation, and Hyper-V side-channel restrictions;
8. provider-aware multi-OS Capsules with Linux libvirt/QEMU/KVM support.

These capabilities are implemented today.

## Next architecture

Two specifications are being designed as the next layer above Capsules:

### [ATES — Argus Test Evidence Specification](ates.md)

ATES defines an **always-on canonical evidence model** for every Argus execution. It standardizes what Argus records, how observations are distinguished from AI interpretations, how checkpoints and failures capture evidence, how provenance and integrity are represented, and how reports are generated from the canonical record.

ATES is intended to be Argus-native and open. Future ISO/IEC/IEEE or industry-specific compatibility will be implemented as optional mappings rather than making Argus dependent on proprietary standards.

**Status: specification / not implemented yet.**

### [Argus Fleet](fleet.md)

Argus Fleet defines distributed Capsule execution across multiple physical hosts on a network. A central Control Center schedules work, receives evidence and health events, and provides a separate read-only Observer experience for live monitoring.

**Status: specification / not implemented yet.**

## Architectural direction

```text
Argus Control Center                 (planned Fleet)
        |
        v
Argus Node Agent                     (planned Fleet)
        |
        v
ExecutionEnvironment                 (implemented)
    |           |
    |           +--> Capsule         (implemented)
    |                  |
    |                  v
    |              Guest Agent
    |                  |
    |                  v
    |               Adapter
    |
    +--------------> Local
                       |
                       v
                    Adapter

Every execution
        |
        +--> ATES canonical evidence (planned)
                 |
                 +--> reports
                 +--> read-only monitoring
                 +--> audit packages
                 +--> compliance mappings
```

## Principle

Argus should make three concerns independent:

1. **Test specification** — what should be tested.
2. **Execution specification** — where and under which isolation policy it may run.
3. **Evidence specification** — what Argus must prove happened.

Keeping those layers separate lets the same test run locally, in a Capsule, or eventually across a Fleet without changing the meaning of the test itself.
