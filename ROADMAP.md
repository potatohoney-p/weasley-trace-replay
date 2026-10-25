# Roadmap and acceptance criteria

## Implemented minimum

- Strict v1 JSONL and lifecycle replay with visible unfinished work. Verify with
  unit tests and the interrupted synthetic demo.
- Differential comparison that tolerates independent actor interleavings and
  detects recorded result/state drift. Verify with the exhaustive 252-case demo.
- Dependency-free CLI, positional library errors, deterministic JSON reports,
  documented assumptions and regression tests.
- Seeded causal-graph reliability campaign with 3–5 actors, multiple steps and
  concurrent calls, independent state oracles, interruption/cancellation at every
  prefix, and deletion-minimized synthetic counterexamples. Verify with
  `python -m demos.causal_campaign` and injected-bug harness self-tests.
- Verified versioned replay snapshots and incremental append. Resume requires and
  replays the original prefix, checks exact canonical state/count/hash, and retains
  the reconstructed hash context. Verify full-report parity at every causal
  campaign boundary and atomic rejection with `tests/test_snapshots.py`.
- Opt-in resource comparison policies with strict versioned JSON, explicit
  absolute input/output token tolerances, canonical policy digests and visible
  tolerated differences. Non-usage drift remains exact. Verify boundaries,
  malformed input, unchanged defaults and CLI behavior with `tests/test_policy.py`.
- Reproducible replay scaling evaluation with deterministic growing histories,
  independent full-report validation, separate fresh-process timing/allocation
  samples, and explicit verified-prefix resume measurements. Verify the protocol
  with `tests/test_scaling.py`; reproduce the descriptive baseline with
  `python -m demos.scaling`. See [measurement boundaries](experiments/scaling.md).
- Opt-in exact SQLite replay history: preserve historical validation and v1
  report/snapshot semantics without growing Python history maps during append.
  Verify independent prefix oracles, differential reconstruction and injected
  storage failures with `tests/test_sqlite_history.py`; reproduce append and
  report allocation tradeoffs with `python -m demos.history_storage`. See
  [ownership and limits](experiments/history-storage.md).

The product capabilities are validated replay, verified replay snapshots, and
differential comparison; the causal campaign strengthens their evidence. Individual
files, tests, examples and commits are not extra capability or repository units.

## Next priorities

1. **Synthetic runtime adapters.** Convert reviewed, synthetic Assistant/Subagents
   events into the contract without importing runtime packages or raw content.
   Document information lost by conversion and fail on ambiguous call identity.
2. **Bounded replay state.** Exact disk-backed history is a first storage option,
   not compaction or a total-memory bound. Use the measured allocation/storage
   tradeoffs to evaluate whether a new report/snapshot contract can justify
   compaction without losing validation or historical identity checks. Full
   reports/snapshots and verified-prefix reconstruction still grow with history.

Snapshots are not constant-time restarts, authenticity/attestation, agent process
recovery, or opaque checkpoint restoration. Prefix replay and growing history
remain intentional costs; there is no compaction or imported runtime state.

No provider calls, hosting, public release, or real trace ingestion is part of
the current minimum. The implementation is proposed through a Draft PR above
a minimal README bootstrap; it is not merged into the private default branch.

## Incremental disk-history reporting

The optional SQLite backend now exports individual v1 observations through
`iter_report` and `replay --report-format jsonl`. This extends the bounded-history
work beyond append: checkpoints are emitted individually and indexed scans avoid
full Python report maps. Acceptance: prefix/terminal parity, explicit completion,
no completion after read/cleanup failure, detached rows, early-close recovery,
CLI compatibility and scoped export-allocation evidence. Full snapshots and
legacy reports remain materialized; compaction, stream comparison and real-runtime
validation remain separate future work.
