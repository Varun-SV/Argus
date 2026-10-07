# Argus stabilization and Environment Matrix specification

Version: **v0.2 / DRAFT**. Date: 2026-10-07.

Purpose: combine the PR #29 Environment Matrix goal with dependable existing CLI, browser, desktop and Capsule workflows. This is an implementation proposal for review, not a claim that these requirements already pass.

Reference: [PR #29](https://github.com/Varun-SV/Argus/pull/29), its `docs/environment-matrix.md`, and the companion [usability issue register](usability-issues.md).

## 1. Scope and approved decisions

The final product MUST retain the PR #28 appearance and conversation interface. Reliability work MUST use the existing execution architecture:

```text
conversation / test plan
        -> ExecutionEnvironment
        -> Capsule where requested
        -> Adapter
        -> target
        -> canonical ATES evidence
```

The operator explicitly requires **both command-based and persistent interactive CLI testing**. A one-shot CLI demonstration does not satisfy that requirement.

PR #29's original goal remains:

1. reuse verified immutable images from PR #27 instead of reinstalling an ISO per test;
2. expand OS, CPU, RAM, supported storage/device choices, application and test combinations into deterministic cases;
3. queue cases and admit them within reserved local capacity;
4. extend the same cases across Fleet Nodes under existing ownership/fencing rules;
5. produce one independently verifiable ATES Run per case;
6. aggregate references into the existing GUI with exact evidence links;
7. consider adaptive minimum-passing-configuration discovery only after deterministic matrix execution works.

No second VM backend, remote execution protocol, CLI execution framework outside Adapter, or canonical evidence format may be introduced.

Implementation MUST preserve PR #27's immutable image verification, runtime identity, fresh generation credentials/TLS, target-user isolation, quarantine/reconnect, cleanup uncertainty and provider ownership invariants. It MUST preserve the PR #28 visual design and authorization checks.

## 2. Planning and implementation gates

This proposal extends the planning scope; it does not remove the original matrix implementation gate.

- PR #27 and PR #28 are merged (confirmed on 2026-10-07). The diagnostic runtime baseline remains `c81a0d3`; it is not a claim of current-main acceptance.
- Final merged APIs and Fleet contracts MUST still be reconciled before matrix implementation.
- The matrix plan MUST be reconciled with the final merged APIs and Fleet contracts.
- Stabilization defects may be fixed in appropriately scoped prerequisite changes. Matrix implementation MUST wait until the original prerequisite gate and the stabilization checks below are met.
- No runtime fixes, host setting changes, installation upgrades or native VM allocations were performed. The approved native diagnostic inspects only the retained unpublished disk read-only; its outcome does not establish boot acceptance.

## 3. Provider and configuration readiness — ARG-01, ARG-02, ARG-09

CFG-01. Readiness MUST distinguish configuration parsed, credentials available to this process, provider reachable, configured model available and required capabilities available. Loading a project MUST NOT be presented as proof that testing works.

CFG-02. Gemini and Ollama MUST have bounded, explicit health checks. A health check MUST show safe cause categories for missing credential, authentication denial, model unavailable, rate limit, timeout and transport failure. It MUST NOT print credentials, authorization headers or secret-bearing provider responses.

CFG-03. Windows environment inheritance guidance MUST explain that an already-running terminal/application may need to restart after a machine/user environment change. Argus MUST NOT silently persist credentials into project configuration. Whether to add an explicit user-triggered credential refresh action is a later implementation choice; automatic registry credential discovery is not required by this specification.

CFG-04. Chat/completion, vision and embedding roles MUST be separate. An embedding-only Ollama model MUST NOT be accepted as the chat model. A failed image probe due to auth/rate-limit/timeout/server failure MUST NOT be reported as proof that the model lacks vision.

CFG-05. Embedding configuration MUST name the provider/backend and model. Ollama embeddings MUST use its embedding API; a local SentenceTransformer name MUST use the local loader. Optional embedding failure MUST leave graph behavior explicit and usable where supported, with a visible reason. No silent uncontrolled model download may be triggered by a status label.

CFG-06. Vector records MUST bind embedding backend/model/dimension. Switching models MUST require a compatible collection or explicit migration/re-index action; incompatible vectors MUST NOT be mixed silently.

CFG-07. Existing config files MUST continue to load through documented compatibility handling. Corrections MUST be shown as a reviewable proposal, preserve unrelated fields, and write atomically only when requested. Diagnostics MUST leave the original file untouched.

## 4. Capsule setup and preflight — ARG-02

CAP-01. The UI and CLI MUST distinguish **installation media**, **provisioned immutable image** and **explicit legacy prepared guest**. An ISO MUST NOT be accepted as a virtual-disk boot image.

CAP-02. Selecting an ISO MUST lead to the existing provisioning flow: approved media digest, approved offline runtime bundle, machine contract, private build workspace, native OS generalization, disposable baseline validation, verified cleanup, image publication and finalized ATES binding.

CAP-03. Provisioned execution MUST resolve its disk from the verified cache/environment definition. Static guest token/CA configuration MUST NOT be blended into the per-generation trust path. Legacy preparation MUST be explicitly labeled and checked separately.

CAP-04. Preflight MUST report all discoverable blockers before VM allocation: wrong image kind, missing files, definition/cache/evidence mismatch, provider/host incompatibility, unsupported machine contract, trust-material errors and insufficient resource/storage capacity. Inspection MUST not create or change a VM.

CAP-05. A Capsule request MUST NOT fall back to local execution. Cleanup uncertainty MUST remain visible and block reuse/publication when relevant. Retained failure state MUST not be described as safe until quarantine is established.

CAP-06. Native acceptance MUST prove two fresh Capsules have distinct machine/Capsule/control identities, retain and reconnect the same mutable VM/disk, reject stale credentials and respect allowed/forbidden execution-mode requests. Linux acceptance requires a suitable libvirt host; Windows success does not prove it.

CAP-07. ISO-provisioned control MUST remain disabled while protected bootstrap delivery is unsupported. Enabling it requires an implemented protected host-to-guest delivery path and guest-side access verification against the non-admin target user, including secret-file and raw-medium access. Setting a capability flag alone, mocking the baseline or bypassing the guard MUST NOT count as implementation or native acceptance. Failed or uncertain media detachment MUST retain the existing cleanup fencing.

## 5. Target selection and Chrome — ARG-03, ARG-05

TGT-01. Targets MUST represent an executable path and arguments distinctly from a URL and adapter selection. The user MUST be shown the selected adapter/environment before authorized execution; a model MUST NOT silently reinterpret a Chrome desktop request as website testing or change isolation.

TGT-02. An exact existing Windows executable path containing spaces MUST work as a path, including when supplied without manually adding shell quotation marks. Commands containing arguments MUST use explicit argv or a verified Windows command-line parser. Ambiguous inputs MUST request clarification.

TGT-03. Missing-target errors MUST identify the checked target and explain the correction. Installed Chrome discovery, supplied executable paths and Playwright's managed browser dependency MUST be distinguished.

TGT-04. Desktop Chrome single-instance handoff MUST preserve process/window ownership checks. Support may use an owned test profile or an explicit supported attachment flow. Argus MUST NOT claim an unrelated existing browser window by title or close the operator's unrelated browsing session.

TGT-05. Generated tests, Roam, regressions, restart and evidence MUST preserve the actual adapter identity. A CLI crash MUST NOT become a `desktop-gui` regression merely because that was the template default.

## 6. Command and interactive CLI — ARG-04, ARG-05

CLI-01. Command mode MUST execute explicit argv with the selected working directory, environment policy, timeout and bounded output. It MUST expose stdout, stderr and exit code independently. Completion and nonzero exit MUST not be confused with a process crash.

CLI-02. Interactive mode MUST own a persistent process and its terminal transport. It MUST support sending input, observing incremental output, waiting for a prompt/expected text, checking process state/exit and closing the session. Shell metacharacters MUST not become host commands merely because text is sent to target stdin.

CLI-03. The first supported fixtures MUST include an owned Windows command prompt and Python REPL, plus a portable interactive fixture. A command/session launch with no supplied arguments MUST be identified as interactive or rejected with guidance, rather than synchronously hanging until it is classified as a crash.

CLI-04. Windows terminal handling SHOULD use an appropriate native terminal mechanism such as ConPTY; POSIX SHOULD use an appropriate PTY. Concrete backend choice remains an implementation detail. Unsupported host/guest combinations MUST fail explicitly.

CLI-05. Cancellation MUST interrupt/stop owned work within a defined bound, finalize evidence and clean only owned processes. Ownership MUST include process identity sufficient to avoid PID reuse. Child processes, EOF, prompt delays, output flood and disconnect MUST have defined behavior.

CLI-06. Capsule CLI interaction MUST run in the same Capsule/guest Adapter boundary, with generation-authenticated operations. It MUST NOT open a separate unauthenticated shell channel or execute the command on the host. Output and stdin privacy MUST follow existing ATES policy.

CLI-07. CLI observations MUST distinguish normal exit, expected nonzero exit, command not found, timeout, intentional Stop, adapter/setup failure, application crash and uncertain dispatch. Classification MUST not be inferred only from `process_alive == false`.

## 7. Browser and desktop action correctness — ARG-06, ARG-07, ARG-08, ARG-13

ACT-01. Observed element IDs MUST bind to the same observation-scoped element mapping for click, type and other element actions. Type MUST not recompute IDs using a different selector list.

ACT-02. If a supplied element cannot be resolved or cannot receive the requested operation, Argus MUST reject it before dispatch when that can be proven. Element-targeted typing MUST NOT fall back to arbitrary keyboard focus. DOM/UIA replacement MUST not silently transfer an old ID to a different control.

ACT-03. Windows semantic adapters MUST expose available permitted operations and verify a usable semantic method before dispatch commitment. Controls lacking a supported operation may be observed but MUST not be advertised as actionable for that operation.

ACT-04. Re-observation/replanning is allowed after a proven pre-dispatch rejection. Once a side effect may have begun or its outcome is uncertain, existing ATES uncertainty fencing MUST remain authoritative. No blanket retry, forged execution event or unsafe physical-input fallback is acceptable.

ACT-05. Browser Roam MUST work from the same GUI/CLI entry points as scripted browser tests. Owned fixtures MUST exercise navigation, input, click, changed DOM, dialog handling where supported, Stop and teardown. Errors MUST state whether launch, browser dependency, provider, policy, navigation or action failed.

ACT-06. Live Gemini and Ollama browser/desktop testing MUST validate observed target effects, not merely parse a model reply or count an accepted action. Only owned benign fixtures are required for deterministic acceptance; live third-party sites require separate operator scenarios and are not a reliable sole oracle.

ACT-07. Browser teardown MUST close the owned page/browser and stop its Playwright driver/context, including after a partial launch or action failure. Close MUST be idempotent; cleanup errors MUST remain visible where they prevent a clean session. A new session on the same thread MUST launch after teardown without a leaked event loop or driver. Cleanup MUST target only resources owned by that session.

## 8. Test drafting and evidence — ARG-10, ARG-11, ARG-12

DRF-01. A drafted test MUST have a runnable target/adapter, supported steps and explicit expected results. Placeholder values and unresolved targets/oracles MUST keep it in a visible draft state and block Run until reviewed.

DRF-02. A generated regression MUST preserve the real adapter/target and distinguish reproduction of the old defect from acceptance of its fix. Unknown trigger/oracle MUST be labeled unresolved. A crash finding MUST NOT invent an expected Error dialog.

DRF-03. Questions, negation, reported text, uncertain requests and model-suggested commands MUST continue to obey PR #28's authorization constraints. Fixing usability MUST NOT bypass these checks.

EVD-01. The UI MUST show termination reason, executed action count and evaluated assertions separately from the canonical outcome. Zero findings MUST NOT be presented as proof that all intended functionality was tested.

EVD-02. Failures MUST distinguish product assertion failures from configuration/provider/adapter failures, user cancellation and uncertain effects. Displayed report status MUST be consistent with finalized ATES evidence; classification corrections MUST follow existing evidence revision rules.

EVD-03. All acceptance runs MUST produce finalized/verifiable canonical ATES evidence. Diagnostic summaries MAY reference those runs; they MUST NOT replace them or claim unverified artifacts are verified.

EVD-04. Secrets, arbitrary provider responses, target stdin/output and screenshots MUST remain subject to existing privacy policy. Public documentation may retain safe counts, scenarios and result references; private artifacts MUST remain local.

## 9. Matrix requirements retained from PR #29

MAT-01. Deterministic plan expansion MUST validate combinations and separate immutable build/image identity from authorized runtime allocation. Case identity MUST include the plan/test/application/environment/requested allocation commitments and MUST NOT depend on placement Node or local path.

MAT-02. Runtime CPU/RAM/storage changes MUST be explicitly supported by the verified image/provider contract. The matrix MUST NOT falsify an existing machine contract, substitute an image or silently downgrade firmware/Secure Boot/TPM/network/architecture requirements.

MAT-03. Capacity admission MUST reserve memory, CPU policy, storage, provider and concurrency resources atomically. Cases with unmet requirements MUST queue or become explicitly unsatisfied. Reservations MUST not be released until the actual resource state is reconciled; retained/quarantined VMs still consume appropriate capacity.

MAT-04. Duplicate dispatch, lost acknowledgement, scheduler restart and Node disconnect MUST reconcile using existing ownership/generation/fencing. Completed cases MUST not rerun automatically unless identity/input changed or the operator explicitly requests another attempt.

MAT-05. Cancellation MUST cover queued, provisioning, interactive CLI, test and teardown phases without orphaning resources or mislabeling uncertain cleanup as complete.

MAT-06. Each child case MUST execute through the existing environment/Capsule/Adapter path and have its own canonical ATES Run. Aggregation MUST retain child references, actual allocation, provider/Node, termination category and evidence trust state without rewriting child evidence.

MAT-07. The existing GUI MUST present every planned case, including unsatisfied/cancelled/error cases, with exact evidence links. Styling MUST remain consistent with the approved PR #28 design.

MAT-08. Discovery remains a later optional phase. A report may identify the lowest *tested* passing configuration; recommendations require an operator-defined policy and MUST NOT infer support for untested configurations.

## 10. Acceptance checks and issue traceability

| Gate | Issues / requirements | Mandatory check |
|---|---|---|
| A01 Provider setup | ARG-01; CFG-01..04 | Missing/inherited keys, valid/invalid model, unauthorized, rate-limited, unavailable endpoint and failed vision probe show the correct safe state. Live Gemini and Ollama chat smoke pass. |
| A02 Embeddings | ARG-09; CFG-05..07 | Local and Ollama embedding configurations route correctly; embedding-only model rejected for chat; unavailable backend and dimension mismatch are explicit; existing config preserved. |
| A03 Capsule preflight | ARG-02; CAP-01..05 | ISO in disk field, absent CA/definition/cache, mismatched image/evidence/provider and insufficient capacity fail before allocation with actionable guidance. No local fallback. |
| A04 Native Capsule | CAP-06..07 | Supported Hyper-V and libvirt provisioning, fresh identities, quarantine, reconnect, old-credential rejection and cleanup verified on real hosts. |
| A05 Windows targets | ARG-03; TGT-01..04 | Quoted/unquoted exact spaced paths, explicit argv, missing executable, installed Chrome selection and single-instance handoff preserve owned process identity and unrelated sessions. |
| A06 CLI commands | ARG-05; CLI-01,05,07 | Portable fixture and Windows commands verify stdout, stderr, zero/nonzero exit, quoted paths, working directory, timeout, Stop and cleanup. |
| A07 Interactive CLI | ARG-04; CLI-02..07 | Command prompt and Python REPL accept input and produce expected output; EOF, prompt wait, timeout, cancellation, flood and owned-child cleanup work locally and inside supported Capsules. |
| A08 Browser element IDs | ARG-06; ACT-01..02 | H1/input/button fixture types into the observed input; hidden/removed/reordered controls, noneditable target and invalid IDs cannot redirect input or falsely report success. |
| A09 Desktop dispatch | ARG-08; ACT-03..04 | Nonactionable/stale UIA controls rejected before commitment; true post-dispatch uncertainty still fences interaction and cannot retry unsafely. |
| A10 Roam workflows | ARG-07; ACT-05..06 | GUI and CLI browser Roam execute observed fixture actions with both live chat providers; desktop/CLI adapter identity stays correct; Stop produces proper ATES/cleanup. |
| A11 Drafts/regressions | ARG-10..11; DRF-01..03 | Placeholder/unknown oracle blocks Run; CLI/browser regressions retain adapter; crash regressions do not invent Error dialogs; accepted post-fix fixture regressions prove behavior. |
| A12 Honest reporting | ARG-12; EVD-01..04 | No-action, setup error, assertion fail, user Stop and uncertain dispatch remain distinguishable; canonical evidence verifies and secrets do not leak. |
| A13 Matrix expansion | MAT-01..02 | Reordering inputs/placement does not change equivalent case IDs; incompatible allocation/security requests explicitly fail. |
| A14 Queue/recovery | MAT-03..05 | Capacity never over-admits; resource/cleanup faults, concurrent reservation, restart, disconnect and duplicate delivery do not cause duplicate execution or premature capacity release. |
| A15 Aggregation/Fleet | MAT-06..08 | Partial matrix displays every case and links to its independently verified evidence; Fleet placement preserves semantics and ownership; discovery does not imply untested support. |
| A16 Browser lifecycle | ARG-13; ACT-07 | Two sequential sessions on the same thread, repeated close, partial-launch failure and action-error teardown stop owned drivers completely; a subsequent launch works without touching unrelated sessions. |

Existing automated test counts are supporting evidence, not substitutes for these gates. Missing native-host acceptance MUST remain marked untested/blocked, not waived by mocks.

## 11. Delivery order

1. Freeze issue scope and fix provider/configuration preflight, target/path handling, browser element mapping and complete browser-driver teardown.
2. Make desktop semantic dispatch rejection/recovery precise; repair draft/regression readiness and status explanations.
3. Add interactive CLI through Adapter/ExecutionEnvironment and exercise command + terminal tests locally and in Capsules.
4. Complete Capsule provisioning/operator setup and real-host acceptance; reconcile final merged PR #27/#28 APIs.
5. Implement deterministic matrix expansion and safe runtime allocation.
6. Add reservations, adaptive queue, cancellation and restart reconciliation.
7. Add Fleet placement and existing-GUI matrix evidence aggregation.
8. Audit requirements and run current-head CI plus native/operator acceptance. Consider discovery only in a later scoped change.

Each delivered slice MUST list implemented requirements, tests, observed outcomes and remaining gates. Preserve the approved visual design throughout. Architecture refinements that change identity, isolation, state transitions, evidence or interactive CLI scope require an explicit specification amendment.

## 12. Current completion status

Completed now: evidence inventory, provider connectivity checks, installed/source comparison, focused existing tests (213 passed), original Chrome request/error capture, owned Chrome/CLI reproductions, live Gemini/Ollama HTTP Roam effect checks, verified canonical assertion failure, repeated-browser lifecycle reproduction, real isolated knowledge record/retrieve, verified ISO/runtime inputs and the production Capsule security blocker. Native inspection evidence remains private and does not replace boot acceptance.

Observed defects are not acceptance passes. Both live providers reproduce false-success typing; interactive CLI remains unsupported; the configured embedding loader cannot record vectors; serial browser teardown leaks the driver. Native provisioning/boot acceptance is blocked by unsupported protected-bootstrap delivery, independently of the original project's legacy trust/configuration mismatch. Approved read-only retained-disk inspection completed with confirmed detachment and an entrypoint matching the verified runtime bundle. Sysprep generalization/shutdown markers and a nonempty error log were found; successful generalization remains unproven. No guest boot was attempted. Real libvirt acceptance still requires a suitable Linux host.

Not completed now: runtime fixes, configuration migration, meaningful interactive CLI support, full native Capsule acceptance, matrix implementation or public release readiness.

This draft and local evidence are ready for review before implementation. Private configuration/logs/keys are not repository documentation. A repository-ready amendment distills sections 1–11 into PR #29 documentation and links the portable issue requirements, without copying private evidence directories or conversation history.

Detailed safe scenarios and validation limits are recorded in [reproduction results](reproduction-results.md).
