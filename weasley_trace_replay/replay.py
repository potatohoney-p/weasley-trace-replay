"""Versioned JSONL contracts and deterministic replay of agent lifecycle events."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,79}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
BASE = {"v", "seq", "run", "type"}
FIELDS = {
    "run_started": set(),
    "step_started": {"actor", "step"},
    "tool_started": {"actor", "step", "call", "tool"},
    "tool_finished": {"actor", "step", "call", "status", "digest"},
    "checkpoint": {"actor", "step", "digest"},
    "usage": {"actor", "step", "input_tokens", "output_tokens"},
    "step_finished": {"actor", "step", "status"},
    "run_finished": {"status"},
}


class TraceError(ValueError):
    """Invalid contract or impossible event ordering; no payload is echoed."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise TraceError(message)


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        _require(key not in result, "duplicate JSON object key")
        result[key] = value
    return result


def _constant(_: str) -> None:
    raise TraceError("non-finite JSON number")


def _float(value: str) -> float:
    number = float(value)
    _require(math.isfinite(number), "non-finite JSON number")
    return number


def read_events(path: str | Path, *, max_line_bytes: int = 65536) -> Iterator[dict]:
    """Read bounded UTF-8 JSON lines. Blank lines and a torn final line fail closed."""
    _require(type(max_line_bytes) is int and max_line_bytes > 0, "invalid line limit")
    with Path(path).open("rb") as stream:
        number = 0
        while raw := stream.readline(max_line_bytes + 1):
            number += 1
            try:
                _require(len(raw) <= max_line_bytes, "line exceeds size limit")
                event = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                                   parse_constant=_constant, parse_float=_float)
                _require(isinstance(event, dict), "event must be an object")
                yield event
            except (ValueError, UnicodeError, RecursionError) as exc:
                # The source may contain private data: report position, never content.
                raise TraceError(f"line {number}: invalid JSON event") from exc


def _validate(event: Any, index: int) -> None:
    _require(type(event) is dict, "event must be an object")
    kind = event.get("type")
    _require(type(kind) is str and kind in FIELDS, "unknown event type")
    _require(set(event) == BASE | FIELDS[kind], "event fields do not match v1 contract")
    _require(type(event["v"]) is int and event["v"] == 1, "unsupported trace version")
    _require(type(event["seq"]) is int and event["seq"] == index, "sequence must be contiguous from zero")
    for key in ("run", "actor", "step", "call", "tool"):
        if key in event:
            _require(type(event[key]) is str and IDENTIFIER.fullmatch(event[key]) is not None, f"invalid {key} identifier")
    if "digest" in event:
        _require(type(event["digest"]) is str and DIGEST.fullmatch(event["digest"]) is not None, "invalid SHA-256 digest")
    for key in ("input_tokens", "output_tokens"):
        if key in event:
            _require(type(event[key]) is int and 0 <= event[key] <= 2**53 - 1, f"invalid {key}")
    if "status" in event:
        allowed = {"completed", "failed", "cancelled"} if kind == "run_finished" else {"ok", "error", "cancelled"}
        _require(type(event["status"]) is str and event["status"] in allowed, "invalid terminal status")


def replay(events: Iterable[dict], *, allow_partial: bool = False) -> dict:
    """Reconstruct state, preserving unfinished work instead of inventing success."""
    run = None
    status = "running"
    steps: dict[str, dict] = {}
    calls: dict[str, dict] = {}
    active: dict[str, str] = {}
    checkpoints: dict[str, list[str]] = {}
    usage = {"input_tokens": 0, "output_tokens": 0}
    digest = hashlib.sha256()
    count = 0
    for index, event in enumerate(events):
        try:
            _validate(event, index)
            kind = event["type"]
            _require(status == "running", "events after terminal run")
            if run is None:
                _require(kind == "run_started", "first event must start a run")
                run = event["run"]
            else:
                _require(kind != "run_started", "duplicate run start")
                _require(event["run"] == run, "mixed run identifiers")
            if kind == "step_started":
                actor, step = event["actor"], event["step"]
                _require(actor not in active, "actor already has an active step")
                _require(step not in steps, "step identifier reused")
                steps[step] = {"actor": actor, "status": "running"}
                active[actor] = step
            elif kind not in {"run_started", "run_finished"}:
                actor, step = event["actor"], event["step"]
                _require(active.get(actor) == step, "event has no matching active step")
                if kind == "tool_started":
                    call = event["call"]
                    _require(call not in calls, "call identifier reused")
                    calls[call] = {"actor": actor, "step": step, "tool": event["tool"], "status": "running"}
                elif kind == "tool_finished":
                    call = calls.get(event["call"])
                    _require(call is not None and call["actor"] == actor and call["step"] == step, "tool result has no matching call")
                    _require(call["status"] == "running", "tool result already recorded")
                    call.update(status=event["status"], digest=event["digest"])
                elif kind == "checkpoint":
                    checkpoints.setdefault(step, []).append(event["digest"])
                elif kind == "usage":
                    for key in usage:
                        usage[key] += event[key]
                        _require(usage[key] <= 2**53 - 1, "aggregate usage exceeds exact integer range")
                elif kind == "step_finished":
                    _require(not any(c["step"] == step and c["status"] == "running" for c in calls.values()), "step has unfinished tools")
                    steps[step]["status"] = event["status"]
                    del active[actor]
            elif kind == "run_finished":
                status = event["status"]
                if status == "completed":
                    _require(not active, "completed run has unfinished steps")
                    _require(all(step["status"] == "ok" for step in steps.values()), "completed run has unsuccessful steps")
            digest.update(json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode())
            digest.update(b"\n")
            count += 1
        except TraceError as exc:
            raise TraceError(f"event {index}: {exc}") from exc
    _require(run is not None, "trace is empty")
    _require(allow_partial or status != "running", "trace is incomplete; use allow_partial for an interrupted run")
    return {
        "version": 1, "run": run, "status": status, "events": count,
        "trace_sha256": digest.hexdigest(),
        "steps": dict(sorted(steps.items())), "tools": dict(sorted(calls.items())),
        "checkpoints": dict(sorted(checkpoints.items())), "usage": usage,
        "unfinished_steps": sorted(active.values()),
        "unfinished_tools": sorted(key for key, call in calls.items() if call["status"] == "running"),
    }


def compare(left: dict, right: dict) -> dict:
    """Compare replay observations, ignoring run identity and interleaving order."""
    keys = ("status", "steps", "tools", "checkpoints", "usage", "unfinished_steps", "unfinished_tools")
    changes = []

    def visit(path: str, before: Any, after: Any) -> None:
        if isinstance(before, dict) and isinstance(after, dict):
            for key in sorted(before.keys() | after.keys()):
                if key not in before or key not in after:
                    changes.append({"path": f"{path}/{key}", "before": before.get(key), "after": after.get(key)})
                else:
                    visit(f"{path}/{key}", before[key], after[key])
        elif before != after:
            changes.append({"path": path, "before": before, "after": after})

    for key in keys:
        visit(f"/{key}", left[key], right[key])
    return {"equivalent": not changes, "changes": changes}
