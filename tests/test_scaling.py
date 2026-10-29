"""Scaling protocol correctness, not machine-dependent performance thresholds."""

import copy
from contextlib import redirect_stdout
import io
import json
import subprocess
import tracemalloc
import unittest
from unittest.mock import patch

from demos import scaling
from weasley_trace_replay import replay, resume, snapshot


class ScalingWorkloadTests(unittest.TestCase):
    def setUp(self):
        # platform may resolve/caches OS details through subprocess on Windows;
        # do that before fault injection at the worker process boundary.
        scaling.environment()

    def test_history_and_concurrent_boundary_have_independent_full_report_parity(self):
        workload = scaling.Workload(rounds=4)
        records = scaling.events(workload)
        self.assertEqual(len(records), 130)
        self.assertEqual(records, scaling.events(workload))
        expected = scaling.expected_report(workload, records)
        self.assertEqual(expected["usage"], {"input_tokens": 136, "output_tokens": 72})
        self.assertEqual(len(expected["steps"]), 16)
        self.assertEqual(len(expected["tools"]), 32)
        self.assertEqual(replay(records), expected)
        prefix, suffix = records[:workload.resume_at], records[workload.resume_at:]
        saved = snapshot(prefix)
        self.assertEqual(len(saved["state"]["active"]), 4)
        self.assertEqual(sum(t["status"] == "running" for t in saved["state"]["tools"].values()), 8)
        self.assertEqual(sum(s["status"] == "ok" for s in saved["state"]["steps"].values()), 8)
        self.assertEqual(resume(saved, prefix, suffix), expected)

    def test_full_report_oracle_catches_shared_replay_and_resume_regressions(self):
        workload = scaling.Workload(rounds=1, actors=1, calls=1)
        records = scaling.events(workload)
        expected = scaling.expected_report(workload, records)
        for field, value in (("trace_sha256", "0" * 64), ("events", 1), ("tools", {}),
                             ("unfinished_tools", ["lost"]), ("usage", {"input_tokens": 0, "output_tokens": 0}),
                             ("version", True)):
            report = copy.deepcopy(expected)
            report[field] = value
            with self.subTest(field=field), self.assertRaises(scaling.BenchmarkError):
                scaling.verify_report(report, expected)

    def test_invalid_workload_dimensions_fail_before_generation(self):
        for field in ("rounds", "actors", "calls"):
            for value in (0, -1, True, 1.0, "1"):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    scaling.Workload(**{"rounds": 1, field: value})

    def test_worker_measures_replay_and_verified_resume_with_report_validation(self):
        workload = scaling.Workload(rounds=1, actors=2, calls=1)
        for operation in ("replay", "verified_resume"):
            for measurement in ("time", "allocations"):
                with self.subTest(operation=operation, measurement=measurement):
                    sample = scaling.measure(workload, operation, measurement)
                    self.assertTrue(sample["validated"])
                    self.assertEqual(sample["operation"], operation)
                    self.assertEqual(sample["measurement"], measurement)
                    if measurement == "time":
                        self.assertGreater(sample["elapsed_ns"], 0)
                        self.assertNotIn("peak_traced_bytes", sample)
                    else:
                        self.assertGreater(sample["peak_traced_bytes"], 0)
                        self.assertGreaterEqual(sample["peak_traced_bytes"], sample["retained_traced_bytes"])
                        self.assertNotIn("elapsed_ns", sample)

    def test_fresh_process_protocol_returns_raw_separate_measurements(self):
        result = scaling.run([scaling.Workload(rounds=1, actors=2, calls=1)], repeats=1)
        self.assertTrue(result["passed"])
        self.assertEqual(result["protocol_version"], 1)
        self.assertEqual(len(result["cases"]), 1)
        case = result["cases"][0]
        self.assertEqual(case["events"], 14)
        for operation in ("replay", "verified_resume"):
            samples = case["operations"][operation]
            self.assertEqual(len(samples["elapsed_ns"]), 1)
            self.assertEqual(len(samples["peak_traced_bytes"]), 1)
            self.assertGreater(samples["elapsed_ns"][0], 0)
        json.dumps(result, allow_nan=False)

    def test_failed_and_malformed_workers_abort_without_partial_success(self):
        workload = scaling.Workload(rounds=1, actors=1, calls=1)
        failures = [subprocess.TimeoutExpired("worker", 1), OSError("private path"),
                    subprocess.CompletedProcess([], 1, "", "private details"),
                    subprocess.CompletedProcess([], 0, '{"validated":true}', ""),
                    subprocess.CompletedProcess([], 0, "not JSON", "")]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                kwargs = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
                with patch.object(subprocess, "run", **kwargs), self.assertRaises(scaling.BenchmarkError):
                    scaling.run([workload], repeats=1)

    def test_measurement_rejects_corrupted_outputs_and_restores_tracing_after_failure(self):
        workload = scaling.Workload(rounds=1, actors=1, calls=1)
        for function, operation in ((replay, "replay"), (resume, "verified_resume")):
            def corrupt(*args, **kwargs):
                report = function(*args, **kwargs)
                report["tools"] = {}
                return report
            for measurement in ("time", "allocations"):
                with self.subTest(operation=operation, measurement=measurement), patch.object(
                        scaling, function.__name__, side_effect=corrupt):
                    with self.assertRaises(scaling.BenchmarkError):
                        scaling.measure(workload, operation, measurement)
                self.assertFalse(tracemalloc.is_tracing())
        with patch.object(scaling, "replay", side_effect=RuntimeError("injected failure")):
            with self.assertRaises(RuntimeError):
                scaling.measure(workload, "replay", "allocations")
        self.assertFalse(tracemalloc.is_tracing())

    def test_worker_identity_metrics_and_json_are_strict(self):
        workload = scaling.Workload(rounds=1, actors=1, calls=1)
        valid = scaling.measure(workload, "replay", "time")
        mutations = (
            lambda s: s.update(protocol_version=True),
            lambda s: s.update(validated=False),
            lambda s: s.update(report_sha256="0" * 64),
            lambda s: s["sources"].update(replay_sha256="0" * 64),
            lambda s: s["workload"].update(rounds=2),
            lambda s: s.update(elapsed_ns=True),
            lambda s: s.update(elapsed_ns=-1),
            lambda s: s.update(elapsed_ns=0),
            lambda s: s.update(extra=1),
        )
        raw_cases = ['{"validated":true,"validated":true}', '{"value":NaN}', '[]']
        for mutate in mutations:
            sample = copy.deepcopy(valid)
            mutate(sample)
            raw_cases.append(json.dumps(sample))
        for raw in raw_cases:
            with self.subTest(raw=raw[:80]), patch.object(subprocess, "run", return_value=
                    subprocess.CompletedProcess([], 0, raw, "")):
                with self.assertRaises(scaling.BenchmarkError):
                    scaling.run([workload], repeats=1)

    def test_invalid_configuration_and_cli_failure_never_emit_success(self):
        workload = scaling.Workload(rounds=1)
        for workloads, kwargs in (([], {}), ([workload, workload], {}), ([workload], {"repeats": True}),
                                  ([workload], {"timeout": 0})):
            with self.subTest(kwargs=kwargs), self.assertRaises(scaling.BenchmarkError):
                scaling.run(workloads, **kwargs)
        for args in (["--rounds", "0"], ["--repeats", "0"], ["--worker", "{}"]):
            with self.subTest(args=args), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(scaling.main(args), 1)
            self.assertEqual(json.loads(output.getvalue()),
                             {"passed": False, "error": "invalid_or_failed_benchmark"})
        with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("worker", 1)), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(scaling.main(["--rounds", "1", "--repeats", "1"]), 1)
        self.assertNotIn("cases", json.loads(output.getvalue()))


if __name__ == "__main__":
    unittest.main()
