# ATES Evidence Privacy Pipeline

PR #20 turns the privacy rules in the ATES design into a runtime boundary.  The
rule is intentionally simple: **ordinary ATES persistence never receives a raw
free-form value until a versioned privacy policy has decided what representation
is allowed.**

This is a text/JSON evidence boundary.  Screenshot bytes and collected files are
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
    +-- redacted ------> literal "<redacted>" + safe reason/opaque secret refs
    +-- suppressed ----> reason metadata only; no value
    `-- protected -----> opaque protected:// reference only
                              |
                              `-- raw value went directly to an authorized sink

Only the representation on the right may enter evidence.jsonl.
```

The privacy decision is a projection.  It does **not** rewrite the object that
Argus executes.  In particular, the exact action returned by the PR #19
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
name is known.  Before validation all action parameter values are sensitive.
After schema, global-policy, capability and platform validation have succeeded,
only a narrow structural vocabulary can be recorded exactly:

- numeric `element_id`, `x` and `y` where applicable;
- validated scroll direction and amount;
- validated wait duration;
- `done.success` boolean;
- the already-canonical Argus key chord;
- allow-listed `report_bug` severity.

Typed text, commands, URLs, menu paths and report prose remain redacted even
after the action is authorized.  The durable `ACTION_DISPATCH_COMMITTED` event
therefore gains useful structural FACTs without becoming a credential or data
exfiltration channel.

## Protected evidence

A deployment may explicitly route selected contexts to a
`ProtectedEvidenceSink`.  The raw JSON/text value is passed directly to that
sink and ordinary ATES receives only an opaque reference such as:

```text
protected://vault/opaque-record-id
```

The sink is an authorization/storage boundary, not merely a callback for
formatting.  PR #20 does not claim that a reference is encrypted, immutable or
WORM-backed; those properties depend on the concrete protected store.  If a
context requires protected storage and no usable sink is configured, the
pipeline fails closed rather than falling back to plaintext ordinary evidence.

Protected references are syntax-checked and must be opaque.  A sink may not
return the raw value as its reference.

## Secret references and reason metadata

Redacted evidence may carry opaque secret references of the form
`secret://<namespace>/<opaque-id>`.  Arbitrary free-form strings are rejected so
metadata cannot become a second path for leaking the secret itself.

Disposition reasons are controlled policy codes such as
`privacy.action_value`, not user-, target- or model-authored prose.  This builds
on the ATES Core restriction that reason metadata itself must be safe.

## Target-generated content

Window titles, accessibility text, dialogs, stdout/stderr, URLs and error text
are treated as untrusted and potentially sensitive.  The standard policy
records useful structural facts—process state, element/dialog counts, presence
of output/error/URL and screenshot presence—but suppresses the free-form values.

This is intentional.  A target can display credentials, tokens, customer data
or adversarial prompt text.  “It came from the application under test” is not a
basis for treating it as safe evidence.

## Findings and assertions

Assertion results and methods remain deterministic structural evidence.  The
expected value is redacted and the observed/actual value is suppressed by
default.  Deployments that explicitly require the actual value may route that
context to a protected sink.

Finding source and allow-listed classification remain machine-readable.  Model
or runtime prose—title, expected/actual details and explanation—is classified
before append and suppressed by the standard policy.  Roam continues to produce
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
> a protected sink was unavailable, an exception was raised, or a target/model
> placed the value in an unexpected free-form field.
