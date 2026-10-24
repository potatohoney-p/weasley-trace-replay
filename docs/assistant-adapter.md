# Reviewed synthetic Assistant graph projection

The opt-in `adapt_synthetic_assistant(records, run_id=...)` library function maps
node lifecycles into replay v1. It does not import the Assistant package, execute
nodes, collect telemetry or ingest arbitrary production logs.

Source semantics are pinned to
[Assistant engine 7ebbc03](https://github.com/potatohoney-p/weasley-assistant/blob/7ebbc03b9a54eb4f063c16b80ebe5d72c6f0a92b/weasley_assistant/graph/engine.py).
The observer receives `notify(kind, event)`. A reviewer creates synthetic records
with `type=kind` and ONLY the fields below. This is an explicit projection, not
a newly claimed native runtime file format. No automated redaction is provided.

| Type | Retained source fields besides type |
| --- | --- |
| graph_start | workflow, nodes |
| node_start | workflow, node, visit |
| node_end | workflow, node, error |
| graph_end | workflow, steps, path, error |

`error` is null or a nonempty synthetic string. Error text is discarded; presence
maps to an error/failed status. An empty graph is valid. Declared node names must
be unique, nonempty strings other than START/END. A node has at most one active
visit; visits must be consecutive positive integers beginning at 1. Node ends
carry no visit in the source, so they pair only with the sole active visit for
that node. The terminal path must exactly match recorded node-end order and its
step count, with no unfinished node. A successful graph cannot contain a failed
node. A graph can fail without a failed node (for example, an engine visit/step
guard). Unscheduled declared nodes do not imply failure.

The source has no run ID. The caller supplies a stable nonempty synthetic run ID
for one graph invocation; the workflow name is also included in its derived ID.
Never combine invocations into one input. Actor IDs hash the node name; step IDs
hash node name and visit. Each derived ID uses canonical JSON and SHA-256 with
collision checks. These IDs are deterministic, not anonymous or authenticated.
Renaming nodes or changing visit numbers changes correspondence across runs.

EOF does not finish anything: interrupted prefixes retain running status and
unfinished steps. Output events pass through the existing ReplayState validator;
reports are independent of the returned event list. Every valid nonempty source
prefix can be snapshotted and resumed with the matching full converted prefix.

## Loss and limits

- Drop route records, topology, routing labels/targets, node output keys, timing,
  and source error text when manually constructing the projection. Unknown fields
  or event kinds submitted to the adapter are rejected, not silently removed.
- Tools and LLM messages are outside this adapter. Graph node wrappers do not
  establish sufficient tool-call identity, so no guessed tool pairs are created.
- No node values, tool-result digests, checkpoints or token usage are fabricated.
  The v1 report's zero usage means **unreported**, not measured zero usage.
- Replay comparison covers observed node/visit identities and outcome statuses.
  The existing `compare()` intentionally ignores run/workflow IDs and cross-actor
  interleaving; different workflows or completion orders may compare equivalent.
  It does not compare graph topology, routing reasons, raw errors, node values or
  side effects. Equal reports are not equivalent runtime execution or model quality.
- Custom nodes that clear earlier entries from the shared `errors` dictionary can
  produce a source success after a failed node. That recovery convention is outside
  this strict subset and is rejected rather than erasing the recorded failure.
- A missing node lifecycle can sometimes be caught by the terminal path/count,
  but a self-consistently edited projection cannot be authenticated. Losing an
  entire final visit or all terminal records cannot be distinguished from a real
  interruption. The function cannot prove supplied input is synthetic or complete.
- Routing-event order is deliberately absent. Assistant PR8's merge-all-before-
  routing change does not change this pinned lifecycle contract, but this adapter
  is not a verification of that runtime fix.

## Reproduce

```python
from weasley_trace_replay.assistant import adapt_synthetic_assistant

result = adapt_synthetic_assistant([
    {"type": "graph_start", "workflow": "synthetic", "nodes": ["worker"]},
    {"type": "node_start", "workflow": "synthetic", "node": "worker", "visit": 1},
    {"type": "node_end", "workflow": "synthetic", "node": "worker", "error": None},
    {"type": "graph_end", "workflow": "synthetic", "steps": 1,
     "path": ["worker"], "error": None},
], run_id="synthetic-example")
assert result["report"]["status"] == "completed"
```

Run `python -m unittest discover -s tests -v`. Fixtures are newly authored synthetic
lifecycles, including concurrent starts, repeated visits, error/guard outcomes,
all source-prefix resume boundaries and malformed/ambiguous records. No providers,
credentials, production data or live runtime capture are needed.
