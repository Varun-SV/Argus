# Environment Matrix and Adaptive VM Test Lab

Status: **planned; documentation only**

Implementation gate: **do not begin implementation until current PR #27 and current PR #28 are merged.**

- PR #27: ISO-backed OS environment provisioning
- PR #28: Claude-style desktop GUI and website redesign

This document records the intended design so the capability is not lost while those pull requests are completed. It is not an implementation contract yet; details may be refined against the merged APIs, but the architectural boundaries below should be preserved.

## Goal

Allow one Argus test plan to execute the same application and test cases across multiple operating systems and virtual hardware configurations without requiring the operator to manually create each VM.

A user should be able to express requirements such as:

- Windows 11, Ubuntu 24.04, or other supported immutable OS images;
- 2, 4, or 8 vCPUs;
- 4 GiB, 8 GiB, or 16 GiB RAM;
- disk size and supported virtual-device choices where meaningful;
- firmware, Secure Boot, TPM, architecture, and network constraints inherited from the environment contract;
- one or more application versions and test specifications.

Argus should expand the requested combinations into independent test cases, schedule as many disposable VMs as the available hardware can safely support, queue the remainder, execute the existing Capsule/test path, collect canonical ATES evidence, and destroy or retain each VM according to the existing Capsule policy.

## Architectural rule

**Do not reinstall an ISO for every matrix case.**

PR #27 is responsible for converting a verified operator-supplied ISO plus an installation/machine definition into a verified immutable base image.

Environment Matrix should consume those immutable images.

```text
operator ISO
    |
    v
PR #27 provisioning
    |
    v
verified immutable image
    |
    +-----------------------------+
    |                             |
    v                             v
matrix case A                 matrix case B
4 GiB / 2 vCPU               8 GiB / 8 vCPU
    |                             |
    v                             v
disposable Capsule clone     disposable Capsule clone
    |                             |
    +-------------+---------------+
                  v
              Argus test
                  |
                  v
                ATES
```

Image identity and OS installation identity remain stable. Runtime hardware allocation is a separate execution concern.

## Relationship to existing Argus layers

The feature must reuse, not replace, the existing boundaries:

```text
Test specification
      |
      v
Environment Matrix plan
      |
      v
Scheduler / capacity planner
      |
      v
ExecutionEnvironment
      |
      v
Capsule
      |
      v
Guest Agent
      |
      v
Adapter
      |
      v
Application under test

Every matrix case
      |
      v
independent ATES Run
```

The matrix layer must not create a second VM execution path, a second evidence format, or a second remote-execution protocol.

## Proposed user model

A future test-plan representation may look conceptually like:

```yaml
application:
  installer: MyApplication.exe

environments:
  os:
    - windows-11-24h2
    - ubuntu-24.04

matrix:
  cpu_count: [2, 4, 8]
  memory_mb: [4096, 8192, 16384]

parallelism: auto

tests:
  - install
  - startup
  - functional
  - stress
  - performance
```

The exact schema should be decided only after #27 and #28 merge so it can use their final public APIs and UX rather than introducing temporary compatibility layers.

## Matrix identity

Each expanded case needs a stable identity derived from the parent plan plus the immutable environment/image identity and requested runtime configuration.

A case should be reproducible and distinguishable from every other case even when it runs on another Fleet Node.

Conceptually:

```text
MatrixPlan
  plan_id
  application identity
  test identities
  case definitions

MatrixCase
  case_id
  parent plan_id
  immutable image identity
  runtime machine request
  execution policy
  test identity
```

The case identity must not depend on which physical Node happened to execute it.

## Runtime machine request

The runtime request should use the capabilities already established by the environment model where applicable, including:

- architecture;
- vCPU count;
- memory;
- disk requirements;
- firmware;
- Secure Boot;
- TPM;
- network mode.

The implementation should explicitly distinguish:

1. **image-build constraints** — properties that must be true when the immutable image is created;
2. **runtime allocation** — resources that may vary safely between disposable clones;
3. **provider capabilities** — what Hyper-V, libvirt, or future providers can actually satisfy.

A case must fail closed as unsatisfied if its requested configuration cannot be represented safely by the selected provider.

## Adaptive local scheduling

`parallelism: auto` should not mean "start everything."

Argus should calculate a safe concurrency limit from currently available host resources and configured safety reserves.

Example:

```text
Host capacity
  32 GiB RAM
  16 logical CPUs

Matrix
  18 cases

Runnable now
  VM-01  4 GiB / 2 vCPU
  VM-02  8 GiB / 4 vCPU
  VM-03  4 GiB / 2 vCPU
  VM-04  8 GiB / 4 vCPU

Queued
  remaining cases
```

When a VM reaches a terminal state, Argus finalizes its evidence, releases its reservation, tears it down according to policy, and admits another queued case.

### Scheduler requirements

The scheduler should eventually account for at least:

- total and available memory;
- CPU allocation and an operator-configurable oversubscription policy;
- required provider;
- immutable image availability;
- disk capacity;
- architecture;
- firmware/Secure Boot/TPM requirements;
- network policy;
- per-host concurrency limits;
- existing Fleet reservations and running sessions.

The scheduler must preserve existing placement fencing, cancellation, and capacity-reservation rules. Matrix execution must not create a bypass around Fleet ownership.

## Local and Fleet execution

The same Matrix Plan should work on one machine or across a Fleet.

### Single host

Argus runs as many cases concurrently as the host can safely support and queues the rest.

### Fleet

The Control Center expands the plan once, then places individual Matrix Cases onto eligible Nodes.

```text
                         Matrix Plan
                             |
               +-------------+-------------+
               |             |             |
               v             v             v
             Node A        Node B        Node C
             VM VM         VM VM         VM VM VM
```

The scheduling decision may vary between runs. The Matrix Case identity and its ATES evidence must not.

## Evidence model

Each case is a normal Argus execution and therefore gets its own canonical ATES Run.

A parent matrix result should aggregate references; it should not rewrite or flatten canonical child evidence.

```text
Matrix Plan
   |
   +-- Case A -> ATES Run A
   +-- Case B -> ATES Run B
   +-- Case C -> ATES Run C
   +-- Case D -> ATES Run D
```

The aggregate view may summarize:

- pass/fail/error/cancelled;
- OS/image identity;
- requested and actual VM configuration;
- application version;
- duration;
- findings;
- performance measurements explicitly produced by the tests;
- artifact/report links;
- Node/provider used;
- evidence verification/trust state.

Clicking or opening a matrix cell should lead back to the full evidence for that exact run.

## Result matrix

A user-facing result may eventually look like:

| OS / configuration | 4 GiB / 2 vCPU | 8 GiB / 4 vCPU | 8 GiB / 8 vCPU |
|---|---:|---:|---:|
| Windows 11 24H2 | FAIL | PASS | PASS |
| Ubuntu 24.04 | PASS | PASS | PASS |

This is an aggregate presentation only. The canonical result remains the ATES evidence for each child run.

## Environment Discovery mode

After deterministic matrix execution is stable, Argus may add an optional discovery mode:

> Find the lowest-resource environment in which these acceptance tests pass.

Rather than brute-force every configuration, Argus may adaptively explore the resource space.

Example:

```text
16 GiB / 8 vCPU -> PASS
 8 GiB / 8 vCPU -> PASS
 4 GiB / 8 vCPU -> FAIL
 8 GiB / 4 vCPU -> PASS
 8 GiB / 2 vCPU -> FAIL
```

This can produce an evidence-backed compatibility envelope.

Important: Argus should distinguish factual observations from product recommendations. A future report can state, for example:

- lowest tested passing configuration;
- configurations that failed the supplied acceptance tests;
- measured performance at each tested point.

Any label such as "recommended configuration" should only be emitted when the test plan contains an explicit operator-defined recommendation policy.

## Proposed implementation phases

Implementation should begin only after current #27 and #28 are merged.

### Phase 1 — Matrix plan and deterministic expansion

- define MatrixPlan and MatrixCase identities;
- reference immutable environment/image identities from #27;
- add schema validation;
- deterministically expand OS/resource/application/test combinations;
- no new VM backend.

### Phase 2 — Disposable runtime variants

- clone/reuse the immutable image without reinstalling the ISO;
- apply allowed per-case CPU/RAM/runtime settings;
- validate provider capabilities before launch;
- preserve the existing ExecutionEnvironment -> Capsule path;
- guarantee cleanup and existing failure-retention behavior.

### Phase 3 — Adaptive scheduler and queue

- capacity reservations;
- safe local concurrency;
- queue/backpressure;
- explicit unsatisfied cases;
- cancellation;
- no silent fallback that changes isolation or provider requirements.

### Phase 4 — Fleet placement

- schedule Matrix Cases across eligible Nodes;
- reuse Fleet ownership/generation/fencing;
- reconcile disconnects without duplicate execution;
- retain case identity when placement changes.

### Phase 5 — ATES matrix aggregation and UX

- one canonical ATES Run per case;
- parent plan index;
- matrix/timeline result view;
- filters by OS/configuration/status;
- links to exact evidence, artifacts, screenshots, and reports;
- integrate the UX with the post-#28 desktop application rather than adding a parallel interface.

### Phase 6 — Environment Discovery

- optional adaptive search;
- operator-defined bounds;
- reproducible search decisions;
- evidence-backed compatibility envelope;
- no unsupported inference beyond configurations actually tested.

## Failure and recovery expectations

Matrix execution introduces additional failure modes. The implementation must define behavior for:

- VM create failure;
- image missing/corrupt;
- insufficient capacity after reservation;
- guest-agent bootstrap failure;
- Node disconnect;
- scheduler restart;
- duplicate dispatch/replay;
- cancellation during queue, provisioning, execution, or teardown;
- partial matrix completion.

A parent Matrix Plan must be resumable/reconcilable without rerunning already completed cases unless the operator requests it or their identity/input changed.

## Security and isolation constraints

The feature must preserve Argus's existing trust boundaries:

- no silent fallback from Capsule to local execution;
- no reuse of a mutable VM as the canonical starting state for unrelated cases;
- guest bootstrap secrets remain per-session and must not be baked into base images;
- matrix configuration must not weaken #27 image verification;
- provider/network/security requirements fail closed;
- ATES privacy and artifact controls remain authoritative;
- Fleet Observer remains read-only and cannot become a control path through matrix UX.

## Non-goals for the first implementation

The first implementation does not need to:

- invent a new hypervisor;
- support arbitrary live VM hardware mutation;
- perform ISO installation for every case;
- auto-tune the host by changing its OS/hypervisor settings;
- claim every configuration between two tested points is supported;
- infer a vendor's official minimum/recommended requirements;
- replace ATES with a matrix-specific evidence format.

## Acceptance criteria for the first usable version

A first usable Environment Matrix implementation should prove that:

1. one test definition can expand into multiple OS/resource Matrix Cases;
2. cases start from verified immutable images created through the #27 path;
3. Argus runs cases concurrently only within explicit capacity limits and queues the rest;
4. each case executes through the existing Capsule/Adapter path;
5. each case produces independently verifiable ATES evidence;
6. teardown releases capacity even after failures;
7. interrupted scheduling can reconcile without duplicate execution;
8. a parent result view shows every case and links to its exact evidence;
9. the same plan can later be placed across Fleet Nodes without changing test semantics.

## Implementation trigger

Do not start implementation merely because this planning PR is merged.

The intended trigger is:

```text
current PR #27 merged
        +
current PR #28 merged
        |
        v
re-evaluate merged APIs
        |
        v
split this plan into implementation PR(s)
```

At that point, review this document against the final #27 provisioning API, the final #28 desktop UX/API, and the then-current Fleet scheduler/execution contracts before writing code.
