"""Resource protocol gates and measurement boundaries, without speed thresholds."""

import copy
from contextlib import redirect_stdout
import io
import json
import subprocess
import tracemalloc
import unittest
from unittest.mock import patch

from demos import history_storage as storage
from demos import scaling


class HistoryStorageProtocolTests(unittest.TestCase):
    def setUp(self):
        # Resolve platform's potential subprocess probe before worker fault injection.
        storage.environment()
        self.workload = scaling.Workload(rounds=1, actors=1, calls=1)

    def test_measurement_boundaries_exclude_setup_and_separate_report(self):
        original = storage.ReplayState
        for measurement in storage.PASSES:
            calls = []

            class ObservedState(original):
                def __init__(self):
                    calls.append(("create", tracemalloc.is_tracing()))
                    super().__init__()

                def extend(self, records):
                    calls.append(("append", tracemalloc.is_tracing()))
                    return super().extend(records)

                def report(self, **kwargs):
                    calls.append(("report", tracemalloc.is_tracing()))
                    return super().report(**kwargs)

            with self.subTest(measurement=measurement), patch.object(storage, "ReplayState", ObservedState):
                sample = storage.measure(self.workload, "memory", measurement)
                self.assertEqual(calls, [("create", False),
                                         ("append", measurement == "append_allocations"),
                                         ("report", measurement == "report_allocations")])
                self.assertEqual("elapsed_ns" in sample, measurement == "append_time")
                self.assertEqual("peak_traced_bytes" in sample, measurement != "append_time")
                self.assertFalse(tracemalloc.is_tracing())

    def test_fresh_process_samples_validate_both_backends_and_record_identity(self):
        result = storage.run([self.workload], repeats=1)
        self.assertTrue(result["passed"])
        self.assertEqual(set(result["sources"]), {
            "demos/history_storage.py", "demos/scaling.py",
            "weasley_trace_replay/replay.py", "weasley_trace_replay/sqlite_history.py"})
        self.assertIn("sqlite_version", result["environment"])
        case = result["cases"][0]
        self.assertEqual(case["events"], 8)
        for backend, passes in case["backends"].items():
            self.assertEqual(set(passes), set(storage.PASSES))
            for measurement, measured in passes.items():
                self.assertEqual(len(measured["samples"]), 1)
                sample = measured["samples"][0]
                self.assertEqual(sample["measurement"], measurement)
                self.assertEqual(sample["report_sha256"], case["report_sha256"])
                self.assertEqual(sample["sources"], result["sources"])
                self.assertEqual(sample["sqlite_pragmas"], storage.PRAGMAS if backend == "sqlite" else None)
                self.assertEqual(sample["database_bytes"] > 0, backend == "sqlite")
        json.dumps(result, allow_nan=False)

    def test_independent_oracle_rejects_corrupted_full_report_in_every_pass(self):
        for cls, backend in ((storage.ReplayState, "memory"), (storage.SQLiteReplayState, "sqlite")):
            original = cls.report

            def corrupt(state, **kwargs):
                report = original(state, **kwargs)
                report["tools"] = {}
                return report

            for measurement in storage.PASSES:
                with self.subTest(backend=backend, measurement=measurement), patch.object(cls, "report", corrupt):
                    with self.assertRaisesRegex(storage.BenchmarkError, "independent oracle"):
                        storage.measure(self.workload, backend, measurement)
                self.assertFalse(tracemalloc.is_tracing())

    def test_append_failure_stops_allocation_tracing(self):
        with patch.object(storage.ReplayState, "extend", side_effect=RuntimeError("injected")):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                storage.measure(self.workload, "memory", "append_allocations")
        self.assertFalse(tracemalloc.is_tracing())

    def test_failed_worker_aborts_cli_without_partial_evidence(self):
        failures = [subprocess.TimeoutExpired("worker", 1), OSError("private path"),
                    subprocess.CompletedProcess([], 1, "", "private details"),
                    subprocess.CompletedProcess([], 0, "{}", "warning"),
                    subprocess.CompletedProcess([], 0, "not JSON", "")]
        for failure in failures:
            kwargs = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
            with self.subTest(failure=type(failure).__name__), patch.object(subprocess, "run", **kwargs), \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(storage.main(["--rounds", "1", "--repeats", "1"]), 1)
            self.assertEqual(json.loads(output.getvalue()),
                             {"passed": False, "error": "invalid_or_failed_benchmark"})

    def test_worker_identity_pragmas_storage_and_metrics_fail_closed(self):
        valid = storage.measure(self.workload, "sqlite", "append_allocations")
        identity = {key: valid[key] for key in ("environment", "sources")}
        mutations = (
            lambda s: s.update(protocol_version=True),
            lambda s: s.update(report_sha256="0" * 64),
            lambda s: s["sources"].update({"weasley_trace_replay/sqlite_history.py": "0" * 64}),
            lambda s: s.update(backend="memory"),
            lambda s: s["sqlite_pragmas"].update(synchronous=0),
            lambda s: s.update(database_bytes=0),
            lambda s: s.update(sidecar_bytes=-1),
            lambda s: s.update(peak_traced_bytes=True),
            lambda s: s.update(retained_traced_bytes=s["peak_traced_bytes"] + 1),
            lambda s: s.update(extra=1),
        )
        for mutate in mutations:
            bad = copy.deepcopy(valid)
            mutate(bad)
            with self.subTest(sample=bad), patch.object(subprocess, "run", return_value=
                    subprocess.CompletedProcess([], 0, json.dumps(bad), "")):
                with self.assertRaises(storage.BenchmarkError):
                    storage._worker_sample(self.workload, "sqlite", "append_allocations",
                                           valid["report_sha256"], 120, identity)

    def test_worker_cli_sanitizes_storage_failures_and_stops_tracing(self):
        request = json.dumps({"workload": {"rounds": 1, "actors": 1, "calls": 1},
                              "backend": "sqlite", "measurement": "append_allocations"})
        for error in (storage.HistoryStorageError("private database path"),
                      storage.sqlite3.OperationalError("private SQL details")):
            with self.subTest(error=type(error).__name__), patch.object(
                    storage.SQLiteReplayState, "extend", side_effect=error), \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(storage.main(["--worker", request]), 1)
            self.assertEqual(json.loads(output.getvalue()),
                             {"passed": False, "error": "invalid_or_failed_benchmark"})
            self.assertFalse(tracemalloc.is_tracing())

    def test_source_change_during_measurement_is_rejected(self):
        with patch.object(storage, "source_identity", side_effect=[{"source": "before"}, {"source": "after"}]):
            with self.assertRaisesRegex(storage.BenchmarkError, "source changed"):
                storage.measure(self.workload, "memory", "append_time")

    def test_invalid_configuration_and_worker_requests(self):
        for workloads, kwargs in (([], {}), ([self.workload, self.workload], {}),
                                  ([self.workload], {"repeats": True}), ([self.workload], {"timeout": 0})):
            with self.subTest(kwargs=kwargs), self.assertRaises(storage.BenchmarkError):
                storage.run(workloads, **kwargs)
        for args in (["--worker", "{}"], ["--worker", '{"backend":"sqlite","backend":"memory"}'],
                     ["--rounds", "0"], ["--repeats", "0"]):
            with self.subTest(args=args), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(storage.main(args), 1)
            self.assertFalse(json.loads(output.getvalue())["passed"])


if __name__ == "__main__":
    unittest.main()
