# ATES durable action dispatch

PR #19 implements the runtime boundary required by the Argus Test Evidence Specification before an action may cause a target-visible side effect.

## Boundary

For model-generated actions the runtime now uses this order:

1. `ACTION_PROPOSED` records a structural, secret-safe proposal identity.
2. `Adapter.prepare_action()` performs schema normalization, global action-policy enforcement, capability authorization, and adapter-specific validation without executing the action.
3. `ACTION_POLICY_VALIDATED` records the same stable `action_id` / `operation_id` after those checks succeed.
4. `ACTION_DISPATCH_COMMITTED` is durably appended before the target adapter is invoked.
5. `Adapter.dispatch_prepared_action()` performs the actual side effect.
6. A successful return is followed by `ACTION_EXECUTED`; a dispatch exception is followed by `ACTION_OUTCOME_UNKNOWN`.

The same immutable `ActionOperationId` is carried through the entire lifecycle.

## Fail-closed rules

A validation or policy rejection occurs before `ACTION_DISPATCH_COMMITTED`, so Argus has evidence that the target-side dispatch boundary was not crossed.

If the durable dispatch commit itself cannot be written, the adapter is not called.

Once `ACTION_DISPATCH_COMMITTED` is durable, any exception from the target dispatch is treated conservatively: the action may have happened. Argus records `ACTION_OUTCOME_UNKNOWN` and blocks further target observation/action through that ATES adapter proxy. This prevents a later model turn from blindly reissuing a potentially non-idempotent or destructive action with a fresh operation identity.

The proxy also blocks further target interaction when the action returned successfully but the terminal `ACTION_EXECUTED` evidence could not be durably established. On recovery, a durable dispatch commit without a trusted terminal event must be treated as ambiguous.

## Adapter compatibility

`Adapter.execute()` remains the public one-shot validation + execution API. Internally it is now composed from:

- `prepare_action(action)` — normalize and authorize without side effects;
- `dispatch_prepared_action(action)` — dispatch an already-prepared action.

`PolicyAdapter.act()` continues to preserve the historical engine API while using those two phases internally.

`dispatch_prepared_action()` is an internal trust-boundary method. It must never be used to pass raw model output around `prepare_action()`.

## Reconciliation boundary

This PR deliberately implements the conservative default: unresolved committed operations stop further target interaction. Provider-specific status queries, deterministic post-dispatch reconciliation, or adapter-side operation-ID deduplication can later prove a terminal outcome and resume execution, but Argus does not invent that proof or blindly retry in this PR.

## Privacy

PR #19 does not widen the PR #18 evidence-capture policy. Action payload values remain suppressed in canonical ATES. `ACTION_POLICY_VALIDATED` and `ACTION_DISPATCH_COMMITTED` persist only allow-listed structural action keys together with stable action/operation identities.
