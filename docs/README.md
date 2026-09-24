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

Capsules provide the production isolation boundary. Their design includes secure guest control, explicit staging and artifact collection, provider capability checks, isolated networking, and optional forensic Failure Capsule retention for supported scripted `argus run` failure paths. Current `argus roam` findings do not trigger the failure-recording lifecycle hook, so roam-triggered Capsule retention is not currently guaranteed.

### Lightweight mode: local execution

`local` remains the compatibility default and is useful for development, quick checks, and environments where virtualization is unavailable.

Local execution is **shared and non-isolated**. On Windows desktop tests, Argus defaults to target-constrained semantic UI Automation rather than intentional host-wide physical mouse/keyboard injection. Legacy physical input exists only as an explicit opt-in and should be treated as a higher-risk mode.

## Architecture shipped through PRs #8–#14

The current execution architecture includes:

1. a centralized action schema and global execution policy;
2. safe semantic Windows desktop input by default;
3. an `ExecutionEnvironment` boundary separating placement from adapters;
4. disposable Hyper-V Capsules;
5. optional forensic Failure Capsule retention for supported scripted `argus run` failure paths;
6. explicit host-to-guest staging and guest-to-host artifact collection;
7. pinned HTTPS control, per-session bearer rotation, network isolation, and Hyper-V side-channel restrictions;
8. provider-aware multi-OS Capsules with Linux libvirt/QEMU/KVM support.

These capabilities are implemented today. Failure Capsule retention should not be inferred for execution paths that do not currently call the failure-recording hook, including `argus roam` findings.

## Next architecture

Two specifications are being designed as the next layer above Capsules:

### [ATES — Argus Test Evidence Specification](ates.md)

ATES is the **implemented canonical evidence authority** for Argus runs. ATES v0.1 includes durable ordered events, runtime lifecycle evidence, durable action dispatch, evidence privacy, protected artifacts, transactional finalization, manifests/verification, derived reports, requirement traceability, and approval/audit records.

**Status: ATES v0.1 implemented.**

### [Argus Fleet](fleet.md)

Argus Fleet extends the existing Capsule boundary across physical machines. The current execution plane implements Node enrollment/identity, authenticated heartbeats and capability advertisements, clock assessment, durable placement/fencing, remote Capsule execution, and canonical ATES transport/reconciliation.

**Status: Fleet execution plane implemented. Fleet operations and the strictly read-only Observer are follow-on work.**

### [Release engineering](releasing.md)

Argus CI now validates Windows, Linux, and macOS across x64/ARM64 where supported. Production releases build Windows portable/EXE/MSI packages, a macOS universal2 app/DMG, Linux AppImage/DEB/RPM/Arch packages, and Python distributions from the same tagged source.

**Status: automated cross-platform packaging/release pipeline.**

## Architectural direction

```text
Argus Control Center
        |
        v
Argus Node Agent                     (Fleet execution plane)
        |
        v
ExecutionEnvironment
    |           |
    |           +--> Capsule
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
        +--> ATES canonical evidence
                 |
                 +--> reports / audit
                 +--> Fleet aggregation
                 +--> future read-only Observer
```

## Principle

Argus should make three concerns independent:

1. **Test specification** — what should be tested.
2. **Execution specification** — where and under which isolation policy it may run.
3. **Evidence specification** — what Argus must prove happened.

Keeping those layers separate lets the same test run locally, in a Capsule, or eventually across a Fleet without changing the meaning of the test itself.
