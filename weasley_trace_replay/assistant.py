"""Strict offline adapter for reviewed synthetic Assistant graph lifecycles."""
from __future__ import annotations

import hashlib

from .replay import ReplayState, TraceError, _canonical, _require

SOURCE_COMMIT = "7ebbc03b9a54eb4f063c16b80ebe5d72c6f0a92b"
SOURCE_REPOSITORY = "https://github.com/potatohoney-p/weasley-assistant"


def _text(value: object) -> None:
    _require(type(value) is str and bool(value), "invalid source identity")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise TraceError("invalid source identity encoding") from exc


def _shape(record: object, fields: set[str]) -> None:
    _require(type(record) is dict and set(record) == fields,
             "invalid synthetic Assistant projection fields")


def _error(value: object) -> bool:
    _require(value is None or (type(value) is str and bool(value)),
             "invalid source error marker")
    return value is not None


def adapt_synthetic_assistant(records: list[dict], *, run_id: str) -> dict:
    """Convert a manually reviewed lifecycle-only projection, not raw logs.

    See docs/assistant-adapter.md for the source pin, omitted fields and identity
    assumptions. No tool identity, result, state digest or usage is fabricated.
    """
    _require(type(records) is list and bool(records), "empty synthetic projection")
    _text(run_id)
    state = ReplayState()
    events = []
    identities = {}
    workflow = None
    nodes = set()
    visits = {}
    active = {}
    completed = []
    failed = False
    terminal = False

    def identity(kind: str, parts: list) -> str:
        encoded = _canonical(parts)
        key = kind + hashlib.sha256(encoded).hexdigest()
        _require(key not in identities or identities[key] == encoded,
                 "derived identity collision")
        identities[key] = encoded
        return key

    def emit(kind: str, **fields: object) -> None:
        event = {"v": 1, "seq": len(events), "run": identity("r", [run_id, workflow]),
                 "type": kind, **fields}
        state.append(event)
        events.append(event)

    for index, record in enumerate(records):
        try:
            _require(not terminal, "source event after terminal graph")
            _require(type(record) is dict, "source record must be an object")
            kind = record.get("type")
            _require(type(kind) is str, "invalid source event type")
            if kind == "graph_start":
                _shape(record, {"type", "workflow", "nodes"})
                _require(index == 0 and workflow is None, "duplicate or late graph start")
                _text(record["workflow"])
                workflow = record["workflow"]
                _require(type(record["nodes"]) is list, "invalid source node list")
                for name in record["nodes"]:
                    _text(name)
                    _require(name not in {"START", "END"} and name not in nodes,
                             "duplicate or reserved source node")
                    nodes.add(name)
                emit("run_started")
                continue
            _require(workflow is not None, "missing graph start")
            _require(record.get("workflow") == workflow, "mixed source workflow")
            if kind == "node_start":
                _shape(record, {"type", "workflow", "node", "visit"})
                name, visit = record["node"], record["visit"]
                _text(name)
                _require(name in nodes and name not in active, "unknown or active source node")
                _require(type(visit) is int and visit == visits.get(name, 0) + 1,
                         "nonconsecutive source node visit")
                visits[name] = visit
                owner = {"actor": identity("a", [name]),
                         "step": identity("s", [name, visit])}
                active[name] = owner
                emit("step_started", **owner)
            elif kind == "node_end":
                _shape(record, {"type", "workflow", "node", "error"})
                name = record["node"]
                _text(name)
                _require(name in active, "source end has no active node")
                error = _error(record["error"])
                emit("step_finished", **active[name], status="error" if error else "ok")
                failed = failed or error
                del active[name]
                completed.append(name)
            elif kind == "graph_end":
                _shape(record, {"type", "workflow", "steps", "path", "error"})
                _require(not active, "terminal source graph has active nodes")
                _require(type(record["steps"]) is int and record["steps"] == len(completed),
                         "source step count mismatch")
                _require(type(record["path"]) is list and record["path"] == completed,
                         "source completion path mismatch")
                error = _error(record["error"])
                _require(not failed or error, "graph success contradicts node failure")
                emit("run_finished", status="failed" if error else "completed")
                terminal = True
            else:
                raise TraceError("unsupported or ambiguous source event")
        except TraceError as exc:
            raise TraceError(f"source record {index}: {exc}") from exc
    return {
        "adapter": "synthetic-assistant-graph-v1",
        "source": {"repository": SOURCE_REPOSITORY, "commit": SOURCE_COMMIT},
        "events": events,
        "report": state.report(allow_partial=True),
        "derived": ["caller-supplied run identity", "node name and consecutive visit step identity",
                    "terminal status from source error presence"],
        "losses": ["topology and routing reasons", "node output keys and state values",
                   "error text", "timings", "tool and LLM events", "unreported token usage"],
    }
