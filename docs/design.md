# Contract v1 and rationale

## Optional resource policy boundary

The v1 event contract and default comparison are unchanged. The opt-in
`compare_with_policy` API and `diff --policy` accept exactly a version-1 policy
with `usage_tolerances`, an allowlist containing only `input_tokens` and/or
`output_tokens`. Limits and report counts must be exact nonnegative integers
at most 2**53 - 1; booleans and floating-point values are not counts. The reader
rejects duplicate/unknown keys, nonfinite numbers and files over 64 KiB by default.

A usage difference is tolerated only when its absolute delta is at most that
field's explicit limit. Equality, missing limits, and zero limits retain exact
behavior. Missing/extra report usage fields are errors, not defaults. Replay initializes
aggregate usage to zero even without usage events, so no-usage telemetry and
explicit 0/0 telemetry remain observationally equal. This feature does not detect
missing measurements, certify token accounting, or change that legacy behavior.
All other observations use the original differential oracle unchanged. In particular, the
policy cannot accept changed outcomes, tool result digests, ownership, checkpoints
or pending work. Run IDs/event counts/trace hashes were already outside observational
comparison and remain so; policies do not add any new identity exception.

Results partition the original differences into rejected `changes` and visible
`tolerated_changes`, retain `exact_equivalent`, and expose policy `equivalent`.
The digest is SHA-256 of sorted-key compact ASCII JSON without a newline; it
identifies the exact policy, not its author or any experiment's validity. An empty
allowlist and an explicit zero limit are distinct policy documents. There is no
relative tolerance, probabilistic comparison, automatic policy selection, general
report authentication, or claim of semantic/performance equivalence. Library
callers must supply replay-produced reports; only policy and usage receive new
validation, not the full shape/provenance of arbitrary report dictionaries.

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
JSON, duplicate events or impossible transitions. The compatible `replay()` wrapper builds a `ReplayState`; callers may also append
events incrementally or resume from a verified snapshot. There is no runtime
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
small explicit state machine over a plugin API. Snapshot verification does not
skip event validation or claim to restore application state.


## Snapshot v1 contract and trust boundary

A snapshot is one JSON object with exactly these fields:

- `format`: `"weasley-replay-snapshot"`.
- `snapshot_version`: integer 1; `trace_version`: integer 1 (booleans are rejected).
- `prefix_events`: positive integer count and next required suffix sequence number.
- `prefix_sha256`: canonical trace SHA-256 for those events, not the source file bytes.
- `state`: exact `run`, `status`, `steps`, `tools`, `active`, `checkpoints`, and `usage`.
  The step/tool maps contain completed as well as pending entries. `active` maps
  actors to running steps. Checkpoints remain ordered lists per step. Usage is
  aggregate exact integers, including repeated usage events.

The parser rejects unknown fields, duplicate JSON object keys (including identity
map keys), non-finite numbers, malformed state, unsupported versions, and torn
JSON. There is no schema migration, implicit repair, pickle, or arbitrary code
loading. Serialization uses sorted keys and the same compact ASCII JSON policy
as event hashing, followed by one newline. Snapshot object map insertion order
and source trace whitespace do not matter; checkpoint list order does.

`ReplayState.from_snapshot(saved, prefix)` first validates the snapshot shape,
then replays the entire supplied prefix with all normal lifecycle checks. It
compares the exact canonical snapshot reconstructed by replay, including count,
digest and every state field. Only that reconstructed state and its live hash
context are returned. The supplied state is never installed. `resume` then
appends suffix events without rereading the prefix or renumbering the suffix.
A saved hexadecimal digest cannot seed Python's SHA-256 continuation state;
hashing the digest plus suffix would give the wrong trace hash.

This proves consistency with the supplied prefix, not authenticity of the trace,
its author, or real-world side effects. There is no signature or attestation;
coordinated edits to both inputs cannot be distinguished from a new valid trace.
Changing a prefix while keeping the original snapshot is rejected, even if the
changed events happen to produce the same observations. Snapshots are not a
substitute for preserving the prefix. Restart is not constant-time: prefix
verification replays all its events and each append copies accumulated state.

Each `append` is atomic: validation and lifecycle changes happen on a detached
candidate before state/hash/count are committed. A rejected event leaves the
cursor reusable and unchanged. `extend` is atomic per event, not per batch; its
valid earlier events remain after a later event or input iterator fails. A
failed strict `report()` does not invalidate a partial cursor. Reports and
snapshots are detached copies. Instances are not thread-safe.

Completed/failed/cancelled snapshots may be restored for reporting, but no new
event may follow a terminal run. Failed/cancelled runs preserve pending work.
Historical step and call IDs survive every boundary, so completed IDs cannot be
reused and a recorded result cannot be accepted twice after restart. Empty
traces have no snapshot. Large histories remain unbounded in memory; snapshot
file reads default to a 16 MiB limit, configurable through the library only.
