# Selective append staging experiment

This measures a narrow synthetic workload: 300 usage events after 100 or 500
completed synthetic steps, each with a completed tool call and a checkpoint.
History construction and report export are excluded from timing. This is not
agent throughput, live runtime performance, or a model benchmark.

Run from the checkout with Python 3.11+:

    python -m experiments.append_staging

For a baseline comparison, copy this same experiment script into an isolated
checkout of commit 312ef81 (the pre-change main), then run the same command.
The script reports every timing sample, Python/platform, trace digest, event
count, and the scope being timed. Each repetition checks report parity and
expected usage/counts. Compare the trace digests across implementations too.
No wall-clock threshold is used in CI.

Observed locally on Python 3.12.14, Linux 6.18.55 x86_64 (glibc 2.41) (three repetitions, median seconds):

- 100 historical steps: baseline 0.147293; selective staging 0.001961.
- 500 historical steps: baseline 0.703612; selective staging 0.001992.

Both implementations produced identical trace digests for each workload:

- 100: de37fb57978cc43e9bace721c2107015d594169e64fd195d4e00c5b354ad27aa
- 500: 563813c5120017a067e5d015d556432431027bc579dcbb8a95c4fd9875ed4116

These numbers are single-machine observations, not a portable speed guarantee.
The implementation copies only containers a validated transition can mutate.
Usage events copy a two-counter dictionary, avoiding all historical maps.
Step/tool changes still shallow-copy their corresponding map; repeated
checkpoints copy that step's checkpoint list. Step/run completion may still scan
history. Full reports/snapshots remain detached deep copies. Memory still grows
with history, and this does not replace the separate SQLite/compaction work.

Atomic rejection is checked separately, including second-field usage overflow,
invalid completed runs, every event kind, retained prior staging views, and
mutation of exported reports/snapshots. Existing independent causal-oracle and
all-prefix snapshot/resume tests remain required. The copy-elimination test
fails the pre-change implementation; this is an optimization regression, not a
claim that the previous implementation returned incorrect lifecycle results.
