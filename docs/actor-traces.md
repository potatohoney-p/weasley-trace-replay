# Opt-in actor-local trace fidelity

Default v1 comparison intentionally compares final recorded observations, not
all event order. For example, one actor may finish call A then B, or B then A,
and produce identical tool maps, digests, usage and checkpoints. Both traces
are valid and default comparison is equivalent. This is a documented boundary,
not a defect in the existing oracle.

`compare_actor_traces(left_events, right_events, allow_partial=False)` adds an
explicit stricter mode for callers that need to retain each actor's recorded
local sequence while allowing global scheduling changes. The CLI is:

```sh
python -m weasley_trace_replay diff examples/actor-traces/sequential.jsonl examples/actor-traces/reordered.jsonl
# exit 0: unchanged default observational comparison
python -m weasley_trace_replay diff examples/actor-traces/sequential.jsonl examples/actor-traces/reordered.jsonl --actor-traces
# exit 1: same observations, different actor-local trace
python -m weasley_trace_replay diff examples/actor-traces/sequential.jsonl examples/actor-traces/interleaved.jsonl --actor-traces
# exit 0: different global interleaving, same actor-local traces
```

These fixtures are newly authored synthetic events, not runtime captures,
provider results or evidence of equivalent real-world side effects.

## Exact contract

Both event iterables are consumed once and validated using normal ReplayState
append and final-report checks before a result is returned. Invalid events on
either side reject the operation. `allow_partial` admits a running EOF, not bad
syntax or lifecycle order. Failed/cancelled terminals preserve pending work.

For every event with an `actor`, remove **only** `seq` and `run`. Include all
remaining v1 fields, including `v`, `type`, `actor`, `step`, `call`, `tool`, outcome,
digest and token counts as applicable. Serialize sorted-key compact ASCII JSON,
append one LF byte per event, and hash this ordered sequence with SHA-256. Keep
an exact event count too. Object-key ordering and source whitespace do not matter.
Actor IDs are the pairing key; actor/step/call IDs must be stable. An absent actor
is not an empty actor record. Changes are sorted by actor ID.

The result has `comparison_version: 1`, `mode: "actor-local-v1"`, the existing
v1 observation comparison under `observations`, actor fingerprint differences
under `actor_traces`, both exact canonical trace hashes, and overall `equivalent`.
Overall equivalence requires both observation and actor-local equality. Run-level
start/finish records have no actor and are excluded from actor fingerprints;
normal validation and observation comparison still enforce run lifecycle and
terminal status. Exact full trace hashes retain seq/run, so they may differ
when actor-local comparison passes. They are evidence identifiers, not equality
criteria or authentication.

## Deliberately stricter, not causal equivalence

- Reordering across actors is ignored; the mode does **not** prove actors are
  independent or reconstruct cross-actor dependencies, messages or happens-before.
- Within one actor, all recorded order is exact. Even valid overlapping calls
  finishing in a different order differ. No independence assumption is made
  within an actor, and no ordering is inferred beyond what the trace recorded.
- Checkpoint placement relative to tools, repeated usage segmentation, explicit
  zero usage versus missing usage, and event values all matter. This is actor-local
  trace fidelity, not merely checking tool names or final outcomes.
- `--actor-traces` and `--policy` are mutually exclusive. Token tolerances cannot
  silently override differences in the strict local projection.
- Hash equality is not a mathematical proof of identical sequences, authentic
  execution, anonymization or semantic equivalence. No side effect is replayed.

Default replay/report/snapshot formats and comparison results are unchanged.
This API consumes traces rather than extending serialized snapshots or trusting
saved hashes. It uses existing in-memory replay and retains ordinary full reports;
additional projection state is one hash/count per actor. It is **not bounded-memory
replay**, compaction, or a SQLite comparison API.

## Validation

`python -m unittest discover -s tests -p test_actor_traces.py -v` checks the valid
same-actor reorder boundary, all 70 interleavings of two four-event actors against
an independent explicit-list oracle, overlapping calls, step/checkpoint/usage
changes, terminal/pending work, malformed traces, single consumption, input
immutability, canonical bytes, CLI exits and incompatible policy selection.
