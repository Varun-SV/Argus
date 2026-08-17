# ATES Evidence Privacy Pipeline

PR #20 turns the privacy rules in the ATES design into a runtime boundary. The
rule is intentionally simple: **ordinary ATES persistence never receives a raw
free-form value until a versioned privacy policy has decided what representation
is allowed.**

This is a text/JSON evidence boundary. Screenshot bytes and collected files are
not written by this pipeline; protected artifact capture remains a separate
follow-up so binary data cannot accidentally be persisted first and sanitized
later.

## Pre-persistence flow

```text
runtime value
    |
    v
EvidencePrivacyPolicy
    |
    +-- safe ----------> exact JSON-safe structural fact
    +-- redacted ------> literal "<redacted>" + safe reason/policy-issued secret refs
    +-- suppressed ----> reason metadata only; no value
    `-- protected -----> Argus-issued protected://ates/<random-id> only
                              |
                              `-- detached raw JSON copy stored under that ID
                                  by an authorized protected sink

Only the representation on the right may enter evidence.jsonl.
```

The privacy decision is a projection. It does **not** rewrite the object that
Argus executes. In particular, the exact action returned by the PR #19
prepare/validate boundary is still the action dispatched to the target; ATES
creates a separate `ActionRecord` containing only policy-approved evidence.

## Default policy (`ates-privacy-v1`)

The initial policy is deliberately conservative.

| Context | Default ordinary evidence |
| --- | --- |
| authored Step instruction | redacted |
| target/launch value | redacted |
| retry reason/value | redacted |
| unvalidated action parameter | redacted |
| assertion expected value | redacted |
| operator annotation | redacted |
| target window/UI text | suppressed |
| target dialogs/stdout/stderr/error/URL | suppressed |
| assertion actual value | suppressed |
| Finding title/description | suppressed |
| free-form error/log text | suppressed |

`None` remains a safe structural indication that a value was absent.

### Validated action facts

A model-controlled value is never promoted to `safe` merely because its field
name is known. Before validation all action parameter values are sensitive.
After schema, global-policy, capability and platform validation have succeeded,
only a narrow structural vocabulary can be recorded exactly:

- numeric `element_id`, `x` and `y` where applicable;
- validated scroll direction and amount;
- validated wait duration;
- `done.success` boolean;
- validated non-content key interactions, such as navigation keys and
  `ctrl`/`alt` shortcuts;
- allow-listed `report_bug` severity.

Bare or shift-only printable key presses remain redacted even when their chord
syntax is canonical, because they can enter arbitrary content into the focused
control one character at a time. Typed text, commands, URLs, menu paths and
report prose likewise remain redacted after the action is authorized. An
explicit protected `ACTION_PARAMETER` policy also takes precedence over safe
fact promotion, so selected deployments still emit only protected references.
The durable `ACTION_DISPATCH_COMMITTED` event therefore gains useful structural
FACTs without becoming a credential or data exfiltration channel.

## Protected evidence

A deployment may explicitly route selected contexts to a
`ProtectedEvidenceSink`. The reference is **not chosen by the sink**. Argus
first generates a payload-independent 128-bit random identifier such as:

```text
protected://ates/4cf4c0ab9c2fda31c9ad3911ec294ecc
```

Argus then freezes the JSON-compatible runtime value into an immutable snapshot,
creates a detached ordinary JSON copy for the sink, and asks the sink to persist
that copy under the already-generated `protected_ref`. Ordinary ATES receives
only that Argus-issued reference.

This ordering is part of the privacy boundary:

1. the reference is generated independently before sink code can inspect the
   payload;
2. mutating the detached value supplied to the sink cannot alter the immutable
   value Argus classified;
3. a sink return value is not used as canonical evidence metadata, so returning
   a payload-derived string cannot smuggle plaintext back into `evidence.jsonl`.

The sink is an authorization/storage boundary, not a formatter. Implementations
must persist the supplied value under the supplied `protected_ref`. PR #20 does
not claim that the protected store itself is encrypted, immutable or WORM-backed;
those properties depend on the concrete protected store. If a context requires
protected storage and no usable sink is configured, the pipeline fails closed
rather than falling back to plaintext ordinary evidence.

## Secret references and reason metadata

Redacted evidence may carry a `secret_ref`, but callers may not construct these
reference strings themselves. The active `EvidencePrivacyPolicy` issues a random
alias using `issue_secret_ref()`:

```text
secret://ates/6930b8853a5cf2e5d17fde00343654b8
```

The caller uses that issued alias as the key for its external secret store and
then supplies the same alias to `capture(..., secret_refs=(...))`. A reference
that merely has the right syntax but was not issued by that policy instance is
rejected. This makes opacity a provenance property rather than an unreliable
substring/randomness heuristic, including for one-character values, numeric
values, short strings and values that happen to occur inside random-looking
identifiers.

When a redacted value carries secret references, the value is snapshotted using
the same generic JSON boundary used by ATES Core. General `Mapping` and non-string
`Sequence` implementations therefore receive the same validation as ordinary
`dict`/`list` values, and caller containers are not mutated.

Disposition reasons are controlled policy codes such as
`privacy.action_value`, not user-, target- or model-authored prose. This builds
on the ATES Core restriction that reason metadata itself must be safe.

## Retry reasons

A retry is correlated with the failure that actually caused it. Before
`STEP_RETRY_SCHEDULED`, the runner derives the cause from the most recent failed
`StepResult` (failure note first, then observed actual/action fallback) and sends
that value through the privacy policy once.

The resulting `EvidenceValue` is reused by both `STEP_RETRY_SCHEDULED.reason` and
the next `STEP_ATTEMPT_STARTED.retry_reason`. Under a protected `RETRY_REASON`
policy this means the protected sink stores the real cause once and both events
carry the same `protected_ref`; the hard-coded lifecycle marker `"retry"` is not
stored as a substitute for the real failure.

## Target-generated content

Window titles, accessibility text, dialogs, stdout/stderr, URLs and error text
are treated as untrusted and potentially sensitive. The standard policy
records useful structural facts—process state, element/dialog counts, presence
of output/error/URL and screenshot presence—but suppresses the free-form values.

This is intentional. A target can display credentials, tokens, customer data
or adversarial prompt text. “It came from the application under test” is not a
basis for treating it as safe evidence.

## Findings and assertions

Assertion results and methods remain deterministic structural evidence. The
expected value is redacted and the observed/actual value is suppressed by
default. Deployments that explicitly require the actual value may route that
context to a protected sink; the runner passes the real `StepResult.actual`
through that boundary rather than substituting a presence marker.

Finding source and allow-listed classification remain machine-readable. Model
or runtime prose—title, expected/actual details and explanation—is classified
before append and suppressed by the standard policy. Roam continues to produce
its existing legacy report; that report is not thereby promoted into the ATES
trust boundary.

## What this PR does not do

PR #20 deliberately does not:

- persist screenshot bytes or collected files as protected artifacts;
- mask/redact an image after first writing the original image;
- generate artifact manifests or hashes;
- implement canonical `RUN_COMPLETED` finalization;
- make legacy reports trusted ATES renderings;
- infer secrets with a best-effort regex scanner and then treat all unmatched
  text as safe.

Binary pre-write decisions and protected artifact storage belong to PR #21.
Transactional finalization remains a later ATES PR.

## Security invariant

The implementation should be reviewed against this invariant:

> A value that is not explicitly admitted as a safe structural fact must never
> reach ordinary canonical ATES as plaintext merely because redaction failed,
> a protected sink was unavailable, an exception was raised, a sink mutated its
> input or returned payload-derived data, or a target/model placed the value in
> an unexpected free-form field.
