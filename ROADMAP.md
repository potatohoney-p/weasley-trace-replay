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
- Selective in-memory append staging that avoids copying unrelated history while
  preserving atomic rejection and detached exports. Verify with
  `tests/test_staging.py`; reproduce the explicitly synthetic usage-only timing
  experiment in `experiments/append-staging.md`. This is not history compaction.

- Loss-aware offline conversion of one pinned historical tau2 episode, with
  native call pairing, explicit outcomes, exact output hashing and a narrowly
  supported conversation terminal. Verify the independent literal oracle,
  every-prefix resume, rejected ambiguous inputs and output drift with
  `python -m demos.tau2_compatibility` and `tests/test_tau2*.py`. See the
  [compatibility protocol](experiments/tau2-compatibility.md) for provenance,
  omitted observations, incomplete usage and optional SQLite parity.

The product capabilities are validated replay, verified replay snapshots,
differential comparison and narrow historical episode conversion; the campaigns
strengthen their evidence. Individual
files, tests, examples and commits are not extra capability or repository units.

- Reviewed synthetic Subagents lifecycle/message projection adapter with explicit
  loss and identity mapping. Verify with `tests/test_subagents_adapter.py`; see
  `docs/subagents-adapter.md`. No production log ingestion or runtime execution.

## Next priorities

1. **Synthetic runtime adapters.** Assistant graph lifecycle projection is implemented
   with explicit source pin, caller run identity, visit/path validation, interruption
   tests and a loss ledger (see `docs/assistant-adapter.md`). Tool/LLM ingestion is
   unsupported; never guess call identity. Other runtime adapters remain separate.
2. **Bounded replay state.** Exact disk-backed history is a first storage option,
   not compaction or a total-memory bound. Use the measured allocation/storage
   tradeoffs to evaluate whether a new report/snapshot contract can justify
   compaction without losing validation or historical identity checks. Full
   reports/snapshots and verified-prefix reconstruction still grow with history.

   The historical tau2 adapter is a separate first compatibility slice; it does
   not provide live runtime integration or general tau2 coverage.

Snapshots are not constant-time restarts, authenticity/attestation, agent process
recovery, or opaque checkpoint restoration. Prefix replay and growing history
remain intentional costs; there is no compaction or imported runtime state.

No provider calls, hosted service, or real trace ingestion is part of the current
minimum. The implementation was merged through
[PR #1](https://github.com/potatohoney-p/weasley-trace-replay/pull/1), and the
repository was made public on 2026-10-21 under the [MIT License](LICENSE).
Included examples are synthetic or the attributed historical simulated tau2
episode; publication does not establish model quality.

No provider calls, hosting or live agent ingestion is part of this capability.
The reviewed external input is a historical simulated benchmark episode, not a
real customer conversation or a reproduced provider run. Integration does not
constitute a package release or establish live-provider correctness.

## Incremental disk-history reporting

The optional SQLite backend now exports individual v1 observations through
`iter_report` and `replay --report-format jsonl`. This extends the bounded-history
work beyond append: checkpoints are emitted individually and indexed scans avoid
full Python report maps. Acceptance: prefix/terminal parity, explicit completion,
no completion after read/cleanup failure, detached rows, early-close recovery,
CLI compatibility and scoped export-allocation evidence. Full snapshots and
legacy reports remain materialized; compaction, stream comparison and real-runtime
validation remain separate future work.

## Opt-in actor-local fidelity

Require both existing observation equality and equal canonical actor-local event
projections, without changing default comparison. Acceptance: a valid same-actor
call reorder is visible; all order-preserving cross-actor interleavings remain
equivalent; invalid/partial inputs and policy incompatibility are explicit.
This detects recorded local trace drift, not cross-actor causal or semantic drift.

## Incremental usage-presence fidelity

Opt-in SQLite report/stream v2 retains missing-versus-recorded-zero usage without
materializing per-step presence arrays during export. Acceptance: every synthetic
prefix matches independent event presence and in-memory v2 reports, snapshot
replay reconstructs presence, invalid/overflowing events cannot mark it, and
cleanup failures cannot publish completion. Default v1 output and snapshot schema
remain unchanged. This extends bounded reporting; it is not evidence of complete
telemetry, production adoption, or real-model performance.
