# Compare live SQLite histories without materializing reports

`weasley_trace_replay.history_comparison.compare_histories(left, right)` compares
two distinct, live `SQLiteReplayState` objects. Each object must still exclusively
own its database. Inputs must already have been ingested and validated through
that API; this is not a reader or validator for arbitrary JSONL or existing files.

```python
from weasley_trace_replay.history_comparison import compare_histories
from weasley_trace_replay.sqlite_history import SQLiteReplayState

# Paths must not already exist. Callers supply and review the trace events.
with SQLiteReplayState("baseline.sqlite") as left, SQLiteReplayState("candidate.sqlite") as right:
    left.extend(baseline_events)
    right.extend(candidate_events)
    result = compare_histories(left, right)
```

Pass `allow_partial=True` explicitly to compare interrupted runs. Empty states,
closed/invalidated states, two references to the same object, or a state with an
already active export are rejected. The comparison owns only the iterators it
creates, not the states or any preexisting export. It closes those iterators on
success and failure. Healthy states remain available for later reads or appends.
These objects are not thread-safe and concurrent external database mutation is
unsupported.

## Semantics and result

The `equivalent` decision matches ordinary `compare(left.report(), right.report())`
for **v1** observations. Run identity, trace hash, event count and global event
interleaving are ignored. Status, aggregate usage, sorted step/tool observations,
and checkpoint order within each step are exact. Pending work is encoded in the
step/tool statuses, so it cannot disappear from comparison. Repeated checkpoints
remain significant. Absent usage and explicitly recorded zero retain the v1
ambiguity. This API does not compare v2 usage presence, actor-local event ordering,
resource tolerance policies, or semantic/causal equivalence.

The result contains `comparison_version: 1`, `mode: "sqlite-observations-v1"`,
`equivalent`, `first_difference`, and `compared_records` counts for both sides.
A comparison record is the status/usage summary followed by step, tool, and
checkpoint records in the producer's canonical order. Counts include that summary
but exclude export headers and terminal completion markers. Equal observations
have `first_difference: null`. Otherwise it contains a zero-based `record_index`
and detached `before`/`after` records, with `null` for an exhausted side.

This is the first **aligned record** difference, not a complete path-wise diff.
For example, inserting a step can align it against a different step or the start
of the next section. The witness establishes a mismatch, but does not attribute
all subsequent drift. Use the existing materialized `compare` for its full list
of changed paths when history-sized memory is acceptable. Editing the witness
cannot modify either input state.

## Failure and resource boundary

Both exports are fully consumed even after a difference is found. No conclusion
is returned unless both completion markers have been reached, both streams have
ended, and their read-transaction cleanup succeeded. Late storage failures,
including cleanup failure after a known mismatch, still raise. A storage failure
invalidates the affected state according to the existing SQLite contract; rebuild
it from a verified prefix in a new database. The comparison does not swallow an
error or treat a truncated export as inequality/equality.

The comparator retains at most one current producer record per side, one first
mismatch pair, and counters. It never calls `report`, `snapshot`, or the full-state
materializer. Existing v1 identifier bounds (80 characters), digests (64 hex
characters), and fixed observation schemas bound individual records; a step's
checkpoint list is never grouped. Integer counters grow logarithmically with the
number of records. Indexed producer scans and fixed SQLite caches are inherited,
not a whole-process constant-RSS guarantee. Database files, ingestion work, native
SQLite memory, OS buffers and original input retention are outside this scoped
working-memory claim. All history must still be ingested and read. No compaction,
constant-time restart, authenticity, provider execution, or model quality is
established.

## Verification

`python -m unittest discover -s tests -p test_history_comparison.py -v` checks
prefix parity, observation mutations, global interleavings, metadata and zero-usage
invariance, detached witnesses, pending/failed/cancelled runs, no materializer
calls, active export ownership, and late read/cleanup failures on either side.
All fixtures are synthetic. Existing replay/report APIs and CLI behavior are
unchanged. The JSONL reader and report-v2 producer are separate capabilities.
