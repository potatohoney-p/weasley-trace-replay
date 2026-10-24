"""Offline conversion of a reviewed synthetic Subagents event projection.

This is deliberately not a permissive reader for production events.jsonl files.
"""
from __future__ import annotations

import hashlib

from .replay import ReplayState, TraceError, _canonical, _require

SOURCE_COMMIT = "72901e3f17eea4d56418b24e7a63f10c7ae22375"
SOURCE_REPOSITORY = "https://github.com/potatohoney-p/weasley-subagents"


def _shape(value: object, fields: set[str]) -> None:
    _require(type(value) is dict and set(value) == fields,
             "invalid synthetic Subagents projection fields")


def _text(value: object) -> None:
    _require(type(value) is str and bool(value), "invalid source identity")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise TraceError("invalid source identity encoding") from exc


def adapt_synthetic_subagents(records: list[dict]) -> dict:
    """Convert strict content-minimized synthetic records; never execute tools.

    Input uses the pinned runtime's lifecycle/message field names, but is an
    explicit projection documented in docs/subagents-adapter.md. Unknown fields
    or unsupported events fail rather than being silently discarded. No claim
    is made that a caller's records are synthetic, complete, or authentic.
    """
    _require(type(records) is list and bool(records), "empty synthetic projection")
    state = ReplayState()
    events = []
    run = None
    identities = {}
    step_agents = {}
    calls = {}
    active = set()
    terminated = False

    def identity(kind: str, parts: list) -> str:
        encoded = _canonical(parts)
        key = kind + hashlib.sha256(encoded).hexdigest()
        _require(key not in identities or identities[key] == encoded,
                 "derived identity collision")
        identities[key] = encoded
        return key

    def emit(kind: str, **fields: object) -> None:
        event = {"v": 1, "seq": len(events), "run": identity("r", [run]),
                 "type": kind, **fields}
        state.append(event)
        events.append(event)

    for index, record in enumerate(records):
        try:
            _require(not terminated, "source event after terminal run")
            _require(type(record) is dict, "source record must be an object")
            kind = record.get("type")
            _require(type(kind) is str, "invalid source event type")
            if kind in {"subagent.run.started", "subagent.run.completed"}:
                fields = {"type", "lifecycleArtifactVersion", "runId"}
                if kind.endswith("completed"):
                    fields.add("status")
                _shape(record, fields)
                _require(type(record["lifecycleArtifactVersion"]) is int and
                         record["lifecycleArtifactVersion"] == 3,
                         "unsupported source lifecycle version")
                _text(record["runId"])
                if kind.endswith("started"):
                    _require(run is None and index == 0, "duplicate or late source run start")
                    run = record["runId"]
                    emit("run_started")
                else:
                    _require(run is not None and record["runId"] == run, "mixed source run")
                    status = record["status"]
                    _require(type(status) is str and status in {"complete", "failed", "stopped"},
                             "unsupported source terminal state")
                    terminated = True
                    emit("run_finished", status={"complete": "completed", "failed": "failed",
                                                 "stopped": "cancelled"}[status])
                continue
            _require(run is not None, "missing source run start")
            child = kind in {"message_end", "tool_result_end"}
            if child:
                _shape(record, {"type", "message", "subagentSource", "subagentRunId",
                                "subagentStepIndex", "subagentAgent"})
                _require(record["subagentSource"] == "child", "invalid child source")
                source_run, step, agent = (record["subagentRunId"], record["subagentStepIndex"],
                                           record["subagentAgent"])
            else:
                _require(kind in {"subagent.step.started", "subagent.step.completed",
                                  "subagent.step.failed", "subagent.step.stopped"},
                         "unsupported or ambiguous source event")
                fields = {"type", "runId", "stepIndex", "agent"}
                if kind != "subagent.step.started":
                    fields.add("exitCode")
                _shape(record, fields)
                source_run, step, agent = record["runId"], record["stepIndex"], record["agent"]
            _require(source_run == run, "mixed source run")
            _require(type(step) is int and 0 <= step <= 2**53 - 1, "invalid source step index")
            _text(agent)
            owner = {"actor": identity("a", [step, agent]), "step": identity("s", [step])}
            if kind == "subagent.step.started":
                _require(step not in step_agents, "reused source step index")
                step_agents[step] = agent
                active.add(step)
                emit("step_started", **owner)
                continue
            _require(step in step_agents and step_agents[step] == agent, "unknown source step owner")
            _require(step in active, "source step is not active")
            if not child:
                code = record["exitCode"]
                _require(type(code) is int, "invalid source exit code")
                _require((code == 0) == (kind == "subagent.step.completed"),
                         "inconsistent source step outcome")
                emit("step_finished", **owner,
                     status={"subagent.step.completed": "ok", "subagent.step.failed": "error",
                             "subagent.step.stopped": "cancelled"}[kind])
                active.remove(step)
                continue
            message = record["message"]
            if kind == "message_end":
                _shape(message, {"role", "content", "usage"})
                _require(message["role"] == "assistant", "unsupported source message role")
                usage = message["usage"]
                if usage is not None:
                    _shape(usage, {"input", "output"})
                    emit("usage", **owner, input_tokens=usage["input"], output_tokens=usage["output"])
                _require(type(message["content"]) is list, "invalid source content")
                for block in message["content"]:
                    _shape(block, {"type", "id", "name"})
                    _require(block["type"] == "toolCall", "unsupported projected content block")
                    _text(block["id"])
                    _text(block["name"])
                    key = (step, block["id"])
                    _require(key not in calls, "reused source call identity")
                    calls[key] = block["name"]
                    emit("tool_started", **owner, call=identity("c", list(key)),
                         tool=identity("t", [block["name"]]))
            else:
                _shape(message, {"role", "toolCallId", "toolName", "isError", "content"})
                _require(message["role"] == "toolResult", "unsupported source result role")
                _text(message["toolCallId"])
                _text(message["toolName"])
                _require(type(message["isError"]) is bool, "missing explicit source result outcome")
                key = (step, message["toolCallId"])
                _require(key in calls and calls[key] == message["toolName"], "unmatched source tool result")
                blocks = message["content"]
                _require(type(blocks) is list, "invalid source result content")
                for block in blocks:
                    _shape(block, {"type", "text"})
                    _require(block["type"] == "text" and type(block["text"]) is str,
                             "unsupported source result content")
                    try:
                        block["text"].encode("utf-8")
                    except UnicodeError as exc:
                        raise TraceError("invalid source result encoding") from exc
                # Hash structured blocks, not their ambiguous concatenation.
                emit("tool_finished", **owner, call=identity("c", list(key)),
                     status="error" if message["isError"] else "ok",
                     digest=hashlib.sha256(_canonical(blocks)).hexdigest())
        except TraceError as exc:
            raise TraceError(f"source record {index}: {exc}") from exc
    return {
        "adapter": "synthetic-subagents-projection-v1", "source_commit": SOURCE_COMMIT,
        "source_repository": SOURCE_REPOSITORY,
        "projection_sha256": hashlib.sha256(_canonical(records)).hexdigest(),
        "events": events,
        "losses": ["timestamps and durations", "paths and process metadata", "tool arguments",
                   "assistant text and reasoning", "cache tokens and cost", "runtime side effects"],
        "derived": ["contiguous replay sequence", "hashed step-scoped actor and call identities",
                    "hashed run and tool identities", "canonical text-block result digests"],
    }
