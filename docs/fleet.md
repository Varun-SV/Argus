# Argus Fleet — Distributed Capsule Execution and Observation

> **Status:** design specification. Argus Fleet is not yet implemented.

Argus Fleet extends the existing `ExecutionEnvironment` and Capsule architecture across multiple physical hosts on the same network (and later across routed/private networks or cloud infrastructure).

The goal is to let one central Argus Control Center enroll execution nodes, schedule Capsule-backed test sessions, receive live evidence, and monitor many tests simultaneously without giving observers interactive control of guest machines.

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
                    authenticated encrypted plane
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
- pin each scheduled Capsule image to an immutable content identity rather than trusting a mutable alias alone;
- bind every staged input to immutable content identity before remote dispatch;
- make remote session creation retry-idempotent so ambiguous network failures cannot launch a destructive test twice;
- persist global placement ownership so Control Center failover cannot redispatch one logical session to two Nodes;
- retain terminal allocation tombstones until the corresponding placement generation is durably retired, so delayed authenticated requests cannot resurrect completed work;
- persist cancellation in globally authoritative coordination state before acknowledging the request so coordinator failure cannot resurrect dispatch;
- make cancellation causally safe so a delayed allocation cannot start after the logical session was cancelled where the owner has observed the cancellation/fence;
- make Node-side admission authoritative and atomic so concurrent placements cannot overcommit advertised capacity;
- preserve producer timestamps while recording a trusted Control Center receipt/clock-offset basis so cross-Node timelines do not silently misorder skewed clocks;
- provide live monitoring of every active session;
- make the Observer surface structurally read-only;
- aggregate ATES events from all running sessions;
- support distributed matrix testing across OS/image/application combinations;
- preserve Capsule isolation and fail-closed behavior when execution becomes remote;
- require authenticated encryption for Fleet control, evidence/event, screen, and artifact traffic in production;
- avoid implicit fallback to local host execution when a remote placement request cannot be satisfied.

## Components

### Control Center

The Control Center is the authoritative coordination service.

Responsibilities should include:

- Node enrollment and identity;
- health/heartbeat tracking;
- capability registry;
- Capsule-image inventory metadata and immutable image identities;
- scheduling and queues;
- session lifecycle coordination and idempotent allocation requests;
- durable global placement ownership/fencing state, cancellation state, and generation retirement;
- immutable staged-input manifests for remote sessions;
- trusted receipt-time and Node clock-offset/uncertainty metadata for cross-Node evidence correlation;
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
  - alias: win11-qa
    image_id: IMG-WIN11-QA-2026-08
    digest: "sha256:..."
  - alias: win10-legacy
    image_id: IMG-WIN10-LEGACY-2026-06
    digest: "sha256:..."
```

A Linux Node could advertise `libvirt` and its Linux golden images instead.

Human-friendly image aliases are discovery/configuration conveniences, not sufficient execution identity. Every schedulable image advertisement must include an **immutable image identity**, preferably a cryptographic digest of the immutable golden-image content or an immutable version identifier that resolves to such a digest. Nodes advertising the same alias with different immutable identities are different execution baselines and must not be treated as interchangeable.

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

- register/remove Nodes;
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

## Fleet transport security

Production Fleet communication must use an **authenticated and encrypted channel**. TLS with mutually authenticated Node credentials is the expected baseline, although an equivalent transport may be used if it provides the same security properties.

The confidentiality/integrity requirement applies to:

- Node enrollment and credential provisioning;
- heartbeats and capability advertisements;
- privileged control/session requests;
- ATES event transport;
- live screen/frame transport;
- logs and result metadata;
- artifact upload/download;
- administrative and Observer API traffic according to role.

An authenticated-but-plaintext implementation is not a conforming production Fleet transport. Authorization and artifact hashes do not replace transport confidentiality.

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

### Enrollment identity and idempotency

Before sending a bootstrap credential, a Node should generate:

- a long-term Node keypair (or equivalent hardware/OS-backed identity key);
- a stable random `enrollment_request_id` for this logical enrollment attempt;
- a request containing the public-key fingerprint and intended Control Center identity.

A conceptual flow:

```text
1. Administrator creates a short-lived, single-use enrollment credential scoped to the intended Control Center.
2. Node generates its keypair and stable enrollment_request_id.
3. Administrator starts `argus-node join <control-center>` without placing the credential in argv.
4. Node authenticates the Control Center before releasing the enrollment credential.
5. The credential is supplied through a protected input path such as hidden stdin,
   restricted file descriptor/file, or OS credential mechanism.
6. Control Center validates the request and, in one durable transaction:
     - binds enrollment_request_id to the Node public-key fingerprint;
     - allocates the durable Node identity;
     - records the credential/provisioning result;
     - consumes the bootstrap credential.
7. Control Center returns the durable enrollment result.
8. Node proves possession of the enrolled private key and acknowledges successful provisioning.
9. Node establishes authenticated heartbeats/control sessions using the durable identity.
```

A non-interactive implementation may support a dedicated option such as `--token-file <path>` or an inherited file descriptor, but the file must be access-restricted and the secret value itself must never appear in argv. Environment variables should not be the default bootstrap mechanism because they may also be exposed by process/debugging facilities on some systems.

### Lost-response recovery

Enrollment completion must be **idempotent** across ambiguous network failures.

If the Control Center has consumed the bootstrap credential and persisted the durable Node identity but the provisioning response is lost:

- the Node retries with the same `enrollment_request_id` and public key;
- the Control Center must not create a second Node identity;
- the Node proves possession of the matching private key;
- the Control Center returns/replays the same pending durable enrollment result until the Node explicitly acknowledges successful provisioning, or until an authenticated administrative recovery/revocation operation definitively closes that pending enrollment;
- a reused request ID with a different key fingerprint is rejected as a protocol/security error;
- replacing the bootstrap token must not silently create another identity for the same pending logical request.

Pending enrollment state **must not expire merely because a recovery timer elapsed** after the bootstrap credential was consumed. The Control Center must retain enough durable state to recover the same Node identity/result after restart or long disconnection. If an operator needs to abandon a never-acknowledged enrollment, that requires an authenticated, auditable revoke/recover operation that invalidates the pending durable identity before a replacement enrollment can be accepted.

A recovery operation may issue a replacement credential/certificate only after the Node proves possession of the originally enrolled private key, or after an authenticated administrator explicitly revokes that identity. It must not depend on the already-consumed bootstrap secret becoming valid again.

The replayable enrollment result must not expose reusable bootstrap material. Credential-delivery design should prefer issuing/deriving durable credentials in a way that can be safely recovered by proof of the enrolled private key, rather than depending on retransmitting a plaintext private credential.

Enrollment credentials should remain short-lived, single-use, and audience-bound to the intended Control Center. Atomic consumption prevents token replay; request/key binding plus acknowledgment-persistent recovery state prevents orphaned identities when a response is lost.

Optional mDNS/LAN discovery may help users find an unregistered Node, but discovery must not equal trust. Production execution requires explicit authenticated enrollment.

## Heartbeats and health

Nodes should periodically report:

- liveness;
- Agent version;
- provider availability;
- capacity and current load;
- disk pressure;
- image availability including immutable image identity/digest;
- virtualization/provider health;
- active session IDs;
- Node wall-clock timestamp plus clock synchronization source/status where available;
- monotonic-clock sample/reference suitable for measuring intervals/restarts where available;
- degraded conditions.

The Control Center should measure and retain enough timing metadata to estimate Node-to-Control-Center clock offset and uncertainty, for example using authenticated request/response timing samples and observed round-trip bounds. Merely accepting a Node's self-reported wall clock as globally correct is not sufficient.

Loss of the Control Center connection should have an explicit policy. Existing running Capsules should not be destroyed or abandoned based on an ambiguous network interruption without a defined reconciliation strategy.

## Trusted Fleet time basis

ATES per-run `sequence` remains the authoritative ordering **within one run**. Fleet, however, may display events from different Nodes on one wall-clock timeline, so cross-Node correlation needs an explicit trust model.

Every ATES event received by the Control Center should preserve the producer's original `occurred_at` unchanged and record aggregation/transport metadata separately, conceptually:

```yaml
producer_time:
  occurred_at: "2026-08-11T10:20:00.120+05:30"
central_receipt:
  received_at: "2026-08-11T10:20:00.287+05:30"
clock_assessment:
  offset_estimate_ms: 132
  uncertainty_ms: 24
  sample_age_ms: 1800
  sync_status: healthy
```

The exact wire/storage shape may evolve, but these semantics are required:

- `occurred_at` remains a Node-produced fact and is never silently rewritten to look like a trusted central timestamp;
- the Control Center records an authenticated local `received_at` timestamp when it accepts the event;
- clock offset estimates carry an uncertainty/error bound and measurement/sample age, not only a point estimate;
- offset/uncertainty metadata is associated with the relevant Node/time interval so later reports can determine what timing knowledge was available;
- Node clock steps, restarts, synchronization loss, excessive drift, or stale offset samples mark the affected cross-Node timing as degraded until a new trustworthy assessment is obtained;
- receipt time may establish when the Control Center observed an event but does not prove the exact time the event happened before network delay;
- producer times adjusted by an offset estimate may be used for visualization only when the uncertainty is acceptable for the requested comparison;
- if two adjusted event-time intervals overlap within their uncertainty bounds, Fleet must not claim a definite causal/temporal order between those cross-Node events merely to draw a tidy timeline;
- when skew/uncertainty exceeds policy thresholds, dashboards/reports visibly qualify timing as approximate/degraded or fall back to receipt-time presentation rather than silently presenting precise global order;
- audit/report exports retain the producer timestamp, central receipt timestamp, and relevant offset/uncertainty assessment so the cross-Node ordering claim can be independently evaluated later.

For example, if Node A's adjusted event is `10:20:00.200 ±40 ms` and Node B's is `10:20:00.230 ±50 ms`, the intervals overlap and Fleet cannot truthfully assert that A happened before B. Per-run sequence ordering remains exact for each individual producer even when cross-Node wall-clock order is uncertain.

This timing metadata is aggregation/provenance evidence. The Control Center must not rewrite a Node's canonical ATES event as though the central service directly observed the underlying guest action.

## Scheduling

The scheduler should choose a Node based on explicit requirements and capabilities.

Possible placement requirements:

```yaml
execution:
  environment: fleet
  capsule:
    guest_os: windows
    image: win11-qa
    image_digest: "sha256:..."
  placement:
    labels:
      gpu: nvidia
    min_memory_mb: 8192
```

The human-friendly `image` alias may be used to select a desired baseline, but the Control Center must resolve it to an immutable image identity **before dispatch**. The session request sent to the Node must carry that immutable identity/digest. Before allocating a Capsule, the Node must verify that its local golden image matches the requested identity. A missing or mismatched image fails/queues the request; it must never silently substitute another image with the same alias.

The immutable image identity used for the run must be recorded in session metadata and ATES provenance so reports can distinguish otherwise similarly named baselines.

The scheduler should fail or queue when requirements cannot be met. It must not silently run the test locally on the Control Center.

### Node-side admission and capacity reservation

Heartbeat capacity is **advisory scheduling data**, not an allocation guarantee. Concurrent Control Center decisions may be based on the same stale heartbeat, so the Node Agent is the final authority for local admission.

Before creating or starting a Capsule, the Node must atomically and durably reserve the requested execution capacity for the `session_request_id`. At minimum this includes a session slot against `max_sessions`; implementations should also account for enforced memory, disk, GPU, or provider-specific limits where those resources participate in placement.

Required semantics:

- admission check and reservation are one local atomic operation with respect to competing allocation requests;
- a reservation is keyed by `session_request_id`, so an idempotent retry reuses the same reservation rather than consuming another slot;
- if capacity is unavailable, the Node rejects or explicitly queues the allocation without creating/starting a Capsule;
- the Control Center must accept a Node-side `capacity_unavailable` result even if its most recent heartbeat advertised free capacity, and may reschedule only as a new logical placement according to policy;
- the reservation is held through allocation/start and is released only when the session reaches a defined terminal/released state or an allocation abort is durably completed;
- Node restart/reconciliation must recover outstanding reservations sufficiently to avoid double-admission after restart.

This prevents two concurrent placements from both consuming the same advertised free slot and protects already-running sessions from accidental overcommit.

## Remote session allocation idempotency

Remote Capsule creation is a destructive side effect and must be **retry-idempotent before Fleet remote execution is considered implementable**.

Before dispatch, the Control Center must allocate and durably persist:

- the stable ATES `run_id` for the logical run;
- a stable random `session_request_id` for the logical remote allocation request;
- a canonical digest of the immutable session request, including the requested image digest, test/spec digest, **immutable staged-input manifest**, network/isolation policy, and other allocation-relevant inputs;
- the selected `owner_node_id` for this placement;
- a monotonically increasing `placement_generation` / fencing token for this logical session;
- authoritative cancellation state/operation identity for the logical session.

### Global placement ownership and fencing

Node-local deduplication is necessary but not sufficient. A restarted/failing-over Control Center must not send the same `session_request_id` to a different Node merely because it did not receive the first Node's allocation response.

Before the first dispatch, the Control Center must commit a **globally authoritative placement record** in durable coordination state, conceptually:

```yaml
session_request_id: SESSION-01K...
run_id: RUN-01K...
request_digest: "sha256:..."
owner_node_id: NODE-0007
placement_generation: 12
state: dispatching
cancellation:
  state: none
  operation_id: null
```

Required semantics:

- creation or transfer of placement ownership is serialized/transactional for a `session_request_id`;
- Control Center replicas/failover instances recover the same committed `owner_node_id`, generation, and cancellation state before issuing allocation/start commands;
- a retry while ownership is ambiguous is sent only to the committed owner Node; it is **not** opportunistically rescheduled to another Node;
- dispatch/start is forbidden when the authoritative cancellation state is `requested`, `pending_owner_ack`, `cancelled`, or another policy-defined non-startable cancellation state;
- the Node validates that the request names itself as owner and carries the current signed/authenticated placement generation/fencing token, and durably binds that generation to its local session mapping;
- a stale generation is rejected and can never authorize a new allocation/start;
- assigning a higher generation to another Node is allowed only after the previous owner's session is known to be terminal/released or the previous owner has been **definitively fenced** so it cannot continue/start the logical execution;
- loss of heartbeat, lease expiry, Control Center restart, or inability to contact the owner is **not by itself proof of fencing** and must not authorize cross-Node redispatch;
- acceptable fencing may include confirmed Node-side terminal/release state, authenticated provider/hypervisor termination verified by the authority controlling that host, or another deployment-specific mechanism that makes continued execution on the old owner impossible;
- if the old owner cannot be reconciled or fenced, the logical session enters an `ownership_unknown`/reconciliation state and fails or waits according to policy rather than running on another Node;
- ownership changes, cancellation transitions, fencing evidence, generation transitions, and generation retirement are auditable and become part of session/ATES provenance where appropriate.

An expiring lease may help detect stale coordinators, but **lease expiry alone is not a safety proof** for destructive execution. The invariant is stronger: at most one Node may be permitted to execute a given logical `session_request_id` at a time, including across Control Center failover.

### Immutable staged-input identity

Fleet must not treat a staged source path or optional user-supplied checksum as sufficient identity for a remote run. Before the session request digest is finalized, the Control Center (or another trusted staging authority) must resolve **every staged input** to immutable content identity.

The canonical staged-input manifest should include, for each file/object:

```yaml
logical_name: application-under-test
stage_path: stage://app/build.zip
size_bytes: 18429312
sha256: "..."
```

Required semantics:

- a cryptographic content digest (or an equivalently immutable content-addressed object identity) is required for every staged input included in a Fleet session, even if the source test specification omitted its optional `sha256` field;
- the digest is computed from the actual bytes selected for dispatch, not merely from the source pathname, mtime, or mutable object name;
- once the canonical session request digest is created, replacing source bytes cannot change what that logical request means;
- transfer to the Node/guest must verify the expected content digest before the target is launched;
- a missing/mismatched staged object fails the allocation/staging phase and must not silently substitute new bytes under the same `session_request_id` or `run_id`;
- ATES provenance records the immutable staged-input identities used by the run where policy permits, without leaking secret content.

If immutable identity cannot be established for a required staged input, the Fleet run fails closed before dispatch. The local non-Fleet syntax may continue to allow an omitted checksum where current behavior permits it; Fleet canonicalization is responsible for producing the required content identity before remote execution.

The Node must durably associate `session_request_id` with the request digest, placement generation, capacity reservation, cancellation state, and allocation result before or atomically with making the Capsule externally observable/runnable.

Required retry semantics:

- a first valid request may allocate at most one logical Capsule/session for that `session_request_id` on its committed owner;
- if the allocation response is lost, the Control Center retries with the **same** `session_request_id`, `run_id`, request digest, owner, and placement generation;
- before any retry of allocation/start, the Control Center re-reads/validates the globally authoritative cancellation state and must not dispatch if cancellation has been durably requested;
- a duplicate request with the same identity/digest/generation returns/replays the original allocation result and current session state rather than creating another Capsule or starting the test again;
- reuse of the same `session_request_id` with a different request digest or `run_id` is rejected as a protocol/security error;
- a request for a non-owner Node or stale placement generation is rejected;
- the Node retains active request-to-allocation state through execution and transitions it to a durable terminal allocation tombstone when the logical session becomes terminal/released;
- retry processing must distinguish `dispatching`, `allocated`, `starting`, `running`, `cancellation_pending`, `completed`, `failed`, `retained`, `cancelled`, `ownership_unknown`, and `released` states so replay never means re-execution.

### Terminal allocation tombstones and generation retirement

A terminal session must not become executable again merely because Node-local deduplication state aged out while an old allocation request is still authenticatable/authorized.

When a session reaches `completed`, `failed`, `cancelled`, `retained`, or `released`, the owner Node must persist a **terminal allocation tombstone** keyed by `session_request_id` and placement generation. The tombstone should retain enough information to reject/replay delayed requests safely, including at least:

```yaml
session_request_id: SESSION-01K...
run_id: RUN-01K...
request_digest: "sha256:..."
owner_node_id: NODE-0007
placement_generation: 12
terminal_state: completed
```

Required semantics:

- a delayed/replayed allocation or start for the same `session_request_id` and generation returns the terminal result/state and **never creates another Capsule**;
- a conflicting digest/run ID is rejected as a protocol/security error;
- terminal tombstones survive Node restart/reconciliation;
- a time-based local retention window alone is insufficient grounds to delete a tombstone;
- the tombstone must remain while any credential, signed command, capability, placement lease/token, or other authorization for that generation can still cause the Node to accept an allocation/start request;
- before the Node may garbage-collect the tombstone, the Control Center must durably transition that placement generation to a **retired/revoked/fenced** state and the Node must have authenticated evidence of that transition such that requests carrying the retired generation can no longer authorize execution;
- generation retirement is itself idempotent and auditable, and retirement state must survive Control Center/Node restart sufficiently to reject stale requests;
- if the Node cannot prove that the old generation is durably non-executable, it retains the tombstone rather than risking resurrection;
- a later legitimate re-execution uses a new logical `session_request_id` (and normally a new `run_id`), never deletion/reuse of an old terminal identity.

This makes deduplication lifetime a function of **authorization lifetime**, not an arbitrary cleanup timer. A request cannot become “new” again while the credentials/generation that made it valid are still capable of reaching the Node.

### Globally durable cancellation and Node tombstones

Cancellation must be safe across message reordering, Control Center crash/failover, Node disconnects, and ambiguous delivery.

A cancellation request must carry the target `session_request_id` plus its own stable idempotent `cancellation_operation_id`.

**Before the Control Center acknowledges that the cancellation request has been accepted**, it must atomically/durably transition the globally authoritative placement record to a non-startable cancellation state and persist the operation identity, for example:

```yaml
session_request_id: SESSION-01K...
placement_generation: 12
state: cancellation_pending
cancellation:
  state: requested
  operation_id: CANCEL-01K...
  requested_at: "..."
```

Required Control Center semantics:

- the durable global cancellation transition is serialized with placement/dispatch state changes for the same `session_request_id`;
- an API/UI response that says cancellation was **accepted** is returned only after that transition commits;
- after the transition commits, every coordinator replica/recovered coordinator must treat allocation/start as forbidden and must never resend an allocation/start merely because an earlier request was ambiguous;
- dispatch workers must re-check authoritative cancellation state immediately before issuing/reissuing a start-capable operation, not rely on an earlier cached scheduling decision;
- cancellation delivery to the owner Node is retry-idempotent using the same operation ID;
- if the owner Node is unreachable, the global state remains `cancellation_pending`/equivalent rather than reverting to `dispatching` or pretending the execution is definitively stopped;
- `cancellation_pending` means the Control Center will not authorize new start/dispatch activity, but an already delivered/in-flight command may still require owner acknowledgment or definitive fencing before the UI can claim the execution is stopped;
- the final `cancelled` state requires authenticated Node acknowledgment/terminal evidence, or definitive fencing/provider termination evidence proving that continued execution on the owner is impossible;
- Control Center restart/failover recovers the durable cancellation operation and resumes delivery/reconciliation rather than recreating the earlier dispatch intent;
- conflicting reuse of a cancellation operation ID is rejected and all transitions are auditable.

Required Node semantics:

- if cancellation reaches a Node before the corresponding allocation request is known, the Node must **persist a cancellation tombstone** for that `session_request_id`/generation rather than treating the request as a no-op;
- allocation/admission/start checks the local cancellation tombstone before reserving capacity or creating/running a Capsule;
- if a matching tombstone already exists, a later delayed allocation is recorded/replayed as cancelled and must not produce executable side effects;
- cancellation of an already allocated/running session transitions it according to the explicit cancellation policy and remains idempotent on retry;
- cancellation tombstones transition into/are retained with the terminal allocation tombstone and are not garbage-collected until the corresponding placement generation is durably retired/revoked so stale allocation/start requests cannot authenticate;
- teardown/release operations that can be retried also require stable operation identities and must not erase the cancellation fact early enough to permit a delayed allocation to resurrect the session.

The durable Control Center state closes the coordinator-crash window; the Node tombstone closes the cancel-before-allocation delivery race. Neither mechanism alone is sufficient. If the Node has not yet observed cancellation and an old command may already be in flight, Fleet reports `cancellation_pending` until reconciliation/fencing resolves that uncertainty rather than overstating safety.

This contract prevents a lost or reordered HTTP/gRPC/WebSocket exchange from turning one logical Fleet request into duplicate, resurrected, coordinator-reissued, or cross-Node execution.

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

Before matrix cases are dispatched, mutable aliases must resolve to immutable image identities and staged application/configuration inputs must resolve to immutable content identities. Each expanded run receives its own `run_id` and `session_request_id`, and its ATES provenance records the resolved image and staged-input digests/versions. Two matrix cases that resolve to different immutable inputs are different baselines even if their aliases or source paths are identical.

The Control Center expands the logical plan into independently identified runs while preserving a parent plan/matrix identity.

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

The Fleet timeline must apply the **Trusted Fleet time basis** above. It may place cross-Node events on a common adjusted wall-clock axis only when the associated offset/uncertainty data supports that precision. Events with overlapping uncertainty intervals or degraded clock state must be shown as approximate/uncertain rather than assigned a false exact order. A receipt-time view may be offered separately and must be labeled as Control Center observation time, not execution time.

Selecting a failure can show:

- failed assertion;
- associated observations;
- checkpoint/failure screenshot;
- finding;
- collected artifacts;
- producer and central timing provenance when cross-Node timing matters;
- Failure Capsule retention state where the underlying execution path supports it.

## ATES integration

Fleet should use ATES as its canonical execution-event vocabulary.

```text
Node/Capsule
    |
    +--> local ATES evidence
    |
    +--> encrypted authenticated event stream --> Control Center
                                                    |
                                                    +--> Observer
                                                    +--> fleet timeline
                                                    +--> final reports
                                                    +--> audit package
```

The Control Center should not rewrite Node facts as though it observed them directly. Provenance must identify which Node/environment emitted each event.

Fleet transport must preserve the ATES event envelope: stable event IDs, canonical gap-free per-run sequences (or explicit ATES tombstones), idempotent retries, explicit gap detection, and conflict rejection. Canonical execution order comes from the producer's ATES sequence rather than network arrival order.

The aggregator additionally records authenticated Control Center receipt timestamps and Node clock offset/uncertainty assessments as separate transport/provenance metadata. This metadata may support cross-run visualization/audit correlation but must not alter the producer event's `occurred_at` or invent a total cross-Node order when timing uncertainty overlaps.

## Results and artifacts

The Control Center may index metadata centrally while large artifacts remain on Nodes or are uploaded to central/object storage according to deployment policy.

Any future central transfer protocol must preserve:

- authenticated encryption in transit;
- artifact identity;
- digest verification after transport;
- size limits/quotas;
- authorization;
- provenance;
- ATES manifest references;
- retention/deletion policy.

If object storage is used, upload/download credentials must be scoped and short-lived where practical, and Observer access must remain read-only according to authorization policy.

## Failure Capsules

Fleet should expose retained Failure Capsules as forensic resources, not as ordinary interactive remote desktops.

The existing rule remains important: retained secure Failure Capsules contain disk/config evidence but do not persist live recovery bearer tokens or TLS private keys merely to make them resumable.

Current Argus retention behavior is defined by the underlying execution path. Fleet must not imply retention for a run type that does not call the existing failure-recording lifecycle hook; unsupported paths must report retention as unavailable rather than pretending a forensic Capsule exists.

Future discard/export workflows should use explicit privileged Control Center operations and be represented in the audit trail.

## Security principles

1. Node discovery is not authentication.
2. Node enrollment is explicit and authenticated.
3. Enrollment secrets never appear as literal argv values and are short-lived, single-use, and atomically consumed.
4. Enrollment is bound to a Node-generated key/request ID; pending durable enrollment state remains recoverable until acknowledgment or explicit authenticated revocation.
5. Fleet control, ATES/event, screen, and artifact traffic uses authenticated encryption in production.
6. Observer authorization is read-only by construction.
7. Control Center privilege does not bypass Capsule guest policy accidentally.
8. Node Agents enforce local provider/capability constraints and atomically reserve local capacity before allocation.
9. Scheduled images are pinned to immutable identities and verified by the Node before allocation.
10. Every staged Fleet input is content-addressed/cryptographically identified and verified before launch.
11. Remote session allocation is idempotent by stable request identity and globally pinned placement ownership; retries never create a second logical execution on the same or another Node.
12. Cross-Node ownership transfer requires definitive fencing of the prior owner; heartbeat/lease expiry alone never authorizes redispatch.
13. Terminal allocation/cancellation tombstones remain until their placement generation is durably retired or revoked and can no longer authorize execution.
14. Cancellation is atomically persisted in global placement state before acceptance is acknowledged; recovered coordinators never re-dispatch a cancelled/pending-cancel session.
15. Node-side cancellation tombstones preserve causal safety when cancellation and allocation arrive out of order; ambiguous in-flight execution remains visibly `cancellation_pending` until owner acknowledgment or fencing.
16. Node producer timestamps are preserved, while Control Center receipt time plus measured offset/uncertainty governs any cross-Node timing claim; skewed/overlapping times are visibly qualified rather than falsely ordered.
17. Remote placement never silently falls back to local execution.
18. All control-plane mutations are auditable.
19. ATES facts retain Node/session/image/input/placement provenance and transport preserves ATES event identity/order.
20. Credentials and guest-control secrets are not emitted into ATES evidence.
21. Ambiguous distributed failures use reconciliation rather than destructive guesses.

## Initial implementation sequence

Recommended order:

1. define Node identity/capability models, Node key ownership, enrollment request IDs, bootstrap credential semantics, acknowledgment-persistent enrollment recovery, and immutable image identity metadata;
2. implement Node Agent daemon and authenticated/idempotent enrollment using protected secret input rather than argv, with explicit recovery/revocation for unacknowledged identities;
3. establish authenticated encrypted Fleet channels and add heartbeat/capability/image registry plus trusted receipt-time/clock-offset-and-uncertainty sampling to the Control Center;
4. define stable `session_request_id`, durable global placement owner/generation/cancellation state, definitive fencing and generation-retirement rules, immutable staged-input manifests, canonical request digests, terminal/cancellation tombstones, durable Node-side deduplication, and atomic capacity-reservation semantics;
5. implement remote session request/response around existing Capsule APIs only after global placement ownership, authorization-lifetime terminal tombstones, globally durable pre-ack cancellation, idempotent allocation, causal Node cancellation, staged-input verification, and Node-side admission are enforced;
6. add reconciliation for disconnect/restart scenarios, including ownership/fencing/retirement/cancellation state, reservations, terminal/cancellation tombstones, clock-state degradation/resampling, and unacknowledged enrollments;
7. add ATES event transport once ATES Core exists, preserving event ID/sequence retry semantics, immutable image/input/placement provenance, producer `occurred_at`, central `received_at`, and clock offset/uncertainty metadata;
8. implement read-only Observer API;
9. implement live screen transport over the protected Fleet channel;
10. add scheduler/queue and placement requirements with immutable image pinning while treating heartbeat capacity as advisory;
11. add matrix execution and a Fleet timeline that explicitly represents uncertain/degraded cross-Node temporal ordering;
12. add encrypted centralized artifact indexing/storage policies;
13. later consider cloud/autoscaling workers.

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
