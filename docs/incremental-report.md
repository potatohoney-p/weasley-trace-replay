# Incremental SQLite report export

This opt-in transport exports the **same selected v1/v2 observations** as `report()` without
building its complete steps/tools/checkpoint maps in Python. It does not compact
history, reopen saved databases, export snapshots, compare streams, or recover an
agent process. Default replay output, comparison and snapshots are unchanged.

```sh
python -m weasley_trace_replay replay examples/completed.jsonl --report-format jsonl
python -m weasley_trace_replay replay examples/interrupted.jsonl --allow-partial --report-format jsonl
```

The CLI ingests and validates the whole bounded-line trace into a fresh temporary
SQLite history before printing anything. It then serializes each report record
separately and removes its temporary database. Invalid late trace input produces
no report. The library API is `SQLiteReplayState.iter_report(allow_partial=False, report_version=1)`;
consume or explicitly close the iterator before appending or exporting again.
A suspended iterator owns a read transaction. A second export or append fails
without changing the state. Early iterator close releases its read transaction;
closing the state instead invalidates further iteration. These objects remain
single-threaded, with no support for concurrent external database modification.

## Transport version 1

Each physical line is a JSON object. Order is:

1. Header: `format: "weasley-replay-report-jsonl"`, `format_version: 1`,
   `report_version: 1`, and the ordinary `run`, `status`, `events`, `trace_sha256`,
   `usage` fields.
2. Zero or more `{ "kind": "step", "id": ..., "value": ... }` records in
   lexicographic step-ID order. `value` is the ordinary actor/status object.
3. Zero or more `{ "kind": "tool", "id": ..., "value": ... }` records in
   call-ID order. `value` contains actor, step, tool, status and, when observed,
   digest. Pending calls do not receive invented output digests.
4. Zero or more `{ "kind": "checkpoint", "step": ..., "digest": ... }`
   records, grouped in step-ID order and in original event order within each
   step. Every checkpoint is its own record, including repeated digests.
5. Exactly one `{ "complete": true, "counts": { "steps": N, "tools": M,
   "checkpoints": K } }` terminal record, after successful read cleanup.

The ordinary report is reconstructed by mapping step/tool records by ID, appending
checkpoint digests to each step's list, copying the header observations, and
setting `version` to `report_version`. `unfinished_steps` and `unfinished_tools`
are the sorted IDs whose values have `status: "running"`. No observation is
omitted, but reconstructing the full report again costs history-sized memory.
There is no new public reader or arbitrary-report validator in this increment.

Consumers must validate supported header versions, record order/shapes, unique
step/tool IDs, checkpoint ownership, exact counts and a single terminal with no
trailing records. Reject interrupted output missing the terminal. For CLI use,
require **exit 0 as well as the terminal**; a pipe/write/flush failure is not a
successful export. `complete` means export completion, not a successfully completed
run. An explicitly allowed interrupted run can have a complete export and visible
pending work. Historical failed/cancelled runs retain their status.

Storage failures before the first record emit nothing. Later read failures can
leave a prefix, never a completion marker, and invalidate the state. The CLI
withholds completion until its database and temporary directory close. An output
failure releases the iterator and storage; this is not atomic file publication,
rollback of a sink's already received bytes, or durability across process death.
The CLI's existing content-free error/exit-2 behavior remains in effect.

## Resource and trust boundaries

Indexed table scans retain one row at a time; checkpoints use an index on
`(step, seq)` instead of sorting history or grouping an unbounded per-step list.
Identifiers and digests already have fixed contract bounds. Python working memory
for export is record-sized, plus fixed cursor/metadata objects and counters.
Native SQLite caching, operating-system buffers and integer counters are not an
entire-process constant-memory guarantee. Disk usage and output size still grow
with history, and ingestion still performs durable per-event writes. Consumers
that retain records negate the streaming memory benefit. Full `report()` and
`snapshot()` still materialize history, and prefix verification still replays it.

The v1 usage caveat is unchanged: absent measurements and explicit zero aggregate
usage are indistinguishable. Opt-in v2 retains recorded presence, not measurement
completeness or billing accuracy. Hashes establish input consistency, not authenticity,
execution proof, semantic equivalence, or model performance.

## Reproduction

`python -m unittest discover -s tests -p test_incremental_report.py -v` checks
full prefix parity, an independent terminal oracle, interrupted/failed/cancelled
work, detached values, no calls to the full-state materializer, single-step
checkpoint growth, indexed query plans, read/cleanup/output failures and CLI
parity. All fixtures are synthetic.

`python -m demos.incremental_export` records export-only traced Python allocation
samples for two synthetic histories. Input construction, ingestion, oracle and
warm-up are excluded. It serializes and discards outputs, measuring neither RSS,
native SQLite allocation, ingestion cost nor timing. Recorded local samples are
in `experiments/incremental-export-result.json`; they are observations for that
source/environment, not portable performance guarantees.

## Opt-in transport version 2: usage presence

Use `replay --report-format jsonl --report-version 2` or
`SQLiteReplayState.iter_report(report_version=2)`. The header has
`format_version: 2` and `report_version: 2`. All other header, tool, checkpoint,
completion and ordering fields are unchanged. Each step row additionally has a
required boolean `usage_recorded`, true if at least one valid usage event was
recorded for that step (including an explicit 0/0 event). Repeated usage events
do not duplicate presence. A consumer supporting only transport v1 must reject
v2 rather than discard this evidence.

To reconstruct a materialized report v2, partition the sorted step IDs into
`usage_observation.recorded_steps` and `usage_observation.unrecorded_steps`
according to that boolean. `value` itself remains only actor/status; do not
insert the boolean into the ordinary steps map. There are no additional records
or counts, and no history-sized presence array in the header. Require the same
completion/exit checks as v1. Presence says nothing about whether every call's
usage was measured, whether totals are complete, or whether costs were billed.

SQLite stores one indexed identity per recorded step in the event transaction.
Overflow or other rejected usage cannot leave presence behind. Snapshot v1 stays
unchanged: reconstruction replays the verified prefix to recover presence rather
than guessing from token totals or adding trusted serialized state. The full
`SQLiteReplayState.report(report_version=2)` is also supported and materialized.
The indexed left join streams one step at a time; native cache and disk remain
outside a Python record-sized export memory claim.

`python -m unittest discover -s tests -p test_sqlite_usage_observation.py -v`
checks each synthetic prefix against a direct input-event presence oracle and
in-memory reports, zero/absent/repeated usage, rejected overflow, snapshot replay,
invalid versions, indexed query plans, early close and read/cleanup faults.
