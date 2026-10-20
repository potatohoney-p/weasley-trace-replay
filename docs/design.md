# Contract v1 and rationale

Every record has exactly `v: 1`, contiguous `seq` starting at zero, a `run`
identifier, a `type`, and the fields below. Unknown fields are rejected.

| Type | Additional fields |
| --- | --- |
| run_started | none |
| step_started | actor, step |
| tool_started | actor, step, call, tool |
| tool_finished | actor, step, call, status, digest |
| checkpoint | actor, step, digest |
| usage | actor, step, input_tokens, output_tokens |
| step_finished | actor, step, status |
| run_finished | status |

Identifiers contain 1–80 ASCII letters/digits/underscore/dot/hyphen and start
with a letter. Digests are 64 lowercase hexadecimal characters. Token counts
are exact nonnegative integers, bounded individually and in aggregate by
2^53−1 for portable JSON consumers. Tool/step statuses are `ok`, `error`,
`cancelled`; run statuses are `completed`, `failed`, `cancelled`.

## Lifecycle

One trace contains one run. Each actor has at most one active step; distinct
actors may interleave freely. Step IDs and call IDs are unique across the run.
Every result must match its pending call, actor and step. A step cannot finish
while its tools remain pending. Completed runs require no unfinished steps and
only successful step statuses. Failed or cancelled runs may retain unfinished
work, which remains visible in the output. Events after a terminal run fail.

`allow_partial=True` admits EOF in a running state, but does not forgive corrupt
JSON, duplicate events or impossible transitions. Appending the remaining events
and replaying from zero is deliberately simple and deterministic. There is no
resume executor or hidden tool dispatch.

## Two comparisons with different purposes

`trace_sha256` hashes canonical sorted-key JSON records with newline separators,
including sequence/run identity. It detects exact trace changes. Differential
comparison ignores identity, event count and order, comparing sorted state maps,
checkpoint sequences per step, aggregate usage and unfinished work. Reordering
independent actors is equivalent; changing a tool result digest is not.

This separation avoids treating benign concurrent scheduling as nondeterministic
behavior. It also states the boundary: different internal sequences that end in
the same recorded observation can compare equal. Per-step checkpoint ordering
still matters. Tool IDs must remain stable across candidate implementations.

## Content minimization

The reader accepts bounded UTF-8 lines and rejects duplicate JSON keys and
non-finite numbers. It never prints unknown payload contents. The schema has no
raw prompt, output, path, URL, headers, credentials or free-form error message.
This reduces accidental content retention; it is not proof of anonymization.
Even a digest can reveal a low-entropy value through guessing. Use synthetic
traces until a reviewed capture adapter and threat model exist.

## Deliberate scope

The implementation uses the Python standard library and stores state in memory.
This makes the demo reproducible without network access or services. It favors a
small explicit state machine over a plugin API. Future persisted snapshots must
bind to the exact canonical prefix hash and contract version before they can be
trusted; they must not silently skip event validation.
