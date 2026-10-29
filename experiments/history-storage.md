# Exact history on disk

The [scaling baseline](scaling.md) found growing Python allocation peaks and
append costs as finished steps, calls and checkpoints accumulated. Those records
are not disposable: the v1 contract reports them and rejects reuse of historical
identities. Removing them would change validation or observations. A compact
snapshot would also need a new contract and a justified trust model.

This change takes a narrower, useful step toward the bounded-state roadmap:
`SQLiteReplayState` retains exact history on disk and avoids growing Python
history maps during append. It is an opt-in library backend, with unchanged v1
events, reports and snapshots. The default in-memory replay and CLI are unchanged.
This is not history compaction, a bound on process memory, or constant-time resume.

## Use and ownership

Python 3.11+ with its standard-library `sqlite3` module is sufficient; no package
installation, provider calls or credentials are needed.

```python
from weasley_trace_replay import read_events
from weasley_trace_replay.sqlite_history import SQLiteReplayState

# The path must not already exist. Keep the source event log as the authority.
with SQLiteReplayState("new-history.sqlite") as state:
    state.extend(read_events("examples/completed.jsonl"))
    report = state.report()  # Materializes ALL observations in Python memory.
```

The caller owns the path and must keep exclusive control of the file during the
object's lifetime. It is not a shared database or an authenticated store. Existing
files are rejected, even if they came from a previous instance. Closing releases
the connection and retains the file for inspection; it does not create a resumable
checkpoint. Initialization failure may leave an empty or partial new file.
Path creation errors retain their filesystem exception types, including
`FileExistsError` when the destination is already present.
Relative paths are made absolute before both creation and SQLite connection, so
SQLite's special `:memory:` spelling cannot silently select another storage mode.

Snapshots are detached verification records in the existing format. To continue:

```python
with SQLiteReplayState.from_snapshot(saved, prefix_events, "new-rebuilt.sqlite") as state:
    state.extend(suffix_events)
    report = state.report(allow_partial=True)
```

This replays the entire supplied prefix, verifies canonical snapshot state,
count and hash, and only then returns a usable cursor. Serialized state is never
installed and a digest never seeds the hash. Prefix replay and snapshot
materialization remain costs. Preserve the original prefix separately; neither
the database nor a digest replaces it.

## Atomicity and failures

Each append stages its canonical hash/count and runs an explicit SQL transaction.
Only a successful commit publishes the new Python hash/count. Contract rejection
rolls back, leaves the previous report/snapshot intact, and permits a valid next
event. `extend` preserves its valid earlier events if a later event fails.

Once initialized, a SQLite operation error raises `HistoryStorageError` and
invalidates the handle. Reports,
snapshots and appends then fail. This conservative rule also covers a commit whose
success was not acknowledged, and rollback failure. It does not assume every SQL
error has the same transaction outcome; see [SQLite transaction behavior](https://www.sqlite.org/lang_transaction.html).
Reconstruct a known verified prefix into a new file and append the intended suffix.
The tests inject failures after a write, after a real commit but before its
acknowledgement, during rollback, and during reads. These are deterministic
library-boundary faults, not power-loss tests or real-agent process recovery.

Historical IDs use SQLite's default binary text equality. Indexed lookups retain
actor ownership and pending work; checkpoints retain event order. Completed IDs
stay reserved after reconstruction. Reports and snapshots allocate complete,
detached maps, using memory proportional to history. Validation and canonical
schema helpers are shared with the existing backend, while lifecycle transitions
are independently implemented and checked differentially.

## Resource protocol

```sh
python -m demos.history_storage --rounds 4 16 64 --repeats 3 > history-storage-result.json
python -m unittest discover -s tests -v
```

The evaluator reuses PR6's deterministic workloads and independent full-report
oracle. Three sizes, two backends, three repeats and three measurement passes
produce 54 fresh serial worker samples. The passes measure append time without
tracing, append Python allocations, and full-report Python allocations separately.
Fixture generation, empty-state/schema setup, imports, initial GC, validation,
file-size inspection and output are excluded. For report allocations, the entire
state already exists before tracing starts. The fixture list remains resident.

Every sample must match the independent report, including canonical input hash,
identity, counts, all observations and token totals. Source hashes cover both
backends, workload/oracle and evaluator; environment and effective SQLite settings
are checked for every worker. Malformed/failed/timed-out workers and mismatched
identities abort the aggregate without partial successful evidence.

The backend uses `DELETE` journaling, `FULL` synchronization, `mmap_size=0` and a
suggested 2 MiB SQLite page cache (`cache_size=-2048`). Effective settings and
SQLite version are recorded. The [cache setting](https://www.sqlite.org/pragma.html#pragma_cache_size)
is not a whole-process memory limit. `tracemalloc` excludes native SQLite memory,
pre-existing fixtures, most interpreter memory and RSS. Database sizes are sampled
after close; they are not bytes written, total I/O, or peak journal sizes.

Per-event durable SQLite writes and volatile in-memory append have different
costs and guarantees. Times describe these particular operations, not a general
performance contest. Three samples on a shared host do not establish statistical
significance, production scale, or a distribution of real traces. No performance
threshold determines correctness.

## Acceptance evidence

Backend tests compare every causal-campaign prefix against both the existing
replay and the campaign's independent graph oracle. Growing-history fixtures use
a separate expected-report projection; reconstruction checks every boundary of
a fixture with historical and pending work. Negative cases cover schema/order,
identity reuse, ownership, terminal states, usage overflow, checkpoint ordering,
detached outputs, changed snapshot facts and deterministic storage failures.

[history-storage-result.json](history-storage-result.json) records the completed
54-sample run on October 23, 2026 KST (October 22 UTC): Windows 11, CPython 3.14.5,
SQLite 3.50.4, Intel Family 6 Model 183, 32 reported logical CPUs. The source
fingerprints match the measured implementation. Medians of three samples were:

| Events | Memory append (ms) | SQLite append (ms) | Memory append peak (KiB) | SQLite append peak (KiB) | Database (KiB) |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 130 | 7.397 | 1,133.164 | 31.282 | 23.121 | 40 |
| 514 | 95.274 | 4,881.760 | 115.401 | 23.449 | 68 |
| 2,050 | 1,301.884 | 22,240.653 | 451.402 | 23.574 | 164 |

Full-report allocation is a separate boundary, and it still grows:

| Events | Memory report peak (KiB) | SQLite report peak (KiB) |
| ---: | ---: | ---: |
| 130 | 22.478 | 32.278 |
| 514 | 82.103 | 119.145 |
| 2,050 | 322.103 | 470.832 |

SQLite append traced allocation stayed near 24 kB across these fixtures, while
disk history grew and per-event commits were substantially slower on this host.
SQLite's full report allocated more Python memory in these measurements; its
rows need new Python values, while the memory backend already holds values before
the report pass. This supports an exact storage option for callers avoiding
growing Python history maps, not a recommendation to change the default backend.
Native page-cache/RSS measurements and larger workloads would be necessary before
claiming a total-memory benefit. No compaction or real-runtime recovery was tested.

The host was shared, and local correctness tests overlapped part of this run.
Scheduling and filesystem load were uncontrolled. Timing is descriptive, not an
isolated performance comparison; no significance or general speedup is claimed.
Results reuse PR6's fixtures but have different measurement boundaries, so its
full-replay numbers are not direct append-only comparators.

The fresh PR6 baseline (`4a9fe77f838a0fc7a591b84632e8eb24dac66be9`) passed 76
tests; this branch passed all 98, with zero failures, errors or skips. An isolated
combination with the unchanged drafts #2–#5 passed all 121, also with zero
failures, errors or skips. The disk backend supports v1 reports; that integration
check does not add PR3's opt-in v2 interface to this new backend. Existing causal
and interleaving experiments matched committed evidence, and CLI smoke,
`compileall` and whitespace checks passed. No linter or typechecker is configured.
Independent review found and resolved the POSIX special-filename issue before
measurement. CI retains the existing OS/Python matrix and runs the small protocol
tests, without machine-dependent performance thresholds.
