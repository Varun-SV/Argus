# Usability reproduction results

Date: 2026-10-07. Diagnostic runtime baseline: PR #28 source `c81a0d3d8a8f747d9dc92597a12405c21362857c`; the seven inspected installed-package modules matched after line-ending normalization. This is a scoped comparison, not full-package provenance or current-main acceptance.

These results supplement the [issue register](usability-issues.md) and [draft stabilization specification](stabilization-spec.md). The original operator configuration and knowledge store were not changed. Diagnostics used isolated projects and owned benign fixtures. Private logs, conversation excerpts, credentials, screenshots and machine paths remain outside Git.

## Reproductions

| Scenario | Observed result | Classification |
|---|---|---|
| Chrome request and launch | Saved history distinguished a website-test drafting request from a separate desktop Chrome Roam request. Bare executable-name launch reproduces WinError 2. An unquoted spaced path produces split argv and fails verified-window attachment; a quoted executable with an owned profile launches successfully. | Target discovery/argument/ownership gap, ARG-03. Windows resolution and Chrome handoff prevent assigning every launch failure solely to argv splitting. |
| Command and interactive CLI | Command fixture passes with exit 0 and expected stdout. Owned reference Python REPL and Windows command prompt accept input and emit expected output. Argus has no persistent stdin/session operation and rejects `type`; its command observation can report process alive after completion. | Missing interactive contract, ARG-04/05. The exact historical crash cause cannot be reconstructed from saved summaries. |
| Browser Roam with Gemini and Ollama | Each live provider executes three actions on an owned loopback HTTP app. Typing reports success while the observed input stays empty. Clicks work, and Gemini reaches the next page. Both runs end as deliberately cancelled after Stop; their canonical ATES evidence verifies. | Concrete input-effect defect, ARG-06/07; these cancelled probes are not functional coverage passes. |
| Browser assertion oracle | A scripted type/submit sequence asserts the expected submitted text. The action step passes but the assertion fails because the input is empty; failed canonical ATES evidence verifies. | An explicit deterministic oracle detects the defect. |
| Browser lifecycle | First launch/close returns, but a second launch on the same thread fails because Playwright's Sync API encounters an active asyncio loop. The returned Playwright object has `stop()` and no `__exit__`. Explicitly stopping the owned driver restores launch. | Teardown defect, ARG-13. The live Ollama probe used a fresh process and diagnostic owned-driver cleanup; production code was not fixed. |
| Configured knowledge operations | Actual isolated project store rejects state/finding writes because an Ollama embedding model name is passed to SentenceTransformer. Retrieval has no recorded vector states/bugs; graph persistence works. Offline settings and external-request blocking prevent uncontrolled downloads. | Embedding routing/readiness defect, ARG-09. |
| Embedding and JSON references | Direct Ollama embedding returns HTTP 200 with one 768-dimensional vector. An initial 35-second attempt times out; a bounded follow-up completes in 40.94 seconds. A JSON reference retrieves the synthetic recorded finding. | Reference capability succeeds; this is not a fix or migration of the operator's store. |

## Native Capsule investigation

The approved Windows ISO and offline runtime bundle match their expected SHA-256 digests, and the production runtime-bundle verifier passes. The original saved Capsule configuration fails on its missing CA file before VM allocation; its ISO-in-disk-field setup is a separate mismatch.

With proper provisioned inputs, the production security validator still blocks control because the Hyper-V provider does not advertise protected bootstrap media. ISO-provisioned control must remain disabled until protected delivery and guest-side access verification are implemented. A capability-flag override or mocked baseline cannot satisfy acceptance.

The operator-approved administrator inspection checked the exact prior unpublished disk's digest and ownership, mounted it read-only without a drive letter, and confirmed detachment afterward. Hyper-V was running and no Argus VM was registered. Runtime and specialization artifacts exist, the runtime executable matches the verified bundle, and the Capsule control-state file is absent. Sysprep logs show generalization and shutdown activity alongside a nonempty error log; successful generalization is **not proven**.

No VM was created or booted during this diagnostic. No network or host setting was changed. Full native provisioning/boot, first-boot specialization, TLS/auth rotation, quarantine/reconnect and stale-credential rejection remain **blocked/unverified**. Real libvirt acceptance additionally requires an eligible Linux host.

## Validation and remaining gates

The previously completed focused baseline suite had **213 passing tests**. Those unchanged-source checks are separate from these reproductions and do not establish that the discovered defects are fixed. No runtime fixes or configuration migration were applied.

Four requested reproduction areas are complete; native inputs, the security blocker and read-only retained-image inspection are complete. Full native acceptance remains blocked. The [stabilization specification](stabilization-spec.md) defines the required remediation and acceptance gates while preserving the approved PR #28 appearance and PR #27 isolation/evidence contracts. This PR remains documentation only; matrix implementation and public release readiness are not established by these results.
