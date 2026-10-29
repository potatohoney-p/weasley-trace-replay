"""Explicit absolute token tolerances; never an outcome-drift allowlist."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .replay import TraceError, _canonical, _constant, _float, _pairs, _require, _open_input, compare

TOKEN_FIELDS = {"input_tokens", "output_tokens"}
MAX_TOKENS = 2**53 - 1


def validate_policy(policy: dict) -> None:
    """Validate a strict v1 policy without adding implicit tolerance defaults."""
    _require(type(policy) is dict and set(policy) == {"policy_version", "usage_tolerances"},
             "invalid comparison policy fields")
    _require(type(policy["policy_version"]) is int and policy["policy_version"] == 1,
             "unsupported comparison policy version")
    tolerances = policy["usage_tolerances"]
    _require(type(tolerances) is dict and set(tolerances) <= TOKEN_FIELDS,
             "invalid token tolerance allowlist")
    for limit in tolerances.values():
        _require(type(limit) is int and 0 <= limit <= MAX_TOKENS,
                 "invalid absolute token tolerance")


def read_policy(path: str | Path, *, max_bytes: int = 65536) -> dict:
    """Read bounded strict JSON. Duplicate keys and nonfinite values fail closed."""
    _require(type(max_bytes) is int and max_bytes > 0, "invalid policy size limit")
    with _open_input(path) as stream:
        raw = stream.read(max_bytes + 1)
    try:
        _require(len(raw) <= max_bytes, "comparison policy exceeds size limit")
        policy = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                            parse_constant=_constant, parse_float=_float)
        validate_policy(policy)
        return policy
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise TraceError("invalid comparison policy JSON") from exc


def compare_with_policy(left: dict, right: dict, policy: dict) -> dict:
    """Compare replay-produced reports under an explicit absolute token policy.

    Non-usage observations remain exact. Missing/invalid usage is not zero.
    This validates policy and usage, not provenance or the full report contract;
    pass reports returned by replay()/resume()/ReplayState.report().
    """
    validate_policy(policy)
    # Fail before comparison rather than silently accepting Python bool/int
    # equality or treating incomplete, hand-constructed usage as a valid trace.
    for report in (left, right):
        _require(type(report) is dict and type(report.get("usage")) is dict and
                 set(report["usage"]) == TOKEN_FIELDS, "invalid replay report usage")
        for count in report["usage"].values():
            _require(type(count) is int and 0 <= count <= MAX_TOKENS,
                     "invalid replay report token count")
    exact = compare(left, right)
    changes, tolerated = [], []
    for change in exact["changes"]:
        field = change["path"].removeprefix("/usage/")
        tolerances = policy["usage_tolerances"]
        if change["path"] == f"/usage/{field}" and field in tolerances:
            delta = abs(change["after"] - change["before"])
            if delta <= tolerances[field]:
                tolerated.append({**change, "absolute_delta": delta,
                                  "absolute_tolerance": tolerances[field]})
                continue
        changes.append(change)
    return {
        "equivalent": not changes,
        "exact_equivalent": exact["equivalent"],
        "policy_sha256": hashlib.sha256(_canonical(policy)).hexdigest(),
        "changes": changes,
        "tolerated_changes": tolerated,
    }
