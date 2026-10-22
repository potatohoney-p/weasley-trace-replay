# Weasley Trace Replay

Offline lifecycle replay and differential testing for multi-agent traces.
Reconstruct what completed, failed, or remained unfinished; compare two runs
without executing a model or repeating a tool side effect.

This is an offline research toolkit. It is not a runtime, benchmark runner,
or claim about any model's quality. Lifecycle demos are synthetic; one reviewed
historical tau2 benchmark episode supplies an external compatibility fixture.

## Run the working demo

Python 3.11+; no runtime dependencies or API keys.

```sh
python -m unittest discover -s tests -v
python -m demos.experiment
python -m demos.causal_campaign
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
- Deterministic incremental state reconstruction and versioned snapshots. Resume
  replays and verifies the supplied prefix, then appends only new suffix events.
  No invented completion, background work, tool execution, or network request occurs.
- Differential testing that tolerates independent actor interleavings while
  locating changes to tool outcomes, checkpoint digests, usage, and terminal state.

## Explicit resource comparison policies

Default `compare(left, right)` and `diff` still compare recorded observations
exactly (apart from run identity/interleaving metadata, as before). Opt into a
versioned token policy only when your experiment justifies a specific tolerance:

```json
{"policy_version": 1, "usage_tolerances": {"input_tokens": 8, "output_tokens": 2}}
```

```sh
python -m weasley_trace_replay diff baseline.jsonl candidate.jsonl --policy policy.json
```

`usage_tolerances` is an explicit allowlist of the two aggregate token fields.
Each nonnegative integer is an **absolute, symmetric** tolerance: a change is
accepted iff `abs(candidate - baseline) <= tolerance`. Unlisted fields remain
exact; zero permits no difference. There are no percentages, automatic estimates,
or inferred budgets. Missing or malformed **report usage fields** are rejected,
never treated as zero. Replay has always initialized aggregate usage to zero:
a valid trace with no usage events and one with explicit 0/0 usage compare the
same. This policy cannot detect missing measurement telemetry or prove usage was
measured; it operates on the recorded aggregate, preserving that legacy behavior.
Only these token fields can be tolerated: actor/step/call identity, outcomes,
tool result digests, checkpoints and unfinished work retain exact comparison.

The policy result separates `exact_equivalent` from policy `equivalent`, includes
all rejected `changes` and all `tolerated_changes` (with delta and configured
limit), and records `policy_sha256`. Exit 0 means acceptance under that policy,
not exact equality or semantic equivalence; exit 1 means remaining drift; exit 2
means invalid/unreadable input. Errors remain content-free. Policies are strict
JSON with duplicate/unknown fields rejected and a default 64 KiB reader limit.

Library callers use `compare_with_policy(left, right, policy)` on reports returned
by replay/resume and `read_policy(path)`. The digest hashes ASCII canonical JSON
(sorted keys, compact separators, escaped non-ASCII, no trailing newline).
Key order/whitespace do not affect it. An absent allowlist entry and an explicit
zero entry have distinct policy digests even though both enforce exactness.
This digest identifies the supplied policy, not its author or appropriateness.

The resource-policy examples are synthetic. A permissive tolerance can accept a large
usage difference; choosing it is the caller's responsibility. Acceptance does not
establish task quality, tokenizer correctness, cost, speed, real runtime behavior,
or authenticity. The API validates policy and usage, not arbitrary hand-made report
provenance or the entire report schema; use replay-produced reports.

## Historical tau2 episode adapter

```sh
python -m demos.tau2_compatibility
```

This offline acceptance command maps one pinned historical June 2025 tau2
episode, checks a literal independent replay oracle, verifies every nonempty
prefix resume, and detects a counterfactual tool-output change. Native call
identity and explicit outcome are preserved; exact UTF-8 output text is hashed.
No tool or provider is executed. The narrow supported profile rejects missing,
duplicate, orphan or ambiguous calls and unsupported conversation terminals.

Use `weasley_trace_replay.tau2.adapt_episode(read_episode(path))` to obtain events,
provenance, a loss ledger and derived-field declarations. This first adapter
does not support arbitrary tau2 episodes. Completion means a supported recorded
conversation boundary, not task resolution or a verified human transfer.
Incomplete usage is omitted: replay v1's zero aggregate means **unreported** here.
Replay alone cannot detect argument-only or final-assistant-text-only changes.
See the [protocol, source verification and limitations](experiments/tau2-compatibility.md).
The [minimized fixture and provenance](examples/tau2/provenance.json) are attributed
to Sierra Research under the [upstream MIT license](examples/tau2/LICENSE).

## Verified snapshots and incremental replay

```sh
python -m weasley_trace_replay snapshot examples/interrupted.jsonl > snapshot.json
# suffix.jsonl must contain only the remaining events, with their original seq.
python -m weasley_trace_replay resume snapshot.json examples/interrupted.jsonl suffix.jsonl
```

`snapshot` accepts any nonempty valid prefix, including completed/failed/cancelled
runs. `resume` requires a terminal result unless `--allow-partial` is supplied.
Both print JSON to stdout; malformed snapshots and traces use the same content-free
error and exit 2 as replay. A terminal snapshot accepts an empty suffix only.

```python
from weasley_trace_replay import ReplayState, read_snapshot, serialize_snapshot, snapshot

saved = snapshot(prefix_events)
serialized = serialize_snapshot(saved)  # canonical strict snapshot JSON
cursor = ReplayState.from_snapshot(saved, prefix_events)
cursor.extend(suffix_events)
report = cursor.report()                # identical to full replay
```

The convenience `resume(saved, prefix, suffix, allow_partial=False)` returns that
same report. `read_snapshot(path)` parses and validates shape, but does not verify
the recorded history. Verification requires the entire original prefix: its
version, count, canonical event digest, and every saved state field must agree
with fresh replay. The reconstructed SHA-256 context then hashes the suffix;
a saved digest is never used as a hash seed.

This is a verified replay boundary, not constant-time restart or an authenticated
checkpoint. Prefix replay still costs time; each incremental append stages a
state copy for atomic rejection. History grows without compaction. Snapshots do
not restore a real agent process, replay tools, or recover opaque checkpoint data.
Anyone who can edit both prefix and snapshot can produce a new consistent pair.

## Design and evidence

See [the contract and design rationale](docs/design.md),
[the reproducible experiment](experiments/README.md), and [roadmap](ROADMAP.md).
The synthetic experiment exhaustively enumerates all 252 order-preserving
interleavings of two five-event actors and checks three known output mutations.
The seeded [causal campaign](experiments/causal-campaign.md) adds 3–5 actors,
repeated steps, parallel tools, cross-actor dependencies, independent state
oracles, every-prefix cancellation, and failure reduction. It checks 1,068
interrupted prefixes across 12 schedules, rejects 60 planted invalid traces, and
detects 48 observational drifts. Injected-bug self-tests verify that the harness
itself detects regressions. Both experiments measure replay invariants, not agent
accuracy or performance. Snapshot tests additionally cover exact full-report
parity at all 1,080 boundaries (1,068 interrupted and 12 terminal) of those same
schedules, cancellation snapshots at every interrupted boundary, hostile inputs,
historical identity reuse, and atomic rejected appends.

Existing Weasley Assistant tracing records runtime-specific JSONL and displays
it as a timeline. Subagents owns execution and recovery; DeepMind owns durable
memory; AutoResearch optimizes measured experiments. This project supplies a
separate, provider-independent event contract and observational differential
oracle. It does not import those runtimes or repackage their code.

## Limitations

Replay inputs must use this contract. The narrow historical tau2 adapter above
is the only external conversion; there is no automatic sanitizer or general
legacy trace importer. Prompt text, tool arguments, raw results, URLs, and free-form
metadata are forbidden fields, but identifiers and hashes can still disclose
information. Review any future real trace before sharing it.

Observational equivalence requires stable actor/step/call identifiers and compares
only recorded evidence. It does not prove unrecorded side effects are equivalent,
validate model reasoning, or replace a real tokenizer. Checkpoints are opaque
digests; the tool does not restore application state from them. A failed tool can
be handled inside a successful step; a failed step cannot end a completed run.

EOF without a final newline is accepted if the last JSON object is complete.
Torn JSON is rejected even in partial mode. Each input line is bounded, while the
reconstructed step/tool maps grow with the trace. Snapshot reads are capped at
16 MiB by default, including in the CLI; library callers may explicitly increase
`read_snapshot(max_bytes=...)`. Bounded-state compaction remains future work.

## License

Licensed under the [MIT License](LICENSE).
