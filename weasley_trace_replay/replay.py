"""Versioned JSONL contracts and deterministic replay of agent lifecycle events."""

from __future__ import annotations

import copy
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


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


class ReplayState:
    """Incremental replay with atomic individual appends, not a runtime executor.

    Reports and snapshots are detached copies. Each append stages affected state;
    rejected events leave the state and hash untouched. An extend failure retains
    its valid earlier events, but never any part of the rejected event. This is
    intentionally not bounded-state replay and is not thread-safe.
    """

    def __init__(self) -> None:
        self._state = {
            "run": None, "status": "running", "steps": {}, "tools": {},
            "active": {}, "checkpoints": {},
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
        self._digest = hashlib.sha256()
        self._count = 0

    def append(self, event: dict) -> None:
        """Validate and append exactly one event without partial mutation."""
        try:
            _validate(event, self._count)
            encoded = _canonical(event)
            candidate = _stage(self._state, event)
            _transition(candidate, event)
            digest = self._digest.copy()
            digest.update(encoded)
            digest.update(b"\n")
        except TraceError as exc:
            raise TraceError(f"event {self._count}: {exc}") from exc
        self._state, self._digest = candidate, digest
        self._count += 1

    def extend(self, events: Iterable[dict]) -> None:
        """Append in order; each event, not the whole iterable, is atomic."""
        for event in events:
            self.append(event)

    def report(self, *, allow_partial: bool = False) -> dict:
        """Return the same detached report as replay() at this boundary."""
        state = self._state
        _require(state["run"] is not None, "trace is empty")
        _require(allow_partial or state["status"] != "running",
                 "trace is incomplete; use allow_partial for an interrupted run")
        return copy.deepcopy({
            "version": 1, "run": state["run"], "status": state["status"],
            "events": self._count, "trace_sha256": self._digest.hexdigest(),
            "steps": dict(sorted(state["steps"].items())),
            "tools": dict(sorted(state["tools"].items())),
            "checkpoints": dict(sorted(state["checkpoints"].items())),
            "usage": state["usage"],
            "unfinished_steps": sorted(state["active"].values()),
            "unfinished_tools": sorted(key for key, call in state["tools"].items()
                                       if call["status"] == "running"),
        })

    def snapshot(self) -> dict:
        """Capture a versioned verification record, including historical IDs."""
        _require(self._state["run"] is not None, "trace is empty")
        return copy.deepcopy({
            "format": "weasley-replay-snapshot", "snapshot_version": 1,
            "trace_version": 1, "prefix_events": self._count,
            "prefix_sha256": self._digest.hexdigest(), "state": self._state,
        })

    @classmethod
    def from_snapshot(cls, saved: dict, prefix: Iterable[dict]) -> ReplayState:
        """Replay the supplied prefix and verify ALL saved state before resuming.

        A digest cannot seed hashlib or authenticate saved state. Prefix replay
        reconstructs both state and the live hash context. No serialized state
        is trusted or installed. Verification is O(prefix replay), not O(1).
        """
        _validate_snapshot(saved)
        restored = cls()
        restored.extend(prefix)
        _require(_canonical(restored.snapshot()) == _canonical(saved),
                 "snapshot does not match verified prefix")
        return restored


def _stage(state: dict, event: dict) -> dict:
    """Copy only containers that this transition can mutate.

    Immutable scalar values and untouched history may be shared internally.
    This private staging view never escapes; reports and snapshots remain deep
    copies. No container mutated by _transition may alias the committed state.
    """
    candidate = state.copy()
    kind = event["type"]
    if kind in {"step_started", "step_finished"}:
        candidate["steps"] = state["steps"].copy()
        candidate["active"] = state["active"].copy()
        if kind == "step_finished" and event["step"] in state["steps"]:
            candidate["steps"][event["step"]] = state["steps"][event["step"]].copy()
    elif kind in {"tool_started", "tool_finished"}:
        candidate["tools"] = state["tools"].copy()
        if kind == "tool_finished" and event["call"] in state["tools"]:
            candidate["tools"][event["call"]] = state["tools"][event["call"]].copy()
    elif kind == "checkpoint":
        candidate["checkpoints"] = state["checkpoints"].copy()
        if event["step"] in state["checkpoints"]:
            candidate["checkpoints"][event["step"]] = state["checkpoints"][event["step"]].copy()
    elif kind == "usage":
        candidate["usage"] = state["usage"].copy()
    return candidate


def _transition(state: dict, event: dict) -> None:
    kind = event["type"]
    steps, calls, active = state["steps"], state["tools"], state["active"]
    _require(state["status"] == "running", "events after terminal run")
    if state["run"] is None:
        _require(kind == "run_started", "first event must start a run")
        state["run"] = event["run"]
    else:
        _require(kind != "run_started", "duplicate run start")
        _require(event["run"] == state["run"], "mixed run identifiers")
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
            _require(call is not None and call["actor"] == actor and call["step"] == step,
                     "tool result has no matching call")
            _require(call["status"] == "running", "tool result already recorded")
            call.update(status=event["status"], digest=event["digest"])
        elif kind == "checkpoint":
            state["checkpoints"].setdefault(step, []).append(event["digest"])
        elif kind == "usage":
            for key in state["usage"]:
                state["usage"][key] += event[key]
                _require(state["usage"][key] <= 2**53 - 1, "aggregate usage exceeds exact integer range")
        elif kind == "step_finished":
            _require(not any(c["step"] == step and c["status"] == "running" for c in calls.values()),
                     "step has unfinished tools")
            steps[step]["status"] = event["status"]
            del active[actor]
    elif kind == "run_finished":
        state["status"] = event["status"]
        if state["status"] == "completed":
            _require(not active, "completed run has unfinished steps")
            _require(all(step["status"] == "ok" for step in steps.values()),
                     "completed run has unsuccessful steps")


def replay(events: Iterable[dict], *, allow_partial: bool = False) -> dict:
    """Compatible full-replay wrapper around incremental lifecycle state."""
    state = ReplayState()
    state.extend(events)
    return state.report(allow_partial=allow_partial)


def snapshot(events: Iterable[dict]) -> dict:
    """Create a snapshot of any nonempty valid prefix, including terminal runs."""
    state = ReplayState()
    state.extend(events)
    return state.snapshot()


def resume(saved: dict, prefix: Iterable[dict], suffix: Iterable[dict], *,
           allow_partial: bool = False) -> dict:
    """Verify a snapshot against its entire prefix, then append only the suffix."""
    state = ReplayState.from_snapshot(saved, prefix)
    state.extend(suffix)
    return state.report(allow_partial=allow_partial)


def _identifier(value: Any) -> None:
    _require(type(value) is str and IDENTIFIER.fullmatch(value) is not None,
             "invalid snapshot identifier")


def _snapshot_digest(value: Any) -> None:
    _require(type(value) is str and DIGEST.fullmatch(value) is not None,
             "invalid snapshot digest")


def _object(value: Any, fields: set[str]) -> None:
    _require(type(value) is dict and set(value) == fields, "invalid snapshot fields")


def _validate_snapshot(saved: Any) -> None:
    """Validate shape/invariants only. State verification still needs the prefix."""
    _object(saved, {"format", "snapshot_version", "trace_version", "prefix_events",
                    "prefix_sha256", "state"})
    _require(type(saved["format"]) is str and saved["format"] == "weasley-replay-snapshot",
             "unsupported snapshot format")
    for key in ("snapshot_version", "trace_version"):
        _require(type(saved[key]) is int and saved[key] == 1, "unsupported snapshot version")
    _require(type(saved["prefix_events"]) is int and saved["prefix_events"] > 0,
             "invalid snapshot event count")
    _snapshot_digest(saved["prefix_sha256"])
    state = saved["state"]
    _object(state, {"run", "status", "steps", "tools", "active", "checkpoints", "usage"})
    _identifier(state["run"])
    _require(type(state["status"]) is str and state["status"] in
             {"running", "completed", "failed", "cancelled"}, "invalid snapshot run status")
    for field in ("steps", "tools", "active", "checkpoints"):
        _require(type(state[field]) is dict, "invalid snapshot map")
        for key in state[field]:
            _identifier(key)
    expected_active = {}
    for step, entry in state["steps"].items():
        _object(entry, {"actor", "status"})
        _identifier(entry["actor"])
        _require(type(entry["status"]) is str and entry["status"] in
                 {"running", "ok", "error", "cancelled"}, "invalid snapshot step status")
        if entry["status"] == "running":
            _require(entry["actor"] not in expected_active, "duplicate active snapshot actor")
            expected_active[entry["actor"]] = step
    for step in state["active"].values():
        _identifier(step)
    _require(state["active"] == expected_active, "invalid snapshot active steps")
    for entry in state["tools"].values():
        _require(type(entry) is dict, "invalid snapshot tool")
        status = entry.get("status")
        _require(type(status) is str and status in {"running", "ok", "error", "cancelled"},
                 "invalid snapshot tool status")
        _object(entry, {"actor", "step", "tool", "status"} |
                ({"digest"} if status != "running" else set()))
        for key in ("actor", "step", "tool"):
            _identifier(entry[key])
        step = state["steps"].get(entry["step"])
        _require(step is not None and step["actor"] == entry["actor"], "invalid snapshot tool owner")
        _require(status != "running" or step["status"] == "running", "invalid snapshot pending tool")
        if status != "running":
            _snapshot_digest(entry["digest"])
    for step, digests in state["checkpoints"].items():
        _require(step in state["steps"] and type(digests) is list and bool(digests),
                 "invalid snapshot checkpoints")
        for digest in digests:
            _snapshot_digest(digest)
    _object(state["usage"], {"input_tokens", "output_tokens"})
    for count in state["usage"].values():
        _require(type(count) is int and 0 <= count <= 2**53 - 1, "invalid snapshot usage")
    if state["status"] == "completed":
        _require(not expected_active and all(s["status"] == "ok" for s in state["steps"].values()),
                 "invalid completed snapshot")


def serialize_snapshot(saved: dict) -> str:
    """Strict canonical JSON plus newline. Validation is not prefix verification."""
    _validate_snapshot(saved)
    return _canonical(saved).decode("ascii") + "\n"


def read_snapshot(path: str | Path, *, max_bytes: int = 16 * 1024 * 1024) -> dict:
    """Read bounded UTF-8 snapshot JSON; reject duplicate keys and malformed state.

    Reading is NOT verification. Use from_snapshot() or resume() with the prefix.
    Increase max_bytes explicitly for larger histories; state is not compacted.
    """
    _require(type(max_bytes) is int and max_bytes > 0, "invalid snapshot size limit")
    with Path(path).open("rb") as stream:
        raw = stream.read(max_bytes + 1)
    try:
        _require(len(raw) <= max_bytes, "snapshot exceeds size limit")
        saved = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                           parse_constant=_constant, parse_float=_float)
        _validate_snapshot(saved)
        return saved
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise TraceError("invalid snapshot JSON or state") from exc


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
