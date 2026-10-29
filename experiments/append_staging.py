"""Synthetic usage-append timing, not a runtime or model benchmark.

Run from the repository root: python -m experiments.append_staging
The history construction and report export are deliberately outside the timed
section. Run the same script on a baseline checkout to compare implementations.
"""

import argparse
import json
import platform
import statistics
import sys
import time

from weasley_trace_replay import ReplayState


def measure(history: int, usage_events: int, repeats: int) -> dict:
    elapsed = []
    expected = None
    for _ in range(repeats):
        state = ReplayState()
        seq = 0
        def append(kind, **fields):
            nonlocal seq
            state.append(dict(v=1, seq=seq, run="synthetic", type=kind, **fields))
            seq += 1
        append("run_started")
        for index in range(history):
            identity = dict(actor="worker", step=f"step{index}")
            append("step_started", **identity)
            append("tool_started", **identity, call=f"call{index}", tool="synthetic")
            append("tool_finished", **identity, call=f"call{index}", status="ok", digest="a" * 64)
            append("checkpoint", **identity, digest="b" * 64)
            append("step_finished", **identity, status="ok")
        append("step_started", actor="worker", step="current")
        started = time.perf_counter()
        for _ in range(usage_events):
            append("usage", actor="worker", step="current", input_tokens=1, output_tokens=2)
        elapsed.append(time.perf_counter() - started)
        report = state.report(allow_partial=True)
        assert report["usage"] == {"input_tokens": usage_events, "output_tokens": 2 * usage_events}
        assert len(report["steps"]) == history + 1 and len(report["tools"]) == history
        if expected is None:
            expected = report
        assert report == expected
    return {"history_steps": history, "usage_events": usage_events,
            "seconds": elapsed, "median_seconds": statistics.median(elapsed),
            "trace_sha256": expected["trace_sha256"], "events": expected["events"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=int, nargs="+", default=[100, 500])
    parser.add_argument("--usage", type=int, default=300)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if any(n < 0 for n in args.history) or args.usage < 1 or args.repeats < 1:
        parser.error("history must be nonnegative; usage and repeats must be positive")
    print(json.dumps({"synthetic": True, "python": sys.version,
                      "platform": platform.platform(),
                      "scope": "usage append only; excludes history setup and report export",
                      "results": [measure(n, args.usage, args.repeats) for n in args.history]}, indent=2))


if __name__ == "__main__":
    main()
