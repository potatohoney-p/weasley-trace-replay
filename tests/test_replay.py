import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from weasley_trace_replay import TraceError, compare, read_events, replay
from weasley_trace_replay.__main__ import main

DIGEST = "a" * 64


def events(*records, run="demo"):
    return [dict(v=1, seq=i, run=run, **record) for i, record in enumerate(records)]


def sample():
    return events(
        {"type": "run_started"},
        {"type": "step_started", "actor": "researcher", "step": "lookup"},
        {"type": "tool_started", "actor": "researcher", "step": "lookup", "call": "call1", "tool": "search"},
        {"type": "tool_finished", "actor": "researcher", "step": "lookup", "call": "call1", "status": "ok", "digest": DIGEST},
        {"type": "checkpoint", "actor": "researcher", "step": "lookup", "digest": DIGEST},
        {"type": "usage", "actor": "researcher", "step": "lookup", "input_tokens": 10, "output_tokens": 3},
        {"type": "step_finished", "actor": "researcher", "step": "lookup", "status": "ok"},
        {"type": "run_finished", "status": "completed"},
    )


class ReplayTests(unittest.TestCase):
    def test_replays_and_hashes_deterministically(self):
        expected = replay(sample())
        self.assertEqual(expected, replay(iter(sample())))
        self.assertEqual(expected["usage"], {"input_tokens": 10, "output_tokens": 3})
        self.assertEqual(expected["tools"]["call1"]["status"], "ok")
        self.assertEqual(expected["checkpoints"], {"lookup": [DIGEST]})
        self.assertEqual(expected["unfinished_steps"], [])

    def test_partial_run_preserves_pending_work_and_can_be_replayed_after_append(self):
        partial = sample()[:3]
        with self.assertRaisesRegex(TraceError, "incomplete"):
            replay(partial)
        result = replay(partial, allow_partial=True)
        self.assertEqual(result["unfinished_tools"], ["call1"])
        self.assertEqual(result["unfinished_steps"], ["lookup"])
        self.assertEqual(result["status"], "running")
        self.assertEqual(replay(partial + sample()[3:]), replay(sample()))

    def test_cancelled_run_keeps_unfinished_work_observable(self):
        data = sample()[:3] + [{"v": 1, "seq": 3, "run": "demo", "type": "run_finished", "status": "cancelled"}]
        result = replay(data)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["unfinished_tools"], ["call1"])

    def test_completed_run_cannot_hide_pending_or_failed_steps(self):
        for prefix in (sample()[:3], sample()[:7]):
            data = copy.deepcopy(prefix)
            if len(data) == 7:
                data[-1]["status"] = "error"
            data.append(dict(v=1, seq=len(data), run="demo", type="run_finished", status="completed"))
            with self.assertRaises(TraceError):
                replay(data)

    def test_errors_can_be_recovered_by_explicit_later_successful_steps(self):
        data = sample()
        data[3]["status"] = "error"  # tool failure handled by the step
        self.assertEqual(replay(data)["status"], "completed")

    def test_rejects_duplicate_tool_result_and_cross_actor_attribution(self):
        for change in ("duplicate", "actor", "call"):
            data = sample()
            if change == "duplicate":
                data[4] = dict(data[3], seq=4)
            else:
                data[3][change] = "wrong"
            with self.assertRaises(TraceError):
                replay(data)

    def test_rejects_missing_start_sequence_gaps_and_mixed_run(self):
        bad = [sample()[1:], [], sample()]
        bad[2][2]["run"] = "different"
        for data in bad:
            with self.assertRaises(TraceError):
                replay(data)

    def test_rejects_second_start_events_after_finish_and_reused_ids(self):
        variants = []
        data = sample()
        data[1] = dict(v=1, seq=1, run="demo", type="run_started")
        variants.append(data)
        data = sample()
        data.append(dict(data[-1], seq=8))
        variants.append(data)
        data = sample()
        data[4] = dict(data[2], seq=4)
        variants.append(data)
        for data in variants:
            with self.assertRaises(TraceError):
                replay(data)

    def test_rejects_raw_content_unknown_fields_and_malformed_contracts(self):
        changes = [("prompt", "DO_NOT_ECHO"), ("v", True), ("v", 2), ("seq", False), ("type", []), ("run", "https://private.example")]
        for key, value in changes:
            data = sample()
            data[0][key] = value
            with self.assertRaises(TraceError) as caught:
                replay(data)
            self.assertNotIn("DO_NOT_ECHO", str(caught.exception))

    def test_usage_requires_bounded_exact_nonnegative_integers(self):
        for value in (-1, True, 1.5, float("inf"), 2**53):
            data = sample()
            data[5]["input_tokens"] = value
            with self.assertRaises(TraceError):
                replay(data)

    def test_aggregate_usage_overflow_is_rejected(self):
        data = sample()
        data[5]["input_tokens"] = 2**53 - 1
        data.insert(6, dict(data[5], input_tokens=1))
        for i, event in enumerate(data):
            event["seq"] = i
        with self.assertRaisesRegex(TraceError, "aggregate"):
            replay(data)

    def test_concurrent_actor_interleavings_are_observationally_equivalent(self):
        start = [{"type": "run_started"}]
        a = [{"type": "step_started", "actor": "a", "step": "a1"}, {"type": "step_finished", "actor": "a", "step": "a1", "status": "ok"}]
        b = [{"type": "step_started", "actor": "b", "step": "b1"}, {"type": "step_finished", "actor": "b", "step": "b1", "status": "ok"}]
        end = [{"type": "run_finished", "status": "completed"}]
        left = replay(events(*(start + a + b + end), run="left"))
        right = replay(events(*(start + [b[0], a[0], a[1], b[1]] + end), run="right"))
        self.assertNotEqual(left["trace_sha256"], right["trace_sha256"])
        self.assertEqual(compare(left, right), {"equivalent": True, "changes": []})

    def test_same_actor_cannot_overlap_steps(self):
        data = sample()
        data[2] = dict(v=1, seq=2, run="demo", type="step_started", actor="researcher", step="other")
        with self.assertRaisesRegex(TraceError, "active step"):
            replay(data)

    def test_step_cannot_finish_before_tool(self):
        data = sample()
        data[3] = dict(data[6], seq=3)
        with self.assertRaisesRegex(TraceError, "unfinished tools"):
            replay(data)

    def test_diff_locates_tool_checkpoint_and_usage_regressions(self):
        data = sample()
        data[3]["digest"] = "b" * 64
        data[4]["digest"] = "c" * 64
        data[5]["output_tokens"] = 9
        result = compare(replay(sample()), replay(data))
        self.assertFalse(result["equivalent"])
        self.assertEqual([d["path"] for d in result["changes"]], ["/tools/call1/digest", "/checkpoints/lookup", "/usage/output_tokens"])

    def test_input_objects_are_never_modified(self):
        data = sample()
        before = copy.deepcopy(data)
        replay(data)
        self.assertEqual(data, before)

    def test_exhaustive_interleavings_and_interruption_campaign(self):
        from demos.experiment import run
        result = run()
        self.assertEqual(result["equivalent_interleavings"], 252)
        self.assertEqual(result["interrupted_prefixes_recovered"], 11)
        self.assertEqual(len(result["detected_mutations"]), 3)
        self.assertTrue(result["invalid_duplicate_result_rejected"])


class FileAndCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "trace.jsonl"

    def write(self, data):
        self.path.write_text("\n".join(json.dumps(e) for e in data), encoding="utf-8")

    def test_jsonl_file_and_cli_roundtrip(self):
        self.write(sample())
        self.assertEqual(replay(read_events(self.path)), replay(sample()))
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main(["replay", str(self.path)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "completed")

    def test_bad_json_utf8_duplicate_keys_nonfinite_and_torn_lines(self):
        for raw in (b"\xff", b"\n", b'{"v":1,"v":1}', b'{"v":NaN}', b'{"seq":1e309}', b'{"v":'):
            self.path.write_bytes(raw)
            with self.assertRaises(TraceError):
                list(read_events(self.path))

    def test_line_size_is_bounded(self):
        self.path.write_bytes(b" " * 65)
        with self.assertRaises(TraceError):
            list(read_events(self.path, max_line_bytes=64))

    def test_cli_difference_and_invalid_input_have_distinct_exit_codes(self):
        self.write(sample())
        second = Path(self.temp.name) / "candidate.jsonl"
        data = sample()
        data[5]["output_tokens"] = 99
        second.write_text("\n".join(json.dumps(e) for e in data), encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["diff", str(self.path), str(self.path)]), 0)
            self.assertEqual(main(["diff", str(self.path), str(second)]), 1)
        self.path.write_text("PRIVATE_DO_NOT_ECHO", encoding="utf-8")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(main(["replay", str(self.path)]), 2)
        self.assertNotIn("PRIVATE_DO_NOT_ECHO", stderr.getvalue())
        self.assertEqual(json.loads(stderr.getvalue()), {"error": "invalid_or_unreadable_trace"})


if __name__ == "__main__":
    unittest.main()
