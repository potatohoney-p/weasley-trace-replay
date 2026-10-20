"""Exhaustively check all order-preserving interleavings of two synthetic actors."""

import copy
import itertools
import json

from weasley_trace_replay import TraceError, compare, replay


def actor(name):
    scope = {"actor": name, "step": f"{name}-lookup"}
    return [
        dict(type="step_started", **scope),
        dict(type="tool_started", call=f"{name}-call", tool="search", **scope),
        dict(type="tool_finished", call=f"{name}-call", status="ok", digest="a" * 64, **scope),
        dict(type="checkpoint", digest="b" * 64, **scope),
        dict(type="step_finished", status="ok", **scope),
    ]


def trace(body):
    records = [{"type": "run_started"}] + body + [{"type": "run_finished", "status": "completed"}]
    return [dict(v=1, seq=index, run="synthetic", **record) for index, record in enumerate(records)]


def run():
    a, b = actor("researcher"), actor("reviewer")
    baseline = trace(a + b)
    baseline_state = replay(baseline)
    equivalent = 0
    for slots in itertools.combinations(range(10), 5):
        slots = set(slots)
        a_iter, b_iter = iter(a), iter(b)
        body = [next(a_iter) if index in slots else next(b_iter) for index in range(10)]
        assert compare(baseline_state, replay(trace(body)))["equivalent"]
        equivalent += 1
    mutations = {}
    for label, position, key, value in [
        ("tool_output_drift", 3, "digest", "c" * 64),
        ("checkpoint_drift", 4, "digest", "d" * 64),
        ("tool_failure", 3, "status", "error"),
    ]:
        candidate = copy.deepcopy(baseline)
        candidate[position][key] = value
        diff = compare(baseline_state, replay(candidate))
        assert not diff["equivalent"]
        mutations[label] = [change["path"] for change in diff["changes"]]
    invalid = copy.deepcopy(baseline)
    invalid[4] = dict(invalid[3], seq=4)
    try:
        replay(invalid)
        raise AssertionError("duplicate result accepted")
    except TraceError:
        pass
    interrupted_prefixes = 0
    for stop in range(1, len(baseline)):
        prefix = baseline[:stop]
        partial = replay(prefix, allow_partial=True)
        assert partial["status"] == "running"
        assert replay(prefix + baseline[stop:]) == baseline_state
        try:
            replay(prefix)
            raise AssertionError("interrupted run accepted as complete")
        except TraceError:
            pass
        interrupted_prefixes += 1
    return {
        "experiment": "two-actor-interleavings-v1",
        "equivalent_interleavings": equivalent,
        "detected_mutations": mutations,
        "invalid_duplicate_result_rejected": True,
        "interrupted_prefixes_recovered": interrupted_prefixes,
        "baseline_sha256": baseline_state["trace_sha256"],
        "network_calls": 0,
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, sort_keys=True))
