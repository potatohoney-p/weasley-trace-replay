# Reviewed synthetic Subagents projection

`weasley_trace_replay.subagents.adapt_synthetic_subagents(records)` converts a
strict, manually reviewed **synthetic projection** of Subagents lifecycle and
child messages into native replay events. It does not read production logs,
import the runtime, run a provider, or execute a tool. The function returns an
adapter identifier, source pin, projection digest, events, and explicit loss and
derived-field lists. Native replay, comparison, and verified-prefix resume work
on those events without changing the v1 contract.

## Source and projection boundary

The source pin is [weasley-subagents 72901e3f17eea4d56418b24e7a63f10c7ae22375](https://github.com/potatohoney-p/weasley-subagents/tree/72901e3f17eea4d56418b24e7a63f10c7ae22375).
Relevant source locations:

- `src/shared/types.ts`: lifecycle artifact version 3.
- `src/runs/background/subagent-runner.ts`: `appendChildEvent` adds child run,
  step and agent attribution. Run/step lifecycle records have explicit boundaries.
  `subagent.run.completed` carries a status, which is not necessarily success.
- `test/support/helpers.ts`: `completedWrite` demonstrates assistant toolCall IDs
  and toolResult.toolCallId pairing. Identity-free toolStart/toolEnd helper events
  are not sufficient for pairing and are deliberately unsupported here.

The fixture in `tests/test_subagents_adapter.py` is newly authored synthetic
input using those field meanings, not a copied runtime trace or proof that the
runtime emitted it. Its purpose is to test the adapter and explain its input.
No upstream implementation or fixture code is bundled. Source evolution requires
reviewing a new adapter version; this is not a general Pi compatibility claim.

Each record must have exactly the following projected fields. Extra fields are
rejected so raw production logs cannot silently pass through this interface:

- Run start: `type: subagent.run.started`, `lifecycleArtifactVersion: 3`, `runId`.
- Run terminal: the same fields, `type: subagent.run.completed`, and `status`
  equal to `complete`, `failed`, or `stopped`.
- Step start: `type: subagent.step.started`, `runId`, `stepIndex`, `agent`.
- Step terminal: the same fields, a type of `subagent.step.completed`,
  `subagent.step.failed`, or `subagent.step.stopped`, and integer `exitCode`.
  Completed requires zero; failed/stopped require nonzero.
- Child records: `type` of `message_end` or `tool_result_end`, `subagentSource:
  child`, `subagentRunId`, `subagentStepIndex`, `subagentAgent`, and `message`.
- Assistant message: exactly `role: assistant`, `content`, and `usage`.
  Content is a list of projected `{type: toolCall, id, name}` blocks. Empty lists
  are allowed. Usage is either null (unreported) or exactly `{input, output}`
  with native replay's exact nonnegative integer bounds. Cache/cost fields are
  not silently included in either count.
- Tool result message: exactly `role: toolResult`, `toolCallId`, `toolName`,
  boolean `isError`, and `content`, a list of `{type: text, text}` blocks.
  Empty content is valid and hashes as the empty list. Nontext blocks fail.

A separate human review must project synthetic source records into this subset.
Do not strip fields from arbitrary real logs and treat this as a reviewed capture
pipeline. The adapter cannot verify that inputs are synthetic or authentic.

## Mapping and loss

Run, tool, actor, step and call identities are deterministic SHA-256 identifiers
with distinct alphabetic prefixes. The inputs are canonical JSON arrays: run is
`[runId]`, tool is `[toolName]`, step is `[stepIndex]`, actor is
`[stepIndex, agent]`, and call is `[stepIndex, toolCallId]`. This distinguishes
parallel instances of the same named agent and call IDs local to different
children. Reusing an ID within a step, mismatched tool names/owners, and orphan or
duplicate results fail. Derived hash collisions within a conversion are checked.
These IDs are stable only while source step indexes and names are stable; this
is not a semantic matching algorithm across differently numbered executions.
Hashes do not anonymize low-entropy identifiers.

Assistant toolCall blocks map to tool_started observations. They record a declared
call, not proof of dispatch or a side effect. Explicit results map to
`tool_finished` with `error` or `ok`. Result digests hash the canonical JSON list
of text blocks (sorted keys, compact separators, ASCII escapes, no newline), not
concatenated text. Text-block boundaries therefore remain observable. Arguments,
assistant text/reasoning, paths, process metadata, timestamps, durations, cache
usage and cost are not part of replay observations. This projection's digest
covers the projected records, not omitted source data. Nothing here proves tool
correctness, successful writes, billing accuracy, timing, or task quality.

Run status maps `complete` to `completed`, `failed` to `failed`, `stopped` to
`cancelled`. Unsupported pause/retry/rejection/truncation records fail instead of
being called successful. Native validation still requires all completed-run
steps to be successful and forbids finishing a step with unresolved tools. Thus
a failed/stopped step record with a pending call is rejected rather than inventing
a result; a failed/stopped run may retain explicitly unfinished steps/tools.

EOF produces no terminal events. Use native replay with `allow_partial=True` to
inspect the pending work. An absent usage record stays absent, though default v1
report totals cannot distinguish unreported usage from explicit zero. There is
no source sequence field in this projection, so completeness and duplicate
usage-only messages cannot be detected; the adapter must not be treated as an
exactly-once collector. It reuses normal atomic replay validation internally but
returns no partial output after any rejected record. Input objects are unmodified
and output events do not retain their mutable lists or dictionaries.

## Reproduce acceptance checks

Run `python -m unittest discover -s tests -p test_subagents_adapter.py -v`.
The tests check literal mapped observations and result digests, identity scoping
and independent actor interleavings, all six source-record prefix boundaries
through native snapshot/resume, interrupted/failed/stopped work, omitted usage,
content drift, strict outcomes, and rejected ambiguous/malformed records.

This is one synthetic adapter capability, not a benchmark score, live integration,
production capture review, or real-agent process recovery. It is separate from
the historical tau2 episode adapter and optional disk-backed replay history.
