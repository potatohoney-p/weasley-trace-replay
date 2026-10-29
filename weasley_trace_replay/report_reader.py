"""Strict materializing reader for incremental report transport v1.

Transport validation is not verification against source events or authenticity.
"""
import copy
from collections.abc import Iterable
from pathlib import Path

from .replay import TraceError, _identifier, _object, _require, _validate_snapshot, read_events


def materialize_report(records: Iterable[dict], *, max_records: int = 100_000) -> dict:
    """Consume the entire v1 transport and return a detached ordinary report.

    Uses O(history) memory. The explicit record cap counts header and terminal.
    No result is returned on missing completion, trailing input or source failure.
    Input iterators remain caller-owned, including on an early validation error.
    """
    _require(type(max_records) is int and max_records >= 2, "invalid report record limit")
    header = None
    counts = dict(steps=0, tools=0, checkpoints=0)
    maps = dict(steps={}, tools={}, checkpoints={})
    phase = 0
    previous = {1: None, 2: None, 3: None}
    complete = False
    for number, raw in enumerate(records, 1):
        _require(number <= max_records, "report exceeds record limit")
        _require(not complete, "trailing report record")
        _require(type(raw) is dict, "report record must be an object")
        row = copy.deepcopy(raw)
        if header is None:
            _object(row, {"format", "format_version", "report_version", "run", "status",
                          "events", "trace_sha256", "usage"})
            _require(type(row["format"]) is str and row["format"] == "weasley-replay-report-jsonl",
                     "unsupported report transport")
            for key in ("format_version", "report_version"):
                _require(type(row[key]) is int and row[key] == 1, "unsupported report transport version")
            header = row
            continue
        if "complete" in row:
            _object(row, {"complete", "counts"})
            _require(row["complete"] is True, "invalid report completion")
            _object(row["counts"], set(counts))
            _require(all(type(n) is int and n >= 0 for n in row["counts"].values())
                     and row["counts"] == counts, "report counts disagree")
            complete = True
            continue
        kind = row.get("kind")
        _require(type(kind) is str and kind in {"step", "tool", "checkpoint"}, "unknown report record")
        current = {"step": 1, "tool": 2, "checkpoint": 3}[kind]
        _require(current >= phase, "report sections out of order")
        phase = current
        if kind == "checkpoint":
            _object(row, {"kind", "step", "digest"})
            key = row["step"]
        else:
            _object(row, {"kind", "id", "value"})
            key = row["id"]
        _identifier(key)
        old = previous[current]
        _require(old is None or key > old or (current == 3 and key == old),
                 "report identifiers repeated or out of order")
        previous[current] = key
        if current == 3:
            maps["checkpoints"].setdefault(key, []).append(row["digest"])
        else:
            maps[kind + "s"][key] = row["value"]
        counts[kind + "s"] += 1
    _require(header is not None and complete, "incomplete report transport")
    # Reuse the existing strict observation/state shape and ownership rules.
    # This synthetic envelope is validated only, never installed or resumed.
    active = {}
    for key, value in maps["steps"].items():
        _object(value, {"actor", "status"})
        _identifier(value["actor"])
        if value["status"] == "running":
            _require(value["actor"] not in active, "duplicate active report actor")
            active[value["actor"]] = key
    state = dict(run=header["run"], status=header["status"], usage=header["usage"], active=active, **maps)
    _validate_snapshot(dict(format="weasley-replay-snapshot", snapshot_version=1,
                            trace_version=1, prefix_events=header["events"],
                            prefix_sha256=header["trace_sha256"], state=state))
    return dict(version=1, run=header["run"], status=header["status"], events=header["events"],
                trace_sha256=header["trace_sha256"], usage=state["usage"], **maps,
                unfinished_steps=sorted(active.values()),
                unfinished_tools=sorted(key for key, value in maps["tools"].items()
                                        if value["status"] == "running"))


def read_report(path: str | Path, *, max_line_bytes: int = 65536,
                max_records: int = 100_000) -> dict:
    """Read bounded UTF-8 JSONL, rejecting duplicate keys and nonfinite numbers.

    A CLI producer must additionally have exited successfully; a saved terminal
    alone cannot prove producer success or the validity of an original trace.
    """
    records = read_events(path, max_line_bytes=max_line_bytes)
    try:
        return materialize_report(records, max_records=max_records)
    finally:
        records.close()
