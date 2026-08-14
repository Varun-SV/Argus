# ATES runtime lifecycle — PR #18

PR #18 connects the reviewed ATES Core schema and durable event store to real Argus execution.

## What becomes runtime evidence

`argus run` and the public `argus.engine.roam.roam` entry point now create a canonical `RunId` and append structural lifecycle events to the PR #17 event store.

Scripted runs record:

- `RUN_STARTED` and the immutable logical Step identities;
- `ENVIRONMENT_PREPARED` and successful target launch/close lifecycle;
- Step-attempt start/completion and retry scheduling with immutable attempt IDs;
- structural observations;
- proposed and returned actions with stable action/operation identities;
- deterministic assertion result and observation linkage;
- Failure Capsule retention when the existing runtime reports it;
- a final `RUN_MARKED_INCOMPLETE` marker while canonical completion remains unavailable.

Roam records one runtime-derived `roam` Step/attempt. Its observations, application actions, and Findings use the same ATES vocabulary without inventing a scripted test-case or requirement chain.

## Privacy boundary

This PR deliberately records only structural evidence that can be persisted safely before the dedicated privacy pipeline exists.

The canonical stream does **not** persist ordinary plaintext copies of:

- authored Step instructions;
- launch targets;
- action parameter values such as typed text, URLs, commands, menu paths, or model rationale;
- accessibility/DOM/CLI/application text;
- assertion expected or actual values;
- model/human Finding prose;
- screenshot bytes.

Those fields use ATES `suppressed` evidence representations. Safe structural facts such as action kind, assertion kind/result, process-alive state, element/dialog counts, retry ordinal, and machine-readable Finding classification may be recorded.

Existing legacy reports, roam journals, and screenshots retain their pre-ATES behavior in this PR. They are **not** canonical ATES artifacts and are not promoted into the ATES trust boundary. The later privacy/artifact PRs will define policy-aware canonical capture.

## Action boundary

PR #18 records `ACTION_PROPOSED` before the existing Adapter call and `ACTION_EXECUTED` only after that call returns successfully. An exception is represented conservatively as `ACTION_OUTCOME_UNKNOWN`.

PR #18 does **not** emit `ACTION_DISPATCH_COMMITTED` or claim that policy validation has been durably recorded before a side effect. The exact pre-dispatch/reconciliation contract is intentionally reserved for PR #19, where the adapter/policy boundary can provide the required durable operation semantics.

## Completion boundary

PR #18 does not emit `RUN_COMPLETED`.

The existing `RunResult` / roam outcome continues to drive current CLI/API compatibility, but ATES ends with `RUN_MARKED_INCOMPLETE` and a safe `execution_result` hint. Canonical `completed` / `passed` remains unavailable until the transactional manifest/finalization layer can publish the exact terminal event and required manifest as one crash-safe logical transaction.

Therefore a PR #18 stream can be complete as an append history while still being intentionally **unfinalized ATES evidence**.

## Compatibility strategy

The merged roam implementation from before PR #18 is retained byte-for-byte in `argus/engine/roam_impl.py`. `argus/engine/roam.py` is a thin ATES-aware public wrapper around that implementation. This keeps exploration logic, prompts, legacy report generation, regression stubs, and screenshots unchanged while adding canonical lifecycle capture at the Adapter boundary.
