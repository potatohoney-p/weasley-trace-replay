"""Loss-aware projection of a narrow historical tau2 conversation profile.

This module reads observations only. It never imports tau2, executes a tool,
contacts a provider, interprets reward, or asserts a real human handoff.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from .replay import IDENTIFIER, TraceError

PROFILE = "tau2-single-call-transfer-v1"
ROW_FIELDS = {"id", "task_id", "trial", "seed", "termination_reason", "messages"}
ROW_METADATA = {"timestamp", "start_time", "end_time", "duration", "agent_cost", "user_cost", "reward_info"}
PARTICIPANT_FIELDS = {"role", "content", "tool_calls", "turn_idx", "usage"}
PARTICIPANT_METADATA = {"timestamp", "cost", "raw_data"}
TOOL_FIELDS = {"id", "role", "content", "requestor", "error", "turn_idx"}
ROLES = ("assistant", "user", "assistant", "tool", "assistant", "user")


class AdapterError(TraceError):
    """Unsupported or ambiguous episode; messages contain no source payload."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AdapterError(message)


def _object(value, required: set[str], optional: set[str] = frozenset()) -> None:
    _require(type(value) is dict and required <= value.keys() <= required | optional,
             "unsupported episode fields")


def _canonical(value) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise AdapterError("episode must contain finite JSON values") from exc


def _identifier(value) -> None:
    _require(type(value) is str and IDENTIFIER.fullmatch(value) is not None,
             "native identifier cannot be represented by replay v1")


def _validate_episode(row: dict) -> None:
    _object(row, ROW_FIELDS, ROW_METADATA)
    _canonical(row)
    _identifier(row["id"])
    _require(type(row["task_id"]) is str and 0 < len(row["task_id"]) <= 80, "invalid task identity")
    for key in ("trial", "seed"):
        _require(type(row[key]) is int and 0 <= row[key] <= 2**53-1, "invalid trial or seed")
    _require(row["termination_reason"] == "user_stop", "unsupported episode terminal")
    messages = row["messages"]
    _require(type(messages) is list and len(messages) == len(ROLES),
             "profile requires six messages and one paired call")
    for index, (message, role) in enumerate(zip(messages, ROLES)):
        _object(message, TOOL_FIELDS if role == "tool" else PARTICIPANT_FIELDS,
                {"timestamp"} if role == "tool" else PARTICIPANT_METADATA)
        _require(message["role"] == role, "unsupported message order or role")
        _require(type(message["turn_idx"]) is int and message["turn_idx"] == index,
                 "native turn indices must be contiguous and ordered")
        if role == "tool":
            _identifier(message["id"])
            _require(message["requestor"] == "assistant", "unsupported result owner")
            _require(type(message["error"]) is bool, "tool result requires an explicit boolean outcome")
            _require(type(message["content"]) is str, "tool result requires exact text")
            try:
                message["content"].encode("utf-8")
            except UnicodeError as exc:
                raise AdapterError("tool output is not valid UTF-8 text") from exc
        else:
            usage = message["usage"]
            if usage is not None:
                _object(usage, {"prompt_tokens", "completion_tokens"})
                _require(all(type(value) is int and 0 <= value <= 2**53-1 for value in usage.values()),
                         "unsupported source usage")
            if index == 2:
                _require(message["content"] is None, "mixed text and call turn is unsupported")
                calls = message["tool_calls"]
                _require(type(calls) is list and len(calls) == 1, "exactly one native call is supported")
                call = calls[0]
                _object(call, {"id", "name", "arguments", "requestor"})
                _identifier(call["id"])
                _identifier(call["name"])
                _require(type(call["arguments"]) is dict, "call arguments must be an object")
                _require(call["requestor"] == "assistant", "unsupported call owner")
            else:
                _require(type(message["content"]) is str and bool(message["content"]), "expected a text turn")
                _require(message["tool_calls"] is None, "user, additional or overlapping calls are unsupported")
    _require(messages[2]["tool_calls"][0]["id"] == messages[3]["id"], "tool result has no matching native call")
    for index in (0, 1, 4):
        _require(not any(marker in messages[index]["content"]
                         for marker in ("###STOP###", "###TRANSFER###", "###OUT-OF-SCOPE###")),
                 "transcript contains an earlier terminal signal")
    _require(messages[-1]["content"] == "###TRANSFER###", "terminal does not match the final user signal")


def minimize_episode(row: dict) -> dict:
    """Remove known unused metadata; preserve native message values unchanged.

    Returns a detached historical-schema subset, not an anonymization guarantee.
    Unknown fields fail validation instead of being silently discarded.
    """
    _validate_episode(row)
    result = {key: copy.deepcopy(value) for key, value in row.items() if key in ROW_FIELDS}
    result["messages"] = [
        {key: copy.deepcopy(value) for key, value in message.items()
         if key in (TOOL_FIELDS if message["role"] == "tool" else PARTICIPANT_FIELDS)}
        for message in row["messages"]
    ]
    return result


def adapt_episode(row: dict) -> dict:
    """Convert one supported complete conversation; reject ambiguity atomically.

    Only native call identity/name, explicit result outcome and exact output
    digest become tool observations. Conversation lifecycle is derived. Full
    input hashing is provenance, not an additional replay comparison signal.
    """
    _validate_episode(row)
    messages = row["messages"]
    call, result = messages[2]["tool_calls"][0], messages[3]
    scope = {"actor": "assistant", "step": "conversation"}
    events = []

    def add(kind, **fields):
        events.append(dict(v=1, seq=len(events), run=row["id"], type=kind, **fields))

    add("run_started")
    add("step_started", **scope)
    add("tool_started", **scope, call=call["id"], tool=call["name"])
    add("tool_finished", **scope, call=result["id"], status="error" if result["error"] else "ok",
        digest=hashlib.sha256(result["content"].encode("utf-8")).hexdigest())
    add("step_finished", **scope, status="ok")
    add("run_finished", status="completed")
    losses = []

    def omit(path, reason):
        losses.append({"path": path, "reason": reason})

    for key in sorted(ROW_METADATA & row.keys()):
        omit("/" + key, "unused historical metadata; entire subtree omitted")
    for key in ("task_id", "trial", "seed", "termination_reason"):
        omit("/" + key, "retained in adapter provenance; no native field in the v1 trace")
    for index, message in enumerate(messages):
        prefix = f"/messages/{index}"
        omit(prefix + "/role", "validated profile role; emitted actor is a derived assistant scope")
        omit(prefix + "/turn_idx", "validated native position; emitted seq numbers a different event stream")
        for key in sorted(message.keys() & (PARTICIPANT_METADATA if index != 3 else {"timestamp"})):
            omit(prefix + "/" + key, "unused historical metadata; entire subtree omitted")
        if index == 3:
            omit(prefix + "/content", "exact UTF-8 text hashed; raw output omitted from replay")
            omit(prefix + "/requestor", "validated assistant ownership; no separate v1 requestor field")
        else:
            omit(prefix + "/content", "dialogue text/null omitted; final user marker checked only for termination")
            omit(prefix + "/usage", "all usage unreported; user-simulator usage is not assistant consumption")
            if index == 2:
                omit(prefix + "/tool_calls/0/arguments", "arguments omitted; argument-only drift is invisible to replay")
                omit(prefix + "/tool_calls/0/requestor", "validated assistant ownership; no separate v1 requestor field")
            else:
                omit(prefix + "/tool_calls", "null call list validated; no tool event")
    observed = [i for i in (0, 2, 4) if messages[i]["usage"] is not None]
    missing = [i for i in (0, 2, 4) if messages[i]["usage"] is None]
    return {
        "adapter": PROFILE, "events": events,
        "provenance": {
            "input_sha256": hashlib.sha256(_canonical(row)).hexdigest(),
            "identity": {key: row[key] for key in ("id", "task_id", "trial", "seed")},
            "source_terminal": row["termination_reason"], "terminal_signal": messages[-1]["content"],
            "usage": {"assistant_observed_turns": observed, "assistant_unreported_turns": missing,
                      "source_assistant_complete": not missing, "emitted": False,
                      "replay_v1_aggregate": "unreported"},
        },
        "losses": sorted(losses, key=lambda entry: entry["path"]),
        "derived": {"v": 1, "seq": "emitted event order, not native turn indices",
                    "actor": "assistant", "step": "conversation",
                    "step_status": "closed episode scope only",
                    "run_status": "recorded conversation boundary only"},
    }


def read_episode(path: str | Path, *, max_bytes: int = 262144) -> dict:
    """Read bounded strict UTF-8 JSON, without fetching or executing anything."""
    _require(type(max_bytes) is int and max_bytes > 0, "invalid episode byte limit")

    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, "duplicate episode object key")
            result[key] = value
        return result

    def constant(_):
        raise AdapterError("nonfinite episode number")

    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(max_bytes + 1)
        _require(len(raw) <= max_bytes, "episode exceeds byte limit")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
        _require(type(value) is dict, "episode must be an object")
        _canonical(value)
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise AdapterError("invalid or oversized episode JSON") from exc
