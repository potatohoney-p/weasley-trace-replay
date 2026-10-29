"""A failure to save artifacts must not hide a failed campaign's evidence."""

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from demos import causal_campaign as campaign
from weasley_trace_replay import read_events


class CampaignOutputTests(unittest.TestCase):
    def failure(self):
        # Exercise the real campaign's injected-regression seam: the comparator
        # deliberately hides drift, which must produce both sides of a failure.
        with self.assertRaises(campaign.CampaignFailure) as caught:
            campaign.run(seeds=(3,), schedules=1,
                         compare_fn=lambda *_: {"equivalent": True, "changes": []})
        return caught.exception

    def invoke_failure(self, failure, *args):
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(campaign, "run", side_effect=failure), redirect_stdout(output), redirect_stderr(errors):
            code = campaign.main(list(args))
        self.assertEqual(code, 1)
        self.assertEqual(errors.getvalue(), "")
        result = json.loads(output.getvalue())
        self.assertFalse(result["passed"])
        self.assertEqual(result["failure"], "missed_tool_finished_digest")
        self.assertEqual(result["synthetic_counterexample"], failure.trace)
        self.assertEqual(result["synthetic_baseline"], failure.baseline_trace)
        self.assertEqual(result["context"], failure.context)
        self.assertEqual(result["minimality"], failure.minimality)
        return result

    def test_existing_artifact_directory_preserves_failure_and_existing_files(self):
        failure = self.failure()
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "PRIVATE_PATH"
            directory.mkdir()
            sentinel = directory / "counterexample.jsonl"
            sentinel.write_bytes(b"existing evidence\n")
            result = self.invoke_failure(failure, "--failure-dir", str(directory))
            self.assertEqual(result["artifact_status"], "failed")
            self.assertEqual(result["artifact_error"], "unable_to_save_failure_artifacts")
            self.assertNotIn("PRIVATE_PATH", json.dumps(result))
            self.assertEqual(sentinel.read_bytes(), b"existing evidence\n")
            self.assertEqual(list(directory.iterdir()), [sentinel])

    def test_invalid_destination_still_reports_original_failure(self):
        failure = self.failure()
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp) / "PRIVATE_PATH"
            parent.write_bytes(b"existing file\n")
            for destination in (str(parent / "child"), "PRIVATE_PATH\x00invalid"):
                with self.subTest(destination=destination):
                    result = self.invoke_failure(failure, "--failure-dir", destination)
                    self.assertEqual(result["artifact_status"], "failed")
                    self.assertNotIn("PRIVATE_PATH", json.dumps(result))
            self.assertEqual(parent.read_bytes(), b"existing file\n")

    def test_directory_permission_and_encoding_errors_do_not_leak_paths(self):
        failure = self.failure()
        errors = (PermissionError("PRIVATE_PATH"),
                  UnicodeEncodeError("utf-8", "PRIVATE_PATH\ud800", 12, 13, "invalid"))
        for error in errors:
            with self.subTest(error=type(error).__name__), patch.object(Path, "mkdir", side_effect=error):
                result = self.invoke_failure(failure, "--failure-dir", "PRIVATE_PATH")
                self.assertEqual(result["artifact_status"], "failed")
                self.assertEqual(result["artifact_error"], "unable_to_save_failure_artifacts")
                self.assertNotIn("PRIVATE_PATH", json.dumps(result))

    def test_partial_artifact_writes_are_never_reported_as_saved(self):
        failure = self.failure()
        write_text = Path.write_text
        filenames = ("counterexample.jsonl", "baseline.jsonl", "candidate.jsonl", "failure.json")
        with tempfile.TemporaryDirectory() as temp:
            for index, filename in enumerate(filenames):
                directory = Path(temp) / str(index)

                def interrupted_write(path, data, *args, **kwargs):
                    if path.name == filename:
                        write_text(path, "{", *args, **kwargs)
                        raise OSError("disk full: PRIVATE_PATH")
                    return write_text(path, data, *args, **kwargs)

                with self.subTest(filename=filename), patch.object(Path, "write_text", interrupted_write):
                    result = self.invoke_failure(failure, "--failure-dir", str(directory))
                self.assertEqual(result["artifact_status"], "failed")
                self.assertEqual(result["artifact_error"], "unable_to_save_failure_artifacts")
                self.assertNotIn("PRIVATE_PATH", json.dumps(result))
                self.assertEqual((directory / filename).read_text(encoding="utf-8"), "{")
                self.assertEqual({p.name for p in directory.iterdir()}, set(filenames[:index + 1]))

    def test_saved_and_unrequested_artifacts_have_distinct_statuses(self):
        failure = self.failure()
        result = self.invoke_failure(failure)
        self.assertEqual(result["artifact_status"], "not_requested")
        self.assertNotIn("artifact_error", result)
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "saved"
            result = self.invoke_failure(failure, "--failure-dir", str(directory))
            self.assertEqual(result["artifact_status"], "saved")
            self.assertNotIn("artifact_error", result)
            self.assertEqual(list(read_events(directory / "counterexample.jsonl")), failure.trace)
            self.assertEqual(list(read_events(directory / "baseline.jsonl")), failure.baseline_trace)
            self.assertEqual(list(read_events(directory / "candidate.jsonl")), failure.trace)
            metadata = json.loads((directory / "failure.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["failure"], result["failure"])

    def test_unexpected_programming_errors_are_not_classified_as_filesystem_failures(self):
        failure = self.failure()
        with patch.object(campaign, "run", side_effect=failure), patch.object(
                Path, "mkdir", side_effect=RuntimeError("unexpected bug")):
            with self.assertRaisesRegex(RuntimeError, "unexpected bug"):
                campaign.main(["--failure-dir", "unused"])


if __name__ == "__main__":
    unittest.main()
