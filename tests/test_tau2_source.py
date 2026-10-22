import copy
import io
import json
import unittest
from unittest.mock import patch

from demos import tau2_source as source
from weasley_trace_replay.tau2 import read_episode


class Tau2SourceTests(unittest.TestCase):
    def test_fetch_enforces_header_and_actual_read_bounds(self):
        for header, body in ((str(source.LIMIT + 1), b""), (None, b"x" * (source.LIMIT + 1))):
            response = io.BytesIO(body)
            response.headers = {} if header is None else {"Content-Length": header}
            with patch.object(source.urllib.request, "urlopen", return_value=response) as opened:
                with self.assertRaises(ValueError):
                    source.fetch_pinned()
                opened.assert_called_once_with(source.URL, timeout=60)
        with patch.object(source.urllib.request, "urlopen", side_effect=OSError):
            with patch("builtins.print") as printed:
                self.assertEqual(source.main(["--fetch-pinned"]), 1)
                self.assertEqual(json.loads(printed.call_args.args[0])["error"], "source_verification_failed")

    def test_original_pin_rejects_truncated_or_different_archive(self):
        manifest = json.loads(source.MANIFEST.read_text(encoding="utf-8"))
        for raw in (b"{}", b"x" * source.ARTIFACT_BYTES):
            with self.assertRaisesRegex(ValueError, "archive identity mismatch"):
                source.verify(raw, manifest, read_episode(source.FIXTURE))

    def test_verification_protocol_detects_projection_identity_and_ledger_changes(self):
        # Small fabricated archive exercises the verifier, not an upstream result.
        fixture = read_episode(source.FIXTURE)
        row = copy.deepcopy(fixture)
        row["duration"] = 1.0
        archive = {"simulations": [row], "info": {"git_commit": "test-revision"}}
        manifest = {"source_url": source.SOURCE_URL, "artifact_revision": source.REVISION,
                    "runner_source_reproduced": False,
                    "selected_identity": {key: row[key] for key in ("id", "task_id", "trial", "seed")},
                    "original_row_canonical_sha256": source.digest(row),
                    "integral_float_normalized_row_sha256": source.digest(source.normalized_numbers(row)),
                    "fixture_canonical_sha256": source.digest(fixture),
                    "fixture_omitted_fields": [{"path": "/duration", "scope": "entire subtree"}],
                    "declared_runner_revision": "test-revision"}
        self.assertNotEqual(manifest["original_row_canonical_sha256"], manifest["integral_float_normalized_row_sha256"])

        def check(data, metadata=manifest, kept=fixture):
            raw = source.canonical(data)
            sha = source.hashlib.sha256(raw).hexdigest()
            with patch.object(source, "ARTIFACT_BYTES", len(raw)), patch.object(source, "ARTIFACT_SHA", sha):
                return source.verify(raw, dict(metadata, artifact_bytes=len(raw), artifact_sha256=sha), kept)

        self.assertTrue(check(archive)["full_row_projection_parity"])
        for rows in ([], [row, row]):
            with self.assertRaisesRegex(ValueError, "missing or duplicated"):
                check(dict(archive, simulations=rows))
        for key, value in (("fixture_omitted_fields", []), ("original_row_canonical_sha256", "bad"),
                           ("source_url", "https://example.invalid/other"), ("artifact_revision", "other"),
                           ("runner_source_reproduced", True), ("runner_source_reproduced", 0),
                           ("declared_runner_revision", "different"),
                           ("selected_identity", dict(manifest["selected_identity"], seed=1))):
            with self.subTest(key=key), self.assertRaises(ValueError):
                check(archive, dict(manifest, **{key: value}))
        changed = copy.deepcopy(fixture)
        changed["messages"][3]["content"] += "changed"
        with self.assertRaisesRegex(ValueError, "projection mismatch"):
            check(archive, kept=changed)


if __name__ == "__main__":
    unittest.main()
