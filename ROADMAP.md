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

## Next priorities

1. **Synthetic runtime adapters.** Convert reviewed, synthetic Assistant/Subagents
   events into the contract without importing runtime packages or raw content.
   Document information lost by conversion and fail on ambiguous call identity.
   The historical tau2 adapter is a separate first compatibility slice; it does
   not provide live runtime integration or general tau2 coverage.
2. **Bounded replay state.** Benchmark large synthetic traces and preserve exact
   validation across compaction; publish environment and memory measurements.

Snapshots are not constant-time restarts, authenticity/attestation, agent process
recovery, or opaque checkpoint restoration. Prefix replay and growing history
remain intentional costs; there is no compaction or imported runtime state.

No provider calls, hosting or live agent ingestion is part of this capability.
The reviewed external input is a historical simulated benchmark episode, not a
real customer conversation or a reproduced provider run. Proposed changes remain
subject to their Draft PRs; this roadmap does not assert a merge or release.
