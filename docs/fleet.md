# Argus Fleet — Distributed Capsule Execution and Observation

> **Status:** design specification. Argus Fleet is not yet implemented.

Argus Fleet extends the existing `ExecutionEnvironment` and Capsule architecture across multiple physical hosts on the same network (and later across routed/private networks or cloud infrastructure).

The goal is to let one central Argus Control Center discover/enroll execution nodes, schedule Capsule-backed test sessions, receive live evidence, and monitor many tests simultaneously without giving observers interactive control of the guest machines.

## Core idea

```text
                         Argus Control Center
                    +----------------------------+
                    | Fleet Registry             |
                    | Scheduler                  |
                    | Session Coordinator        |
                    | Evidence Aggregator        |
                    | Result / Artifact Index    |
                    | Observer API               |
                    +-------------+--------------+
                                  |
                    authenticated control plane
                                  |
              +-------------------+-------------------+
              |                   |                   |
              v                   v                   v
       Physical Host A      Physical Host B      Physical Host C
       Argus Node Agent     Argus Node Agent     Argus Node Agent
              |                   |                   |
        Hyper-V / KVM         Hyper-V / KVM        Hyper-V / KVM
          |       |              |       |             |
       Capsule  Capsule       Capsule  Capsule       Capsule
```

Argus Fleet is a layer **above**, not a replacement for, `ExecutionEnvironment` and `CapsuleExecutionEnvironment`.

## Design goals

- run Capsule-backed tests on many physical machines;
- schedule work according to host capabilities and available capacity;
- keep test specifications independent of a specific physical host;
- provide live monitoring of every active session;
- make the Observer surface structurally read-only;
- aggregate ATES events from all running sessions;
- support distributed matrix testing across OS/image/application combinations;
- preserve Capsule isolation and fail-closed behavior when execution becomes remote;
- avoid implicit fallback to local host execution when a remote placement request cannot be satisfied.

## Components

### Control Center

The Control Center is the authoritative coordination service.

Responsibilities should include:

- Node enrollment and identity;
- health/heartbeat tracking;
- capability registry;
- Capsule-image inventory metadata;
- scheduling and queues;
- session lifecycle coordination;
- test-plan and matrix expansion;
- ATES event aggregation;
- result and artifact indexing;
- Observer API;
- administrative API;
- audit trail for control-plane changes.

The Control Center should not require direct interactive desktop access to Nodes or guests.

### Node Agent

Each physical execution machine runs an Argus Node Agent.

A Node advertises capabilities such as:

```yaml
node_id: NODE-0007
hostname: test-host-03
host_os: windows
capsule_provider: hyperv
cpu_logical: 32
memory_mb: 131072
capacity:
  max_sessions: 8
  active_sessions: 3
images:
  - win11-qa
  - win10-legacy
```

A Linux Node could advertise `libvirt` and its Linux golden images instead.

The Agent is responsible for translating a centrally authorized session request into the local `ExecutionEnvironment` / Capsule provider APIs.

### Capsule

The existing Capsule remains the per-session isolation boundary.

Fleet must not weaken Capsule guarantees merely because allocation is remote.

The Node Agent should still enforce provider capabilities, secure guest control, staging/collection authority, networking policy, retention policy, and teardown locally.

### Observer

Observer is the read-only monitoring experience.

This is not just a UI with hidden buttons. Its identity and API authority should be incapable of mutating test sessions.

Observer may access operations equivalent to:

```text
GET fleet state
GET node health
GET session state
GET session timeline
GET live screen stream
GET ATES events
GET logs/artifact metadata permitted for the viewer
```

Observer credentials must not authorize equivalents of:

```text
click
keyboard input
arbitrary guest command
stop/restart session
change test specification
change node configuration
change network policy
```

## Control Center versus Observer

Argus should expose two security roles/surfaces even if they share frontend components.

### Control Center

Privileged operations:

- register/remove nodes;
- schedule/cancel tests;
- configure images and placement policies;
- manage queues;
- approve retention/discard operations;
- configure networking and capacity;
- manage access.

### Observer

Read-only operations:

- view fleet health;
- view active/queued/completed tests;
- watch a session;
- inspect current step/action/status;
- inspect ATES evidence allowed by policy;
- view results and findings.

Observer should receive a separate read-only credential and API surface.

## Live session monitoring

The monitoring view should show the complete guest screen when available while avoiding accidental interaction.

A session view may include:

```text
+---------------------------------------------------------------+
| CAPSULE-A21        LIVE        Windows 11 / Hyper-V          |
+---------------------------------------------------------------+
|                                                               |
|                     READ-ONLY GUEST VIEW                      |
|                                                               |
+---------------------------------------------------------------+
| Current step                                                   |
| 18 / 31: Verify network configuration                         |
|                                                               |
| Current normalized action                                     |
| click -> Button "Save"                                       |
|                                                               |
| elapsed 03:24 | tokens 4,812 | evidence profile standard      |
+---------------------------------------------------------------+
```

The viewer should not send mouse or keyboard input to the guest.

Argus may optionally overlay the **target selected by the agent** or show the normalized action alongside the screen. That is preferable to giving the viewer an interactive cursor.

## Screen transport

The exact screen transport is intentionally not fixed in this initial design.

Requirements:

- one-way or read-only semantics from the Observer's authorization perspective;
- no implicit remote-control channel;
- authenticated and encrypted transport in production;
- bandwidth-aware frame rate/quality controls;
- a mechanism to correlate frames with ATES timestamps/events;
- explicit redaction/policy support for sensitive applications where required.

Potential implementations should be evaluated later based on latency, cross-platform support, security model, and resource cost.

## Node enrollment and discovery

Fleet should support secure enrollment rather than trusting arbitrary network discovery.

Enrollment credentials are security-sensitive bootstrap secrets. They **must not be accepted as literal command-line argument values** because process listings, shell history, terminal logging, audit tooling, and diagnostic collectors can expose argv.

A conceptual flow:

```text
1. Administrator creates a short-lived, single-use enrollment credential scoped to the intended Control Center.
2. Administrator starts `argus-node join <control-center>` without placing the credential in argv.
3. Node authenticates the Control Center before releasing the enrollment credential.
4. The credential is supplied through a protected input path such as a hidden stdin prompt,
   restricted file descriptor/file, or OS credential mechanism.
5. Control Center validates and atomically consumes the enrollment credential.
6. Control Center assigns a durable Node identity and provisions long-term Node credentials.
7. Bootstrap credential material is erased from temporary storage as soon as practical.
8. Node establishes authenticated heartbeats/control sessions using its durable identity.
```

A non-interactive implementation may support a dedicated option such as `--token-file <path>` or an inherited file descriptor, but the file must be access-restricted and the secret value itself must never appear in argv. Environment variables should not be the default bootstrap mechanism because they may also be exposed by process/debugging facilities on some systems.

Enrollment credentials should be short-lived, single-use, audience-bound to the intended Control Center, and consumed atomically to reduce replay/race risk. A failed or ambiguous enrollment attempt must not silently mint multiple durable Node identities from one credential.

Optional mDNS/LAN discovery may help users find an unregistered Node, but discovery must not equal trust. Production execution requires explicit authenticated enrollment.

## Heartbeats and health

Nodes should periodically report:

- liveness;
- Agent version;
- provider availability;
- capacity and current load;
- disk pressure;
- image availability;
- virtualization/provider health;
- active session IDs;
- clock information sufficient to detect dangerous skew;
- degraded conditions.

Loss of the Control Center connection should have an explicit policy. Existing running Capsules should not be destroyed or abandoned based on an ambiguous network interruption without a defined reconciliation strategy.

## Scheduling

The scheduler should choose a Node based on explicit requirements and capabilities.

Possible placement requirements:

```yaml
execution:
  environment: fleet
  capsule:
    guest_os: windows
    image: win11-qa
  placement:
    labels:
      gpu: nvidia
    min_memory_mb: 8192
```

The scheduler should fail or queue when requirements cannot be met. It must not silently run the test locally on the Control Center.

## Matrix execution

Fleet should support expanding a logical test plan across environments.

Conceptual example:

```yaml
matrix:
  image:
    - win11-stable
    - win11-preview
    - ubuntu-24.04
  application_channel:
    - stable
    - beta
```

The Control Center expands that into independently identified runs, each with its own ATES record, while preserving a parent plan/matrix identity.

## Fleet timeline

When many sessions run at once, a video grid alone is insufficient.

A timeline view should summarize sessions and expose significant ATES events:

```text
22:01 ------------------------------------------------------ 22:12

A1  |==============================| PASS
A2  |====================..........| RUNNING
A3  |===========X                    | FAIL
B1       |======================| PASS
B2       |================..........| RUNNING
```

Selecting a failure can show:

- failed assertion;
- associated observations;
- checkpoint/failure screenshot;
- finding;
- collected artifacts;
- Failure Capsule retention state.

## ATES integration

Fleet should use ATES as its canonical execution-event vocabulary.

```text
Node/Capsule
    |
    +--> local ATES evidence
    |
    +--> authenticated event stream --> Control Center
                                         |
                                         +--> Observer
                                         +--> fleet timeline
                                         +--> final reports
                                         +--> audit package
```

The Control Center should not rewrite Node facts as though it observed them directly. Provenance must identify which Node/environment emitted each event.

Fleet transport must preserve the ATES event envelope: stable event IDs, monotonic per-run sequence numbers, idempotent retries, explicit gap detection, and conflict rejection. Canonical execution order comes from the producer's ATES sequence rather than network arrival order.

## Results and artifacts

The Control Center may index metadata centrally while large artifacts remain on Nodes or are uploaded to central/object storage according to deployment policy.

Any future central transfer protocol must preserve:

- artifact identity;
- digest verification;
- size limits/quotas;
- authorization;
- provenance;
- ATES manifest references;
- retention/deletion policy.

## Failure Capsules

Fleet should expose retained Failure Capsules as forensic resources, not as ordinary interactive remote desktops.

The existing rule remains important: retained secure Failure Capsules contain disk/config evidence but do not persist live recovery bearer tokens or TLS private keys merely to make them resumable.

Future discard/export workflows should use explicit privileged Control Center operations and be represented in the audit trail.

## Security principles

1. Node discovery is not authentication.
2. Node enrollment is explicit and authenticated.
3. Enrollment secrets never appear as literal argv values and are short-lived, single-use, and atomically consumed.
4. Observer authorization is read-only by construction.
5. Control Center privilege does not bypass Capsule guest policy accidentally.
6. Node Agents enforce local provider/capability constraints.
7. Remote placement never silently falls back to local execution.
8. All control-plane mutations are auditable.
9. ATES facts retain Node/session provenance and transport preserves ATES event identity/order.
10. Credentials and guest-control secrets are not emitted into ATES evidence.
11. Ambiguous distributed failures use reconciliation rather than destructive guesses.

## Initial implementation sequence

Recommended order:

1. define Node identity/capability models and enrollment credential semantics;
2. implement Node Agent daemon and authenticated enrollment using protected secret input rather than argv;
3. add heartbeat/capability registry to the Control Center;
4. implement remote session request/response around existing Capsule APIs;
5. add reconciliation for disconnect/restart scenarios;
6. add ATES event transport once ATES Core exists, preserving event ID/sequence retry semantics;
7. implement read-only Observer API;
8. implement live screen transport;
9. add scheduler/queue and placement requirements;
10. add matrix execution and fleet timeline;
11. add centralized artifact indexing/storage policies;
12. later consider cloud/autoscaling workers.

## Relationship to current Argus

Today:

```text
Runner -> ExecutionEnvironment -> Local or Capsule -> Adapter
```

Planned Fleet:

```text
Control Center
    -> Node Agent
        -> ExecutionEnvironment
            -> Capsule
                -> Adapter
```

This keeps the current execution boundary reusable instead of replacing it with a second remote-specific runner.