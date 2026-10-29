# Seeded causal-graph reliability campaign

## Why this is separate from the original experiment

The exhaustive demo covers all 252 interleavings of two fixed one-step actors.
This campaign exercises different lifecycle semantics: 3–5 actors, two steps per
actor, 2–3 outstanding calls per step, ordered checkpoints, repeated usage
updates, and dependencies between actors. Tool outcomes include success, error,
and cancellation; final step outcomes cover all three as well. Error/cancelled
tools can be handled by a successful step, but unsuccessful steps cannot hide
inside a completed run.

The synthetic DAG is a test generator, not a new event schema or a runtime
adapter. Cross-actor dependencies constrain the generator's schedules; trace v1
neither records nor validates those extra causal edges. No execution, tools,
provider requests, real traces, or private company data are involved.

## Reproduce offline

From the checkout root with Python 3.11+ and only the standard library:

```sh
python -m demos.causal_campaign
python -m unittest discover -s tests -v
```

The default seeds are 3, 17, and 101, with four schedules each. Counts and canonical
trace hashes are saved in [causal-result.json](causal-result.json); a test compares
the whole report with a fresh run. SHA-256-based priorities, sorted tie-breaking,
and stable node identities avoid process hash randomization and PRNG-version
assumptions. The CI matrix runs this same gate on Ubuntu/Windows and Python
3.11/3.14. These are proposed checks until the new commit's CI actually passes.

Expected report:

- 3 graphs, 12 sampled topological schedules, 3–5 actors, 70/92/108 events.
- 1,068 nonempty interrupted prefixes checked against a declarative state oracle.
- The same 1,068 prefixes rejected without partial mode, and 1,068 terminal
  cancellations checked without dropping pending steps or calls.
- 1,068 prefix-plus-suffix full replay parity checks. This reruns the trace from
  zero; it is **not** incremental snapshot recovery or a resume executor.
- 60 independently witnessed invalid traces rejected: duplicate result, result
  before its call, step finishing with pending calls, completed run with pending
  steps, and events after a terminal run.
- 48 planted tool digest/status, checkpoint digest, and token-usage drifts detected.

These counts are selected synthetic cases, not coverage percentages or estimates
of real-world bug detection. The sampled graph schedules are not exhaustive.

## Independent oracle and harness sensitivity

The generator supplies validity through a DAG. The oracle then projects expected
state from sets of started/finished steps and calls, checkpoint order, and usage
sums. It does not call the replay implementation, comparator, or their validators.
Thus the campaign is not merely comparing replay with another invocation of
itself. The original API tests and exhaustive experiment are retained unchanged.

Harness self-tests deliberately inject three faulty implementations:

1. Drop unfinished calls from the report: the independent prefix oracle rejects it.
2. Silently remove duplicate results: the invalid-trace probe rejects it, producing
   a five-event reproducer (run start, step start, call start, two results).
3. Treat every comparison as equivalent: an observational drift probe rejects it.

These are seeded test doubles, **not** discovered bugs in the current replay
implementation. They establish sensitivity to the named failure classes only.

## Minimized failure evidence

A failed gate exits 1 and prints the failure label plus a synthetic JSONL-equivalent
counterexample in its JSON report, so the CI log contains a reproducer. Optionally
persist it locally:

```sh
python -m demos.causal_campaign --failure-dir test-temp/causal-failure
```

On a detected failure only, this creates a new directory containing
`counterexample.jsonl` and `failure.json`. Comparison failures additionally save
`baseline.jsonl` and `candidate.jsonl` after reduction, and print both traces in
the failure report. Their metadata identifies the replay mode and expected
comparison, so a counterexample does not require reconstructing its other side.
Repeated-replay mismatches save two identical input traces: their context states
that the bug is differing strict replay outputs, not observational input drift.
It refuses to overwrite an existing directory. A successful run creates no
artifacts and retains its existing report format.

Failure reports include `artifact_status`: `not_requested` without
`--failure-dir`, `saved` after every file was written, or `failed` if saving
encountered an I/O or invalid-path error. In the last case, the report also has
`artifact_error: "unable_to_save_failure_artifacts"`; paths and exception text
are not printed. The original failure label, context, minimality, counterexample,
and comparison baseline remain in stdout JSON, and the gate still exits 1.
The library `save_failure()` continues to raise its filesystem exceptions.

Saving is not transactional: an interrupted write can leave incomplete files in
the newly created directory. A failed status does not claim which files survived;
use the stdout evidence and a new destination for a later attempt. There is no
automatic deletion, overwrite, or retry. This fallback requires writable stdout;
it does not cover process termination or unexpected programming errors. Self-tests
exercise existing destinations, invalid paths, permission/encoding errors, partial
writes, successful roundtrips, the failed exit code, and no-write success.

The reducer removes events and renumbers sequence fields while preserving the
specific failure predicate. Valid-state failures retain a dependency-closed DAG
selection; invalid-state failures retain their independent violation witness.
For repeatable failures, reduction reaches **1-minimality under single-event
deletion within the predicate's validity constraints**, not a globally smallest
trace. Distinct failure predicates may produce different counterexamples. A
strict-mode rejection preserves a complete, dependency-closed trace. An append
mismatch is reduced only when two strict replays reproduce differing outputs;
rejection is not allowed to masquerade as that mismatch.

If a failure cannot be reproduced consistently during reduction, the original
trace is saved with `minimality` explicitly saying **not reduced**. Strict and
append failures also retain the seed, schedule index, replay mode, and (for an
append failure) prefix length, since a stateful failure may require rerunning the
campaign rather than replaying one trace in isolation. Regression tests cover
strict-only rejection, append-only rejection, persistent repeat-output drift,
and transient failures. The CLI still emits the typed failure and saved artifact
instead of leaking an unhandled `TraceError`, `AssertionError`, or non-reproduction
`ValueError` from those paths. All examples remain synthetic; no payload sanitizer
is implied.

## Boundaries

The campaign does not change the public library, CLI, or contract. It does not
prove the correctness of the oracle itself, exhaust all DAGs, model unrecorded
side effects, or certify any provider/model. Its append-recovery metric still
means full replay from zero, not snapshot recovery. Separate snapshot regression
tests reuse these schedules to verify prefix-bound snapshots and suffix append
at every boundary; see [the snapshot contract](../docs/design.md#snapshot-v1-contract-and-trust-boundary).
Resource policies, adapters, and bounded-state replay remain roadmap work.
