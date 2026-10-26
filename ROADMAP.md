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

The product capabilities are validated replay, verified replay snapshots, and
differential comparison; the causal campaign strengthens their evidence. Individual
files, tests, examples and commits are not extra capability or repository units.

## Next priorities

1. **Synthetic runtime adapters.** Convert reviewed, synthetic Assistant/Subagents
   events into the contract without importing runtime packages or raw content.
   Document information lost by conversion and fail on ambiguous call identity.
2. **Bounded replay state.** Benchmark large synthetic traces and preserve exact
   validation across compaction; publish environment and memory measurements.

Snapshots are not constant-time restarts, authenticity/attestation, agent process
recovery, or opaque checkpoint restoration. Prefix replay and growing history
remain intentional costs; there is no compaction or imported runtime state.

No provider calls, hosting, public release, or real trace ingestion is part of
the current minimum. The implementation is proposed through a Draft PR above
a minimal README bootstrap; it is not merged into the private default branch.

## Opt-in actor-local fidelity

Require both existing observation equality and equal canonical actor-local event
projections, without changing default comparison. Acceptance: a valid same-actor
call reorder is visible; all order-preserving cross-actor interleavings remain
equivalent; invalid/partial inputs and policy incompatibility are explicit.
This detects recorded local trace drift, not cross-actor causal or semantic drift.
