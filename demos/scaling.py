"""Fresh-process replay scaling measurements over deterministic synthetic history."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import gc
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time
import tracemalloc

from weasley_trace_replay import replay, resume, serialize_snapshot, snapshot

ROOT = Path(__file__).resolve().parents[1]
OPERATIONS = ("replay", "verified_resume")
MEASUREMENTS = ("time", "allocations")


class BenchmarkError(ValueError):
    """A measurement failed its protocol or correctness gate."""


@dataclass(frozen=True)
class Workload:
    rounds: int
    actors: int = 4
    calls: int = 2

    def __post_init__(self):
        for value in (self.rounds, self.actors, self.calls):
            if type(value) is not int or value < 1:
                raise ValueError("workload dimensions must be positive integers")
        # The event contract bounds aggregate usage; fail before allocating.
        if self.rounds * (7 * self.actors + self.actors * (self.actors - 1) // 2) > 2**53 - 1:
            raise ValueError("workload exceeds exact token range")

    @property
    def events_per_round(self) -> int:
        return self.actors * (2 * self.calls + 4)

    @property
    def event_count(self) -> int:
        return 2 + self.rounds * self.events_per_round

    @property
    def resume_at(self) -> int:
        # Completed history plus an active round with every tool still pending.
        return 1 + (self.rounds // 2) * self.events_per_round + self.actors * (1 + self.calls)


def _digest(identity: str) -> str:
    return hashlib.sha256(identity.encode("ascii")).hexdigest()


def events(workload: Workload) -> list[dict]:
    """Repeated concurrent rounds; historical IDs and observations accumulate."""
    records = []

    def add(kind, **fields):
        records.append(dict(v=1, seq=len(records), run="scaling-synthetic", type=kind, **fields))

    add("run_started")
    for round_index in range(workload.rounds):
        scopes = [dict(actor=f"actor{a}", step=f"actor{a}-round{round_index}")
                  for a in range(workload.actors)]
        for scope in scopes:
            add("step_started", **scope)
        for call_index in range(workload.calls):
            for scope in scopes:
                add("tool_started", **scope, call=f"{scope['step']}-call{call_index}", tool=f"tool{call_index}")
        for actor_index, scope in enumerate(scopes):
            add("checkpoint", **scope, digest=_digest(scope["step"]))
            add("usage", **scope, input_tokens=7 + actor_index, output_tokens=3 + round_index % 5)
        for call_index in reversed(range(workload.calls)):
            for scope in reversed(scopes):
                call = f"{scope['step']}-call{call_index}"
                add("tool_finished", **scope, call=call, digest=_digest(call),
                    status="ok" if call_index % 2 == 0 else "error")
        for scope in scopes:
            # Tool errors are handled inside otherwise successful steps.
            add("step_finished", **scope, status="ok")
    add("run_finished", status="completed")
    return records


def expected_report(workload: Workload, records: list[dict]) -> dict:
    """Project final facts from workload dimensions, without replay or compare."""
    steps, tools, checkpoints = {}, {}, {}
    for actor_index in range(workload.actors):
        actor = f"actor{actor_index}"
        for round_index in range(workload.rounds):
            step = f"{actor}-round{round_index}"
            steps[step] = {"actor": actor, "status": "ok"}
            checkpoints[step] = [_digest(step)]
            for call_index in range(workload.calls):
                call = f"{step}-call{call_index}"
                tools[call] = dict(actor=actor, step=step, tool=f"tool{call_index}",
                                   status="ok" if call_index % 2 == 0 else "error", digest=_digest(call))
    digest = hashlib.sha256()
    for record in records:
        digest.update(json.dumps(record, sort_keys=True, separators=(",", ":"),
                                 ensure_ascii=True, allow_nan=False).encode("ascii") + b"\n")
    return {
        "version": 1, "run": "scaling-synthetic", "status": "completed",
        "events": workload.event_count, "trace_sha256": digest.hexdigest(),
        "steps": steps, "tools": tools, "checkpoints": checkpoints,
        "usage": {
            "input_tokens": workload.rounds * (7 * workload.actors + workload.actors * (workload.actors - 1) // 2),
            "output_tokens": workload.actors * (3 * workload.rounds + 10 * (workload.rounds // 5)
                                               + sum(range(workload.rounds % 5))),
        },
        "unfinished_steps": [], "unfinished_tools": [],
    }


def verify_report(actual: dict, expected: dict) -> None:
    """Check every report field, including types and canonical input hash."""
    try:
        encoded = json.dumps(actual, sort_keys=True, allow_nan=False)
        wanted = json.dumps(expected, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise BenchmarkError("invalid measured report") from exc
    if encoded != wanted:
        raise BenchmarkError("measured report does not match independent oracle")


def _json_digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode("ascii")).hexdigest()


def source_identity() -> dict:
    # Normalize line endings so Windows checkouts identify the same source text.
    return {name: hashlib.sha256((ROOT / path).read_text(encoding="utf-8").encode("utf-8")).hexdigest()
            for name, path in (("benchmark_sha256", "demos/scaling.py"),
                               ("replay_sha256", "weasley_trace_replay/replay.py"))}


def environment() -> dict:
    return {"python": platform.python_version(), "implementation": platform.python_implementation(),
            "python_compiler": platform.python_compiler(),
            "system": platform.system(), "release": platform.release(), "machine": platform.machine(),
            "processor": platform.processor(), "logical_cpus": os.cpu_count(),
            "timer": "perf_counter_ns", "gc_enabled": gc.isenabled()}


def measure(workload: Workload, operation: str, measurement: str) -> dict:
    """One operation only; fixture, snapshot, oracle and output costs excluded.

    The CLI runs every invocation in a fresh interpreter. Direct calls are useful
    for protocol tests, but are not isolated benchmark samples.
    """
    if operation not in OPERATIONS or measurement not in MEASUREMENTS:
        raise BenchmarkError("unsupported measurement")
    if tracemalloc.is_tracing():
        raise BenchmarkError("worker already has allocation tracing enabled")
    sources = source_identity()
    records = events(workload)
    if operation == "verified_resume":
        prefix, suffix = records[:workload.resume_at], records[workload.resume_at:]
        saved = json.loads(serialize_snapshot(snapshot(prefix)))
        perform = lambda: resume(saved, iter(prefix), iter(suffix))
    else:
        perform = lambda: replay(iter(records))
    gc.collect()
    if measurement == "time":
        started = time.perf_counter_ns()
        report = perform()
        measured = {"elapsed_ns": time.perf_counter_ns() - started}
    else:
        tracemalloc.start(1)
        try:
            report = perform()
            retained, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        measured = {"peak_traced_bytes": peak, "retained_traced_bytes": retained}
    expected = expected_report(workload, records)
    verify_report(report, expected)
    if sources != source_identity():
        raise BenchmarkError("source changed during measurement")
    return {
        "protocol_version": 1, "workload": asdict(workload),
        "operation": operation, "measurement": measurement, "validated": True,
        "report_sha256": _json_digest(expected), "environment": environment(),
        "sources": sources, **measured,
    }


def _decode(raw: str) -> dict:
    def pairs(entries):
        result = {}
        for key, value in entries:
            if key in result:
                raise BenchmarkError("duplicate worker field")
            result[key] = value
        return result

    def constant(_):
        raise BenchmarkError("nonfinite worker value")

    if len(raw) > 65536:
        raise BenchmarkError("worker output exceeds protocol limit")
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, RecursionError) as exc:
        raise BenchmarkError("invalid worker JSON") from exc
    if type(value) is not dict:
        raise BenchmarkError("worker output must be an object")
    return value


def _worker_sample(workload: Workload, operation: str, measurement: str,
                   expected_sha: str, timeout: int, identity: dict) -> dict:
    request = {"workload": asdict(workload), "operation": operation, "measurement": measurement}
    env = dict(os.environ, PYTHONHASHSEED="0")
    try:
        completed = subprocess.run([sys.executable, "-m", "demos.scaling", "--worker", json.dumps(request)],
                                   cwd=ROOT, env=env, capture_output=True, text=True,
                                   encoding="utf-8", timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise BenchmarkError("worker timed out") from exc
    except (OSError, UnicodeError) as exc:
        raise BenchmarkError("worker could not run") from exc
    if completed.returncode != 0 or completed.stderr:
        raise BenchmarkError("worker failed")
    sample = _decode(completed.stdout)
    metrics = {"elapsed_ns"} if measurement == "time" else {"peak_traced_bytes", "retained_traced_bytes"}
    metadata = {"protocol_version": 1, "workload": asdict(workload), "operation": operation,
                "measurement": measurement, "validated": True, "report_sha256": expected_sha,
                **identity}
    if set(sample) != set(metadata) | metrics:
        raise BenchmarkError("unexpected worker fields")
    if _json_digest({key: sample[key] for key in metadata}) != _json_digest(metadata):
        raise BenchmarkError("worker identity or validation mismatch")
    if any(type(sample[key]) is not int or sample[key] < 0 for key in metrics):
        raise BenchmarkError("invalid worker measurement")
    if measurement == "time" and sample["elapsed_ns"] == 0:
        raise BenchmarkError("invalid worker duration")
    if measurement == "allocations" and sample["peak_traced_bytes"] < sample["retained_traced_bytes"]:
        raise BenchmarkError("inconsistent worker allocation measurement")
    return sample


def run(workloads: list[Workload], *, repeats: int = 3, timeout: int = 120) -> dict:
    """Run serial fresh workers; any invalid sample aborts the whole report."""
    if (not workloads or any(type(item) is not Workload for item in workloads)
            or type(repeats) is not int or repeats < 1 or type(timeout) is not int or timeout < 1):
        raise BenchmarkError("invalid benchmark configuration")
    if len(set(workloads)) != len(workloads):
        raise BenchmarkError("duplicate workload")
    identity = {"environment": environment(), "sources": source_identity()}
    cases = []
    for workload in workloads:
        expected = expected_report(workload, events(workload))
        case = {"workload": asdict(workload), "events": workload.event_count,
                "steps": workload.rounds * workload.actors,
                "tools": workload.rounds * workload.actors * workload.calls,
                "resume_prefix_events": workload.resume_at, "trace_sha256": expected["trace_sha256"],
                "report_sha256": _json_digest(expected), "operations": {}}
        for operation in OPERATIONS:
            samples = {"elapsed_ns": [], "peak_traced_bytes": [], "retained_traced_bytes": []}
            for _ in range(repeats):
                for measurement in MEASUREMENTS:
                    sample = _worker_sample(workload, operation, measurement, case["report_sha256"], timeout, identity)
                    for key in samples:
                        if key in sample:
                            samples[key].append(sample[key])
            samples["median_elapsed_ns"] = statistics.median(samples["elapsed_ns"])
            samples["median_peak_traced_bytes"] = statistics.median(samples["peak_traced_bytes"])
            case["operations"][operation] = samples
        cases.append(case)
    if identity != {"environment": environment(), "sources": source_identity()}:
        raise BenchmarkError("benchmark identity changed during run")
    return {"benchmark": "replay-scaling", "protocol_version": 1, "passed": True,
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            **identity,
            "repeats": repeats, "worker_timeout_seconds": timeout, "pythonhashseed": "0", "cases": cases}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", nargs="+", type=int, default=[4, 16, 64])
    parser.add_argument("--actors", type=int, default=4)
    parser.add_argument("--calls", type=int, default=2)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=120, help="per-worker timeout in seconds")
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.worker is not None:
            request = _decode(args.worker)
            if set(request) != {"workload", "operation", "measurement"}:
                raise BenchmarkError("invalid worker request")
            if type(request["workload"]) is not dict or set(request["workload"]) != {"rounds", "actors", "calls"}:
                raise BenchmarkError("invalid worker workload")
            result = measure(Workload(**request["workload"]), request["operation"], request["measurement"])
        else:
            result = run([Workload(rounds, args.actors, args.calls) for rounds in args.rounds],
                         repeats=args.repeats, timeout=args.timeout)
    except (BenchmarkError, ValueError, OSError):
        print(json.dumps({"passed": False, "error": "invalid_or_failed_benchmark"}))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
