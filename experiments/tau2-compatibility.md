# One historical tau2 episode: offline compatibility

This capability projects one externally recorded benchmark episode into replay
v1 without importing tau2 or executing any recorded tool. It addresses the
roadmap's adapter boundary: preserve representable observations, reject ambiguity,
and make conversion losses reviewable. It is independent of the synthetic scaling
and SQLite history work. The existing v1 schema and comparison semantics do not
change.

## Source and scope

The [pinned artifact](https://github.com/sierra-research/tau2-bench/blob/05736c27219b769d0be507574dd3135010916644/data/tau2/results/final/o4-mini-2025-04-16_airline_default_gpt-4.1-2025-04-14_4trials.json)
at revision `05736c27219b769d0be507574dd3135010916644` has 7,599,444 bytes and
SHA-256 `6a3a354c9bce42b52905af096e9cea6f555548acbb17b567fd403c356df7b4ec`.
Only simulation `bfaceb85-d727-4de5-b37c-e31fdf90fb39`, task `46`, trial `0`,
seed `626729` is included. It contains six messages and one paired native tool
call/result, with explicit `error: false` and recorded terminal `user_stop`.

The [publication README](https://github.com/sierra-research/tau2-bench/blob/05736c27219b769d0be507574dd3135010916644/data/tau2/results/final/README.md)
identifies these as original June 2025 paper results; the airline tasks predate
later fixes. This is neither a current leaderboard measurement nor a statement
about provider quality. The artifact declares runner revision
`c30d59aaa71c65f9b9eb6a8f8636b48945028fcf`; that execution source was not
independently available. Artifact reproduction does **not** establish execution-
source reproducibility, model reproducibility or the authenticity of observations.

The fixture retains the native dialogue, arguments and result needed to inspect
the projection. Review found no obvious customer identifiers or credentials in
this selected row; minimization is not an automatic anonymization guarantee.
Separate task objects, other simulations, archive metadata, duplicated `raw_data`,
cost/timing fields and reward explanations are excluded. The full archive is not
vendored. The fixture is 1,898 bytes in Git (LF line endings); checkout line endings
can change its byte size. Copyright (c) 2025 Sierra Research; the
[complete upstream MIT notice](../examples/tau2/LICENSE) accompanies it.

## Supported profile and conversion

`tau2-single-call-transfer-v1` accepts exactly the ordered roles
assistant, user, assistant-with-call, tool, assistant, user, with native turn
indices 0 through 5. The sole call and result must have matching unmodified IDs
and assistant ownership. Identifiers must already satisfy v1's identifier grammar;
no normalization or collision-prone replacement is attempted. Unknown fields,
extra/overlapping calls, mixed text/call turns, missing/duplicate/orphan results,
nonboolean/absent outcome and unsupported shapes fail before any events return.

Only `user_stop` with exact final user text `###TRANSFER###` is accepted. The
profile ensures the sole call has resolved and there are no pending calls. Earlier
text turns containing known stop, transfer or out-of-scope markers are rejected;
a later transfer marker cannot repair an already terminated transcript. The
upstream [user stop logic](https://github.com/sierra-research/tau2-bench/blob/05736c27219b769d0be507574dd3135010916644/src/tau2/user/user_simulator.py)
provides contextual interpretation; the adapter deliberately supports only this
exact marker. The pinned repository's schema/logic is not evidence that the
unavailable original runner used identical code.

| Native observation | v1 projection or explicit loss |
| --- | --- |
| Episode `id` | Unchanged `run` on all six events |
| Call `id`, `name` and result `id` | Unchanged `call` and `tool`; pairing validated |
| Explicit result `error` | `false` maps to tool `ok`; `true` maps to tool `error` |
| Result `content` | SHA-256 of exact UTF-8 bytes; no trimming, normalization, newline insertion or JSON serialization |
| `task_id`, `trial`, `seed`, `termination_reason` | Adapter provenance only; no equivalent v1 fields |
| All message roles and `turn_idx` | Validated profile/order; not native v1 identity or sequence |
| Dialogue text/nulls, call arguments, null call lists | Omitted from replay; terminal marker validated separately |
| Call/result `requestor` | Assistant ownership checked; no separate requestor observation |
| Participant `usage` | All omitted; user simulator usage is not assistant consumption |
| Row timing, costs, reward; message timestamps, costs, `raw_data` | Entire known subtrees omitted; unknown fields rejected |

The adapter returns a sorted JSON-pointer loss ledger for every omitted source
field/subtree and a provenance hash of the complete supplied row. The fixture
[manifest](../examples/tau2/provenance.json) separately lists all 23 subtrees
removed from the original selected row, plus archive-level exclusions. Kept
container fields (`messages`, the one call list) supply the validated structure.

All lifecycle events are **derived**: `run_started` then `step_started` bracket
the observed conversation; `tool_started`/`tool_finished` represent the one native
pair; `step_finished` and `run_finished` close the supported recorded terminal.
Version `1`, actor `assistant`, step `conversation` and contiguous seq `0..5` are
adapter choices, not native runtime events. Step `ok` means this episode scope
closed, including when a native tool explicitly reported an error. Run `completed`
means the recorded supported conversation boundary. Neither establishes task
resolution, successful real-human transfer, or successful real-agent execution.
No checkpoints, concurrent actors or recovery events are invented.

Assistant turn 0 has `usage: null`; turns 2 and 4 report usage. The adapter omits
all usage events instead of adding partial totals or guessing missing consumption.
Its provenance reports observed and unreported assistant turns. Replay v1's
resulting `0/0` aggregate is **unreported, not measured zero**. Even a structurally
supported input with all usage present does not emit usage in this first profile.

## Run and verify

Python 3.11+, standard library only; default acceptance is fully offline:

```sh
python -m demos.tau2_compatibility
python -m unittest discover -s tests -v
```

The evaluator uses a literal independent six-event oracle and full expected
report, including independently canonicalized trace/report hashes. It checks all
six nonempty mapped-event prefixes, snapshot serialization, verified prefix
reconstruction and suffix replay against the full report. The empty-prefix case
is outside the existing snapshot API; these are replay boundaries, not six native
message checkpoints or actual agent recovery points.

Changing only the native tool output produces a single meaningful rejected
comparison at `/tools/call_jSwZRs774js2PBliHRee5crf/digest`. Tests also deliberately
change call arguments or final assistant text: replay comparison remains
equivalent because those observations are omitted. The input provenance hash
changes, but `compare()` does not inspect it. Fixture hash verification and
replay drift detection are different acceptance gates.

```python
from weasley_trace_replay import replay
from weasley_trace_replay.tau2 import adapt_episode, read_episode

converted = adapt_episode(read_episode("examples/tau2/task46-trial0.json"))
report = replay(converted["events"])
# Keep converted["provenance"], ["losses"] and ["derived"] alongside the report.
```

`read_episode` bounds input at 256 KiB by default and rejects duplicate keys,
nonfinite numbers, malformed JSON and invalid UTF-8. The evaluator bounds both
fixture fingerprint reads as well. The in-memory adapter accepts a parsed object;
callers using it directly are responsible for their input allocation limits.

With the optional SQLite history backend from
[PR7](https://github.com/potatohoney-p/weasley-trace-replay/pull/7) available:

```sh
python -m demos.tau2_compatibility --sqlite
```

This requires exact in-memory/SQLite report and snapshot parity at every prefix,
then verifies prefix reconstruction into a fresh database and suffix completion.
Without that backend, requesting `--sqlite` fails explicitly; it is never reported
as a successful skip. SQLite validation does not require this adapter to depend
on PR7, and does not claim database reopen recovery or a resource improvement.

For optional artifact provenance verification, explicitly fetch only the pinned
public URL (60-second request timeout, at most 8 MiB plus one overflow byte), or
provide an existing local archive:

```sh
python -m demos.tau2_source --fetch-pinned
python -m demos.tau2_source --archive /path/to/original-artifact.json
```

The source verifier first checks exact archive length and SHA-256, then unique
episode identity, both row hash protocols, exact minimized fixture, every omitted
subtree, full-row/minimized event parity, and declared runner revision. It retains
no archive file and prints only verification evidence. Network/source failures
produce a nonzero exit and content-free error. Normal tests never fetch upstream.

Canonical JSON here uses Python `json.dumps(sort_keys=True, separators=(",", ":"),
ensure_ascii=True, allow_nan=False)`, UTF-8 bytes and no trailing newline. Preserving
parsed integer/float types gives original row SHA-256
`f8878ea1a9b47309d37b1ea76fa1bbeb3d012e7c2918b6e5e90fd4f821c99e96`.
Recursively converting integral finite floats (such as `0.0`) to integers before
the same serialization gives the independently supplied research hash
`fb13dfb6654884270c31adcaab51fd30be38a3240e637907c131adfe9684a76f`.
These protocols are intentionally distinguished, not treated as interchangeable.
The minimized fixture canonical hash is
`9fc77bb6ca72f58a42589ffa8bbac89d26fec1b0d0bef53ea2a8ddb523fec636`.

Acceptance output includes fixture/source file hashes, source identity, trace and
report hashes, the output-drift difference, loss ledger and scope limitations.
The [recorded local validation](tau2-validation.json) contains the base comparison,
full-suite results, verified source hashes and isolated PR7 compatibility results.
Source file hashes use UTF-8 text with newline normalization; the fixture canonical
hash is portable across checkout line endings. CI validation is tracked on the PR.
There are no performance numbers or provider-quality scores. This validates one
historical adapter capability, not general tau2 coverage, a benchmark rerun,
current task correctness, real-human handoff or recovery of an agent process.
