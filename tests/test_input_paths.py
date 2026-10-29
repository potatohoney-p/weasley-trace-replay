"""Malformed file names must not escape the content-free CLI error boundary."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from weasley_trace_replay import TraceError, read_events, read_snapshot, snapshot
from weasley_trace_replay.__main__ import main
from weasley_trace_replay.policy import read_policy


class InputPathTests(unittest.TestCase):
    def readers(self, path):
        return (lambda: list(read_events(path)), lambda: read_snapshot(path),
                lambda: read_policy(path))

    def test_embedded_nul_paths_are_trace_errors(self):
        for reader in self.readers("private-marker\x00secret"):
            with self.subTest(reader=reader), self.assertRaisesRegex(
                    TraceError, "invalid input path"):
                reader()

    def test_filesystem_encoding_errors_are_trace_errors(self):
        # Deterministic on Windows and POSIX, independent of filesystem encoding.
        error = UnicodeEncodeError("utf-8", "private-marker\ud800", 14, 15, "invalid")
        for reader in self.readers("unused"):
            with self.subTest(reader=reader), patch.object(Path, "open", side_effect=error):
                with self.assertRaisesRegex(TraceError, "invalid input path"):
                    reader()

    def test_operating_system_errors_retain_library_semantics(self):
        for reader in self.readers("unused"):
            with self.subTest(reader=reader), patch.object(Path, "open", side_effect=PermissionError):
                with self.assertRaises(PermissionError):
                    reader()

    def test_all_cli_file_arguments_reject_nul_without_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            start = dict(v=1, seq=0, run="demo", type="run_started")
            finish = dict(v=1, seq=1, run="demo", type="run_finished", status="completed")
            paths = {name: str(root / name) for name in ("trace", "prefix", "suffix", "saved", "policy")}
            for name, records in (("trace", [start, finish]), ("prefix", [start]), ("suffix", [finish])):
                Path(paths[name]).write_text("".join(json.dumps(e) + "\n" for e in records), encoding="utf-8")
            Path(paths["saved"]).write_text(json.dumps(snapshot([start])), encoding="utf-8")
            Path(paths["policy"]).write_text('{"policy_version":1,"usage_tolerances":{}}', encoding="utf-8")
            commands = [
                (["replay", paths["trace"]], [1]),
                (["snapshot", paths["prefix"]], [1]),
                (["resume", paths["saved"], paths["prefix"], paths["suffix"]], [1, 2, 3]),
                (["diff", paths["trace"], paths["trace"], "--policy", paths["policy"]], [1, 2, 4]),
            ]
            for command, positions in commands:
                for position in positions:
                    bad = command.copy()
                    bad[position] = "private-marker\x00secret"
                    with self.subTest(command=command[0], position=position):
                        stdout, stderr = io.StringIO(), io.StringIO()
                        with redirect_stdout(stdout), redirect_stderr(stderr):
                            self.assertEqual(main(bad), 2)
                        self.assertEqual(stdout.getvalue(), "")
                        self.assertEqual(stderr.getvalue(), '{"error": "invalid_or_unreadable_trace"}\n')


if __name__ == "__main__":
    unittest.main()
