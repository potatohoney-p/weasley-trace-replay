# Roadmap and acceptance criteria

## Implemented minimum

- Strict v1 JSONL and lifecycle replay with visible unfinished work. Verify with
  unit tests and the interrupted synthetic demo.
- Differential comparison that tolerates independent actor interleavings and
  detects recorded result/state drift. Verify with the exhaustive 252-case demo.
- Dependency-free CLI, positional library errors, deterministic JSON reports,
  documented assumptions and regression tests.

These are two substantive capabilities: validated replay and differential
comparison. Individual files, tests, examples and commits are not extra units.

## Next priorities

1. **Generative causal-graph tests.** Fixed seeds, 3+ actors, cancellation at every
   prefix, and saved minimized counterexamples; all existing invariants retained.
2. **Verified incremental snapshots.** Bind snapshots to schema version and exact
   prefix hash; reject edited/truncated prefixes; show parity with full replay.
3. **Synthetic runtime adapters.** Convert reviewed, synthetic Assistant/Subagents
   events into the contract without importing runtime packages or raw content.
   Document information lost by conversion and fail on ambiguous call identity.
4. **Resource-aware differential policies.** Explicit token tolerances and allowlists
   with a machine-readable policy digest; never silently ignore outcome drift.
5. **Bounded replay state.** Benchmark large synthetic traces and preserve exact
   validation across compaction; publish environment and memory measurements.

No provider calls, hosting, public release, or real trace ingestion is part of
the current minimum. The implementation is proposed through a Draft PR above
a minimal README bootstrap; it is not merged into the private default branch.
