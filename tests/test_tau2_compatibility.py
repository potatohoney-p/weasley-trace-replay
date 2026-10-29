"""Independent mapping oracle, offline CLI, and explicit optional-backend gates."""

import copy
import builtins
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from demos import tau2_compatibility as acceptance


class Tau2CompatibilityTests(unittest.TestCase):
    def test_pinned_fixture_full_report_prefixes_and_meaningful_output_drift(self):
        with patch.object(acceptance, "resume", wraps=acceptance.resume) as resume:
            result = acceptance.run()
        self.assertTrue(result["passed"])
        self.assertEqual(result["fixture_canonical_sha256"], acceptance.FIXTURE_CANONICAL_SHA)
        self.assertEqual(result["facts"]["output_utf8_bytes"], 19)
        self.assertEqual(result["facts"]["sqlite_parity"], "not requested")
        self.assertEqual([call.args[0]["prefix_events"] for call in resume.call_args_list], list(range(1, 7)))
        drift = result["counterfactual_output_drift"]
        self.assertFalse(drift["equivalent"])
        self.assertEqual(len(drift["changes"]), 1)
        self.assertEqual(drift["changes"][0]["path"], f"/tools/{acceptance.CALL}/digest")
        self.assertFalse(result["provenance"]["usage"]["emitted"])
        manifest = json.loads(acceptance.MANIFEST.read_text(encoding="utf-8"))
        for key, value in (("source_url", "https://example.invalid/other.json"),
                           ("artifact_revision", "0" * 40),
                           ("runner_source_reproduced", True),
                           ("runner_source_reproduced", 0)):
            corrupted = {**manifest, key: value}
            with self.subTest(manifest_field=key, value=value), patch.object(
                    acceptance, "MANIFEST") as manifest_path:
                manifest_path.read_text.return_value = json.dumps(corrupted)
                with self.assertRaisesRegex(acceptance.AcceptanceError, "pinned source provenance"):
                    acceptance.run()

    def test_independent_report_oracle_rejects_result_status_count_hash_and_type_regressions(self):
        mutations = (
            lambda report: report["tools"][acceptance.CALL].update(digest="0" * 64),
            lambda report: report["tools"][acceptance.CALL].update(status="error"),
            lambda report: report.update(events=5),
            lambda report: report.update(trace_sha256="0" * 64),
            lambda report: report.update(version=True),
        )
        for mutate in mutations:
            corrupted = acceptance.expected_report()
            mutate(corrupted)
            with self.subTest(report=corrupted), patch.object(acceptance, "replay", return_value=corrupted):
                with self.assertRaisesRegex(acceptance.AcceptanceError, "independent full report"):
                    acceptance.run()

    def test_adapter_trace_corruption_is_rejected_before_replay(self):
        adapted = acceptance.adapt_episode(acceptance.read_episode(acceptance.FIXTURE))
        corrupted = copy.deepcopy(adapted)
        corrupted["events"][3]["digest"] = "0" * 64
        with patch.object(acceptance, "adapt_episode", return_value=corrupted), \
                patch.object(acceptance, "replay") as replay:
            with self.assertRaisesRegex(acceptance.AcceptanceError, "independent mapped trace"):
                acceptance.run()
        replay.assert_not_called()

    def test_unmapped_fixture_change_fails_pinned_fingerprint(self):
        row = json.loads(acceptance.FIXTURE.read_text(encoding="utf-8"))
        row["messages"][0]["content"] += " Changed greeting."
        with tempfile.TemporaryDirectory(prefix="tau2-fixture-test-") as directory:
            path = Path(directory) / "episode.json"
            path.write_text(json.dumps(row), encoding="utf-8")
            with self.assertRaisesRegex(acceptance.AcceptanceError, "pinned fixture fingerprint"):
                acceptance.run(path)
            path.write_bytes(b" " * (acceptance.MAX_FIXTURE_BYTES + 2))
            with patch.object(acceptance, "read_episode") as read_episode:
                with self.assertRaisesRegex(acceptance.AcceptanceError, "fixture exceeds byte limit"):
                    acceptance.run(path)
            read_episode.assert_not_called()
        stream = io.BytesIO(b" " * (acceptance.MAX_FIXTURE_BYTES + 2))
        with patch.object(Path, "open") as opened:
            opened.return_value.__enter__.return_value.read = read = unittest.mock.Mock(wraps=stream.read)
            with self.assertRaisesRegex(acceptance.AcceptanceError, "fixture exceeds byte limit"):
                acceptance._fixture_bytes(Path("oversized.json"))
        read.assert_called_once_with(acceptance.MAX_FIXTURE_BYTES + 1)

    def test_cli_is_deterministic_and_contains_no_timing_claim(self):
        outputs = []
        original_import = builtins.__import__

        def without_sqlite(name, *args, **kwargs):
            if name in {"sqlite3", "_sqlite3"} or name.startswith("sqlite3."):
                raise ImportError("SQLite is unavailable")
            return original_import(name, *args, **kwargs)

        # Reload exercises module imports as well as the default execution path.
        with patch.object(builtins, "__import__", side_effect=without_sqlite):
            acceptance.importlib.reload(acceptance)
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(acceptance.main([]), 0)
        for _ in range(2):
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(acceptance.main([]), 0)
            outputs.append(output.getvalue())
        self.assertEqual(outputs[0], outputs[1])
        result = json.loads(outputs[0])
        self.assertNotIn("elapsed_ns", result)
        self.assertNotIn("recorded_at_utc", result)

    def test_explicit_sqlite_request_cannot_silently_skip_missing_or_incompatible_backend(self):
        for kwargs in ({"side_effect": ModuleNotFoundError("private installation path")},
                       {"return_value": object()}):
            with self.subTest(kwargs=kwargs), patch.object(acceptance.importlib, "import_module", **kwargs), \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(acceptance.main(["--sqlite"]), 1)
            self.assertEqual(json.loads(output.getvalue()),
                             {"passed": False, "error": "sqlite_backend_unavailable"})

    def test_cli_failure_is_sanitized_and_never_publishes_partial_acceptance(self):
        with patch.object(acceptance, "_check_prefixes", side_effect=ValueError("private state details")), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(acceptance.main([]), 1)
        self.assertEqual(json.loads(output.getvalue()),
                         {"passed": False, "error": "invalid_or_failed_acceptance"})


if __name__ == "__main__":
    unittest.main()
