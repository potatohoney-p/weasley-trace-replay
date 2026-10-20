# Weasley Trace Replay

Offline lifecycle replay and differential testing for multi-agent traces.
Reconstruct what completed, failed, or remained unfinished; compare two runs
without executing a model or repeating a tool side effect.

This is a new private portfolio project. It is not a runtime, benchmark runner,
or claim about any model's quality. All included traces are synthetic.

## Run the working demo

Python 3.11+; no runtime dependencies or API keys.

```sh
python -m unittest discover -s tests -v
python -m demos.experiment
python -m weasley_trace_replay replay examples/completed.jsonl
python -m weasley_trace_replay replay examples/interrupted.jsonl --allow-partial
python -m weasley_trace_replay diff examples/completed.jsonl examples/drift.jsonl
```

The last command intentionally exits **1** and identifies a changed tool output
digest. Exit **0** means valid replay/equivalent comparison; **2** means an invalid
or unreadable trace. Omit `--allow-partial` to reject traces without a terminal
run event. Library callers receive detailed positional `TraceError` diagnostics;
the CLI deliberately emits a content-free error code.

Optional local installation: `python -m pip install --no-deps .` provides the
`weasley-trace-replay` command. Installation may fetch the build backend;
the commands above work directly from the checkout without installation.

## What it demonstrates

- A strict versioned JSONL contract, with contiguous sequence numbers and explicit
  run, actor, step, and call identity. Malformed, mixed, duplicated, or out-of-order
  events cannot silently turn into successes.
- Deterministic state reconstruction across interrupted runs: replay the valid
  prefix to inspect pending work, then replay the appended trace. No invented
  completion, background work, filesystem mutation, or network request occurs.
- Differential testing that tolerates independent actor interleavings while
  locating changes to tool outcomes, checkpoint digests, usage, and terminal state.

## Design and evidence

See [the contract and design rationale](docs/design.md),
[the reproducible experiment](experiments/README.md), and [roadmap](ROADMAP.md).
The synthetic experiment exhaustively enumerates all 252 order-preserving
interleavings of two five-event actors and checks three known output mutations.
It measures replay invariants, not agent accuracy or performance.

Existing Weasley Assistant tracing records runtime-specific JSONL and displays
it as a timeline. Subagents owns execution and recovery; DeepMind owns durable
memory; AutoResearch optimizes measured experiments. This project supplies a
separate, provider-independent event contract and observational differential
oracle. It does not import those runtimes or repackage their code.

## Limitations

Traces must already use this contract. There is no automatic sanitizer or legacy
trace importer. Prompt text, tool arguments, raw results, URLs, and free-form
metadata are forbidden fields, but identifiers and hashes can still disclose
information. Review any future real trace before sharing it.

Observational equivalence requires stable actor/step/call identifiers and compares
only recorded evidence. It does not prove unrecorded side effects are equivalent,
validate model reasoning, or replace a real tokenizer. Checkpoints are opaque
digests; the tool does not restore application state from them. A failed tool can
be handled inside a successful step; a failed step cannot end a completed run.

EOF without a final newline is accepted if the last JSON object is complete.
Torn JSON is rejected even in partial mode. Each input line is bounded, while the
reconstructed step/tool maps grow with the trace. Incremental persisted cursors
and bounded-state compaction are future work.
