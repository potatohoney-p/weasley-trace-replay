# Replay scaling protocol v1

The causal campaign exercises correctness at up to 108 events. It does not
measure the cost of retaining a longer history. This evaluator implements the
measurement stage of the bounded-state roadmap: a reproducible workload,
explicit measurement boundaries, independent correctness gates, and raw samples.
It does not implement compaction or change replay behavior.

## Run offline

Python 3.11+, standard library only, from the checkout root:

```sh
python -m demos.scaling --rounds 4 16 64 --repeats 3 > scaling-result.json
python -m unittest discover -s tests -p test_scaling.py -v
```

The first command runs 36 serial worker processes: three sizes, two operations,
three repeats, and separate timing/allocation passes. `--actors` defaults to 4,
`--calls` to 2, and `--timeout` to 120 seconds per worker. Larger dimensions are
explicit opt-ins and may be expensive; the current replay implementation copies
accumulated state on every append. No dependency installation, provider call,
network access, or runtime integration is needed.

## Synthetic history

Each round opens one step per actor, starts all tools before any result, records
one checkpoint and one usage event per step, finishes tools in reverse order,
then completes the steps. Alternating tools return `ok`/`error`; those errors are
handled within successful steps. IDs are unique across the entire run. Finished
step/call/checkpoint maps accumulate while active concurrency stays fixed.

| Rounds | Events | Historical steps | Historical calls | Resume prefix events |
| ---: | ---: | ---: | ---: | ---: |
| 4 | 130 | 16 | 32 | 77 |
| 16 | 514 | 64 | 128 | 269 |
| 64 | 2,050 | 256 | 512 | 1,037 |

The resume boundary follows the tool starts of the middle round. With the default
shape, it retains four active steps, eight pending tools, and completed historical
steps. It therefore exercises ownership and history restoration, not merely an
empty boundary. Every workload ends completed; this is one history-growth shape,
not a representative distribution of real agent traces or an exhaustive search.

## Measurement boundaries

Every sample runs in a new Python interpreter with `PYTHONHASHSEED=0`. Imports,
fixture construction, an initial garbage collection, oracle validation, source
hashing, and JSON output are outside the measured region. Garbage collection
remains enabled during the operation. Workers run serially within this command;
other host workloads are not controlled.

- **replay:** `replay(iter(records))`, including validation, incremental state
  copying, canonical hashing, and final detached report construction.
- **verified_resume:** `resume(saved, iter(prefix), iter(suffix))`, including
  snapshot shape validation, the entire prefix replay and comparison, suffix
  replay, and final detached report construction. Snapshot construction and its
  serialize/parse roundtrip occur before measurement. This is not suffix-only
  restart, constant-time recovery, or resumption of a real agent process.

Fixture records, prefix/suffix lists, and the saved snapshot are already resident
before measurement. Resume setup itself uses replay to construct the snapshot,
so it warms code and allocator state before measurement. Fresh processes do not
make these two operations a controlled cold-start comparison; interpret each as
the specific operation above.

Timing uses `perf_counter_ns` with allocation tracing disabled. A separate fresh
worker uses `tracemalloc` with one traceback frame and records peak and retained
traced bytes while the returned report is alive. These are **Python allocations
made during the operation**, excluding pre-existing fixtures/snapshot, native
allocations, interpreter memory, and process RSS. Allocation tracing adds overhead;
its wall time is deliberately not reported as replay timing.

## Correctness and failure gates

Each worker validates the entire report against a separate projection of workload
facts: historical step/call/checkpoint maps, outcomes, token totals, empty pending
work, run identity, event count, version, and the canonical input SHA-256. Expected
maps and counts are derived from workload dimensions without replay/compare.
The trace digest is independently computed from the actual synthetic records
using standard JSON/hashlib, without production canonicalization helpers.
Strict JSON comparison distinguishes booleans from integers. Comparing resume
only against replay would miss shared regressions and is not the acceptance gate.

The parent pins source/environment identity before launching workers and validates
each worker's protocol, workload, operation, measurement type, oracle-report hash,
and metric types. Source text hashes normalize newline encoding. A change during
the run, invalid worker response, nonzero exit, stderr output, timeout, or parity
failure aborts the aggregate. The CLI emits a failure object and exits 1; it never
publishes a partially successful result. Exit 0 requires every sample to pass.
There is no automatic retry or performance-based pass threshold.

Tests include deliberately corrupted reports, malformed/failed/timed-out workers,
invalid sample identity and metrics, invalid configuration, real subprocess
roundtrips, and cleanup of allocation tracing after an exception. Existing replay,
snapshot, policy, and causal-campaign suites still provide broader semantic
coverage. CI runs the small protocol tests through its existing unit-test step;
it does not enforce machine-dependent latency/allocation numbers.

## Evidence and interpretation

[scaling-result.json](scaling-result.json) contains an observed run, including
Python/OS/architecture/processor, normalized benchmark and replay source hashes,
UTC recording time, workload/report fingerprints, raw samples, and medians.
The replay implementation baseline is main commit
`312ef8123da46f4f56e7f323d226de2adb94ced5`; this evaluator changes no production
replay code. Repeating the command should reproduce workload/report fingerprints,
not timing or allocation values byte-for-byte. The result file is evidence, not
a golden performance fixture.

The recorded run used Windows 11, CPython 3.14.5 (MSC v.1944, AMD64), and an
Intel Family 6 Model 183 processor with 32 reported logical CPUs. Medians of the
three independent samples per cell were:

| Events | Replay time (ms) | Verified resume time (ms) | Replay peak (KiB) | Verified resume peak (KiB) |
| ---: | ---: | ---: | ---: | ---: |
| 130 | 6.133 | 5.489 | 35.927 | 36.700 |
| 514 | 78.987 | 79.293 | 129.950 | 129.989 |
| 2,050 | 1,106.172 | 1,185.004 | 509.200 | 509.239 |

Peak values use the allocation scope above, not total resident memory. The
observed costs grow with retained history on this host. Three samples on a shared
machine do not establish confidence intervals or a significant difference between
the two operations; the raw samples and setup asymmetry matter.

This baseline can support future compaction or append-copy experiments, provided
the protocol, Python version, workload identities, environment, and correctness
gates remain comparable. It does not establish bounded memory, throughput under
production load, statistical significance, provider speed/cost, or runtime
recovery. No performance improvement is claimed by adding this evaluator.
