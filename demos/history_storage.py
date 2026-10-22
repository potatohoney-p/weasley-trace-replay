"""Compare append-only history storage and separate full-report materialization.

Every sample uses a fresh interpreter. Imports, fixture generation, empty-state
creation (including SQLite schema), GC, oracle checks, file-size inspection and
output are excluded. Append timing and allocation tracing run separately; the
report allocation pass builds the entire state before tracing. Resident fixture
objects and native SQLite allocations are excluded from tracemalloc, so these
figures are not RSS or a total memory bound. SQLite uses its durable per-event
transactions: this is deliberately not a durability-equivalent speed contest.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import gc
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc

from demos import scaling
from weasley_trace_replay import ReplayState
from weasley_trace_replay.sqlite_history import HistoryStorageError, SQLiteReplayState

ROOT = scaling.ROOT
BenchmarkError = scaling.BenchmarkError
Workload = scaling.Workload
BACKENDS = ("memory", "sqlite")
PASSES = ("append_time", "append_allocations", "report_allocations")
PRAGMAS = {"journal_mode": "delete", "synchronous": 2,
           "cache_size": -2048, "mmap_size": 0}


def source_identity() -> dict:
    paths = ("demos/history_storage.py", "demos/scaling.py",
             "weasley_trace_replay/replay.py", "weasley_trace_replay/sqlite_history.py")
    return {path: hashlib.sha256((ROOT / path).read_text(encoding="utf-8").encode("utf-8")).hexdigest()
            for path in paths}


def environment() -> dict:
    return {**scaling.environment(), "sqlite_version": sqlite3.sqlite_version}


def _sqlite_settings(state) -> dict:
    # Private evaluator instrumentation of the actual measured connection.
    return {name: state._db.execute(f"PRAGMA {name}").fetchone()[0]
            for name in PRAGMAS}


def _storage_bytes(path: Path) -> dict:
    # Sizes after close, not bytes written or peak journal usage.
    return {"database_bytes": path.stat().st_size if path.exists() else 0,
            "sidecar_bytes": sum(candidate.stat().st_size
                                 for suffix in ("-journal", "-wal", "-shm")
                                 if (candidate := Path(str(path) + suffix)).exists())}


def measure(workload: Workload, backend: str, measurement: str) -> dict:
    """Direct calls are for protocol testing; run() supplies fresh interpreters."""
    if backend not in BACKENDS or measurement not in PASSES:
        raise BenchmarkError("unsupported measurement")
    if tracemalloc.is_tracing():
        raise BenchmarkError("worker already has allocation tracing enabled")
    sources = source_identity()
    records = scaling.events(workload)
    with tempfile.TemporaryDirectory(prefix="history-storage-") as directory:
        path = Path(directory) / "history.sqlite"
        state = SQLiteReplayState(path) if backend == "sqlite" else ReplayState()
        try:
            pragmas = _sqlite_settings(state) if backend == "sqlite" else None
            if backend == "sqlite" and pragmas != PRAGMAS:
                raise BenchmarkError("unexpected SQLite settings")
            if measurement == "report_allocations":
                state.extend(iter(records))
            gc.collect()
            if measurement == "append_time":
                started = time.perf_counter_ns()
                state.extend(iter(records))
                metrics = {"elapsed_ns": time.perf_counter_ns() - started}
            else:
                tracemalloc.start(1)
                try:
                    if measurement == "report_allocations":
                        report = state.report()
                    else:
                        state.extend(iter(records))
                    retained, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
                metrics = {"peak_traced_bytes": peak, "retained_traced_bytes": retained}
            if measurement != "report_allocations":
                report = state.report()
            expected = scaling.expected_report(workload, records)
            scaling.verify_report(report, expected)
            if backend == "sqlite" and _sqlite_settings(state) != pragmas:
                raise BenchmarkError("SQLite settings changed during measurement")
        finally:
            if backend == "sqlite":
                state.close()
        storage = _storage_bytes(path)
    if sources != source_identity():
        raise BenchmarkError("source changed during measurement")
    return {"protocol_version": 1, "workload": asdict(workload), "backend": backend,
            "measurement": measurement, "validated": True,
            "report_sha256": scaling._json_digest(expected), "environment": environment(),
            "sources": sources, "sqlite_pragmas": pragmas, **storage, **metrics}


def _worker_sample(workload: Workload, backend: str, measurement: str,
                   expected_sha: str, timeout: int, identity: dict) -> dict:
    request = {"workload": asdict(workload), "backend": backend, "measurement": measurement}
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "demos.history_storage", "--worker", json.dumps(request)],
            cwd=ROOT, env=dict(os.environ, PYTHONHASHSEED="0"), capture_output=True,
            text=True, encoding="utf-8", timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise BenchmarkError("worker timed out") from exc
    except (OSError, UnicodeError) as exc:
        raise BenchmarkError("worker could not run") from exc
    if completed.returncode != 0 or completed.stderr:
        raise BenchmarkError("worker failed")
    sample = scaling._decode(completed.stdout)
    metrics = {"elapsed_ns"} if measurement == "append_time" else {
        "peak_traced_bytes", "retained_traced_bytes"}
    storage = {"database_bytes", "sidecar_bytes"}
    metadata = {"protocol_version": 1, "workload": asdict(workload), "backend": backend,
                "measurement": measurement, "validated": True, "report_sha256": expected_sha,
                "sqlite_pragmas": PRAGMAS if backend == "sqlite" else None, **identity}
    if set(sample) != set(metadata) | metrics | storage:
        raise BenchmarkError("unexpected worker fields")
    if scaling._json_digest({key: sample[key] for key in metadata}) != scaling._json_digest(metadata):
        raise BenchmarkError("worker identity or validation mismatch")
    if any(type(sample[key]) is not int or sample[key] < 0 for key in metrics | storage):
        raise BenchmarkError("invalid worker measurement")
    if measurement == "append_time" and sample["elapsed_ns"] == 0:
        raise BenchmarkError("invalid worker duration")
    if measurement != "append_time" and sample["peak_traced_bytes"] < sample["retained_traced_bytes"]:
        raise BenchmarkError("inconsistent worker allocation measurement")
    if (backend == "sqlite" and sample["database_bytes"] == 0
            or backend == "memory" and any(sample[key] != 0 for key in storage)):
        raise BenchmarkError("invalid worker storage measurement")
    return sample


def run(workloads: list[Workload], *, repeats: int = 3, timeout: int = 120) -> dict:
    """Serial workers, with no partial result on failure and no speed threshold."""
    if (not workloads or any(type(item) is not Workload for item in workloads)
            or type(repeats) is not int or repeats < 1 or type(timeout) is not int or timeout < 1
            or len(set(workloads)) != len(workloads)):
        raise BenchmarkError("invalid benchmark configuration")
    identity = {"environment": environment(), "sources": source_identity()}
    cases = []
    for workload in workloads:
        expected = scaling.expected_report(workload, scaling.events(workload))
        case = {"workload": asdict(workload), "events": workload.event_count,
                "steps": workload.rounds * workload.actors,
                "tools": workload.rounds * workload.actors * workload.calls,
                "trace_sha256": expected["trace_sha256"],
                "report_sha256": scaling._json_digest(expected), "backends": {}}
        for backend in BACKENDS:
            passes = {}
            for measurement in PASSES:
                samples = [_worker_sample(workload, backend, measurement, case["report_sha256"],
                                          timeout, identity) for _ in range(repeats)]
                metrics = ("elapsed_ns",) if measurement == "append_time" else (
                    "peak_traced_bytes", "retained_traced_bytes")
                passes[measurement] = {"samples": samples, "medians": {
                    key: statistics.median(sample[key] for sample in samples)
                    for key in (*metrics, "database_bytes", "sidecar_bytes")}}
            case["backends"][backend] = passes
        cases.append(case)
    if identity != {"environment": environment(), "sources": source_identity()}:
        raise BenchmarkError("benchmark identity changed during run")
    return {"benchmark": "history-storage", "protocol_version": 1, "passed": True,
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(), **identity,
            "repeats": repeats, "worker_timeout_seconds": timeout,
            "pythonhashseed": "0", "cases": cases}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", nargs="+", type=int, default=[4, 16, 64])
    parser.add_argument("--actors", type=int, default=4)
    parser.add_argument("--calls", type=int, default=2)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.worker is not None:
            request = scaling._decode(args.worker)
            if set(request) != {"workload", "backend", "measurement"}:
                raise BenchmarkError("invalid worker request")
            if type(request["workload"]) is not dict or set(request["workload"]) != {"rounds", "actors", "calls"}:
                raise BenchmarkError("invalid worker workload")
            result = measure(Workload(**request["workload"]), request["backend"], request["measurement"])
        else:
            result = run([Workload(rounds, args.actors, args.calls) for rounds in args.rounds],
                         repeats=args.repeats, timeout=args.timeout)
    except (BenchmarkError, ValueError, OSError, sqlite3.Error, HistoryStorageError):
        print(json.dumps({"passed": False, "error": "invalid_or_failed_benchmark"}))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
