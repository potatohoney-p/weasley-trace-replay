"""Snapshot boundary, hostile input, atomicity, and independent-oracle checks."""

import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from demos.causal_campaign import SEEDS, _number, graph, oracle, records, schedule
from weasley_trace_replay import (
    ReplayState, TraceError, read_events, read_snapshot, replay, resume,
    serialize_snapshot, snapshot,
)
from weasley_trace_replay.__main__ import main
from test_replay import sample


def assert_resume_parity(test, saved, prefix, suffix, expected, resume_fn=resume):
    test.assertEqual(resume_fn(saved, iter(prefix), iter(suffix), allow_partial=True), expected)


class SnapshotTests(unittest.TestCase):
    def test_every_campaign_prefix_has_exact_resume_parity_and_independent_state(self):
        boundaries = 0
        interrupted = 0
        for graph_index, seed in enumerate(SEEDS):
            nodes = graph(seed, 3 + graph_index)
            for schedule_index in range(4):
                order = schedule(nodes, _number(seed, schedule_index))
                events = records(order)
                expected = replay(events)
                # Check the digest independently of both replay wrappers.
                encoded = b"".join(json.dumps(e, sort_keys=True, separators=(",", ":"),
                                              ensure_ascii=True).encode() + b"\n" for e in events)
                self.assertEqual(expected["trace_sha256"], hashlib.sha256(encoded).hexdigest())
                cursor = ReplayState()
                for stop, event in enumerate(events, 1):
                    cursor.append(event)
                    saved = json.loads(serialize_snapshot(cursor.snapshot()))
                    report = cursor.report(allow_partial=True)
                    self.assertEqual({key: report[key] for key in oracle(order[:stop])}, oracle(order[:stop]))
                    assert_resume_parity(self, saved, events[:stop], events[stop:], expected)
                    boundaries += 1
                    if stop < len(events):
                        interrupted += 1
                        cancelled = dict(v=1, seq=stop, run=events[0]["run"],
                                         type="run_finished", status="cancelled")
                        suffix = [cancelled]
                        cancellation = resume(saved, events[:stop], suffix)
                        self.assertEqual(cancellation, replay(events[:stop] + suffix))
                        terminal_saved = snapshot(events[:stop] + suffix)
                        self.assertEqual(resume(terminal_saved, events[:stop] + suffix, []), cancellation)
        self.assertEqual(interrupted, 1068)
        self.assertEqual(boundaries, 1080)  # also covers each terminal snapshot

    def test_incremental_report_wrapper_and_detached_values(self):
        events = sample()
        cursor = ReplayState()
        for stop, event in enumerate(events, 1):
            cursor.append(event)
            self.assertEqual(cursor.report(allow_partial=True), replay(events[:stop], allow_partial=True))
        expected = replay(events)
        report, saved = cursor.report(), cursor.snapshot()
        report["tools"]["call1"]["status"] = "running"
        report["checkpoints"]["lookup"].clear()
        saved["state"]["steps"].clear()
        events[0]["run"] = "changed"
        self.assertEqual(cursor.report(), expected)

    def test_empty_and_partial_policies_are_explicit(self):
        with self.assertRaises(TraceError):
            snapshot([])
        cursor = ReplayState()
        for method in (cursor.report, cursor.snapshot):
            with self.assertRaises(TraceError):
                method()
        prefix = sample()[:3]
        saved = snapshot(prefix)
        with self.assertRaisesRegex(TraceError, "incomplete"):
            resume(saved, prefix, [])
        self.assertEqual(resume(saved, prefix, [], allow_partial=True), replay(prefix, allow_partial=True))
        # Failing strict report does not invalidate the incremental state.
        cursor.extend(prefix)
        with self.assertRaises(TraceError):
            cursor.report()
        cursor.extend(sample()[3:])
        self.assertEqual(cursor.report(), replay(sample()))

    def test_prefix_and_state_are_both_verified_not_just_digest(self):
        prefix = sample()[:6]
        saved = snapshot(prefix)
        candidates = [prefix[:-1], prefix + [sample()[6]], [], sample()[1:6]]
        edited = copy.deepcopy(prefix)
        edited[3]["digest"] = "b" * 64
        candidates.append(edited)
        mixed = copy.deepcopy(prefix)
        mixed[3]["run"] = "other"
        candidates.append(mixed)
        renamed = [dict(event, run="other") for event in prefix]
        candidates.append(renamed)
        for candidate in candidates:
            with self.subTest(prefix=candidate), self.assertRaises(TraceError):
                resume(saved, candidate, sample()[6:])
        for field, value in (("prefix_events", 5), ("prefix_sha256", "0" * 64),
                             ("snapshot_version", 2), ("trace_version", 2),
                             ("snapshot_version", True), ("trace_version", True),
                             ("prefix_events", True), ("prefix_events", 6.0),
                             ("format", "unknown")):
            changed = copy.deepcopy(saved)
            changed[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(TraceError):
                resume(changed, prefix, sample()[6:])
        # Each of these states is structurally valid. The original digest and
        # count must not let an edited deserialized observation through.
        for modify in (
            lambda s: s["usage"].update(input_tokens=11),
            lambda s: s["tools"]["call1"].update(digest="b" * 64),
            lambda s: s["checkpoints"]["lookup"].append("b" * 64),
            lambda s: s["tools"].pop("call1"),
        ):
            changed = copy.deepcopy(saved)
            modify(changed["state"])
            with self.assertRaisesRegex(TraceError, "verified prefix"):
                resume(changed, prefix, sample()[6:])

    def test_same_observations_do_not_let_an_edited_prefix_pass(self):
        data = sample()
        data.insert(6, dict(data[5], input_tokens=4, output_tokens=2))
        for seq, event in enumerate(data):
            event["seq"] = seq
        prefix = data[:7]
        changed = copy.deepcopy(prefix)
        changed[5]["input_tokens"] += 1
        changed[6]["input_tokens"] -= 1
        saved, edited = snapshot(prefix), snapshot(changed)
        self.assertEqual(saved["state"], edited["state"])
        self.assertNotEqual(saved["prefix_sha256"], edited["prefix_sha256"])
        consumed = []
        def suffix():
            consumed.append(True)
            yield from data[7:]
        with self.assertRaisesRegex(TraceError, "verified prefix"):
            resume(saved, changed, suffix())
        self.assertEqual(consumed, [])

    def test_checkpoint_order_is_part_of_verified_state(self):
        data = sample()
        data.insert(5, dict(data[4], digest="b" * 64))
        for seq, event in enumerate(data):
            event["seq"] = seq
        saved = snapshot(data[:6])
        saved["state"]["checkpoints"]["lookup"].reverse()
        with self.assertRaisesRegex(TraceError, "verified prefix"):
            resume(saved, data[:6], data[6:])

    def test_historical_step_and_call_ids_survive_restart(self):
        prefix = sample()[:7]
        cursor = ReplayState.from_snapshot(snapshot(prefix), prefix)
        duplicate_step = dict(prefix[1], seq=7)
        with self.assertRaisesRegex(TraceError, "reused"):
            cursor.append(duplicate_step)
        cursor.append(dict(duplicate_step, step="second"))
        before = cursor.snapshot()
        with self.assertRaisesRegex(TraceError, "reused"):
            cursor.append(dict(prefix[2], seq=8, step="second"))
        self.assertEqual(cursor.snapshot(), before)
        cursor.append(dict(prefix[6], seq=8, step="second"))
        cursor.append(dict(sample()[7], seq=9))
        self.assertIn("call1", cursor.report()["tools"])
        self.assertEqual(len(cursor.report()["steps"]), 2)

    def test_completed_result_cannot_be_recorded_again_after_restore(self):
        prefix = sample()[:4]
        cursor = ReplayState.from_snapshot(snapshot(prefix), prefix)
        before = cursor.snapshot()
        with self.assertRaisesRegex(TraceError, "already recorded"):
            cursor.append(dict(prefix[-1], seq=4))
        self.assertEqual(cursor.snapshot(), before)
        cursor.extend(sample()[4:])
        self.assertEqual(cursor.report(), replay(sample()))

    def test_invalid_appends_never_mutate_state_or_hash_and_are_recoverable(self):
        prefix = sample()[:3]
        cursor = ReplayState.from_snapshot(snapshot(prefix), prefix)
        bad = [dict(sample()[3], seq=0), dict(sample()[3], seq=4),
               dict(sample()[3], run="other"), dict(sample()[3], actor="other"),
               dict(sample()[3], call="missing"), dict(sample()[3], prompt="SECRET"),
               dict(sample()[6], seq=3), dict(sample()[7], seq=3)]
        for event in bad:
            before = cursor.snapshot()
            with self.assertRaises(TraceError):
                cursor.append(event)
            self.assertEqual(cursor.snapshot(), before)
        cursor.extend(sample()[3:])
        self.assertEqual(cursor.report(), replay(sample()))
        # The old one-shot transition mutated status before terminal validation.
        # First-event failure must likewise leave a new state reusable.
        empty = ReplayState()
        with self.assertRaises(TraceError):
            empty.append(dict(sample()[1], seq=0))
        empty.extend(sample())
        self.assertEqual(empty.report(), replay(sample()))

    def test_usage_second_field_overflow_rolls_back_first_field_and_hash(self):
        prefix = sample()[:6]
        prefix[5]["output_tokens"] = 2**53 - 1
        cursor = ReplayState.from_snapshot(snapshot(prefix), prefix)
        before = cursor.snapshot()
        with self.assertRaisesRegex(TraceError, "aggregate"):
            cursor.append(dict(prefix[-1], seq=6, input_tokens=1, output_tokens=1))
        self.assertEqual(cursor.snapshot(), before)
        cursor.append(dict(prefix[-1], seq=6, input_tokens=1, output_tokens=0))
        self.assertEqual(cursor.report(allow_partial=True)["usage"]["input_tokens"], 11)

    def test_extend_is_atomic_per_event_not_per_batch(self):
        cursor = ReplayState()
        data = sample()[:3] + [dict(sample()[3], seq=4)]
        with self.assertRaises(TraceError):
            cursor.extend(data)
        self.assertEqual(cursor.snapshot(), snapshot(sample()[:3]))
        cursor.extend(sample()[3:])
        self.assertEqual(cursor.report(), replay(sample()))

    def test_all_terminal_statuses_reject_any_suffix_and_keep_pending_work(self):
        traces = [sample()]
        for status in ("cancelled", "failed"):
            traces.append(sample()[:3] + [dict(sample()[-1], seq=3, status=status)])
        for trace in traces:
            saved = snapshot(trace)
            self.assertEqual(resume(saved, trace, []), replay(trace))
            cursor = ReplayState.from_snapshot(saved, trace)
            before = cursor.snapshot()
            with self.assertRaisesRegex(TraceError, "after terminal"):
                cursor.append(dict(sample()[-1], seq=len(trace)))
            self.assertEqual(cursor.snapshot(), before)
            if trace[-1]["status"] != "completed":
                self.assertEqual(cursor.report()["unfinished_tools"], ["call1"])

    def test_parity_checker_negative_controls_catch_count_hash_and_state_loss(self):
        prefix, suffix = sample()[:6], sample()[6:]
        saved, expected = snapshot(prefix), replay(sample())
        for field, value in (("events", len(suffix)), ("trace_sha256", "0" * 64),
                             ("tools", {}), ("usage", {"input_tokens": 0, "output_tokens": 0}),
                             ("checkpoints", {})):
            def broken(*args, field=field, value=value, **kwargs):
                result = resume(*args, **kwargs)
                result[field] = value
                return result
            with self.subTest(field=field), self.assertRaises(AssertionError):
                assert_resume_parity(self, saved, prefix, suffix, expected, broken)
        # Hashing a saved digest as data is not continuation of the prefix hash.
        wrong_hash = hashlib.sha256(bytes.fromhex(saved["prefix_sha256"]))
        for event in suffix:
            wrong_hash.update(json.dumps(event, sort_keys=True, separators=(",", ":")).encode() + b"\n")
        self.assertNotEqual(wrong_hash.hexdigest(), expected["trace_sha256"])


class SnapshotFileAndCliTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.prefix = self.directory / "prefix.jsonl"
        self.suffix = self.directory / "suffix.jsonl"
        self.saved = self.directory / "snapshot.json"
        self.prefix.write_text("\n".join(json.dumps(e) for e in sample()[:3]), encoding="utf-8")
        self.suffix.write_text("\n".join(json.dumps(e) for e in sample()[3:]), encoding="utf-8")
        self.saved.write_text(serialize_snapshot(snapshot(sample()[:3])), encoding="utf-8")

    def test_snapshot_and_resume_cli_roundtrip(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(main(["snapshot", str(self.prefix)]), 0)
        self.saved.write_text(stdout.getvalue(), encoding="utf-8")
        self.assertEqual(read_snapshot(self.saved), snapshot(sample()[:3]))
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(main(["resume", str(self.saved), str(self.prefix), str(self.suffix)]), 0)
        self.assertEqual(json.loads(stdout.getvalue()), replay(sample()))
        # JSON whitespace/key order are not trace changes: digest binds canonical
        # events rather than source file bytes.
        self.prefix.write_text("\n".join(json.dumps(e, sort_keys=True, indent=None,
                                                      separators=(",", ":")) for e in sample()[:3]))
        self.assertEqual(resume(read_snapshot(self.saved), read_events(self.prefix), read_events(self.suffix)), replay(sample()))

    def test_partial_resume_cli_and_content_free_error_contract(self):
        self.suffix.write_text("", encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        args = ["resume", str(self.saved), str(self.prefix), str(self.suffix)]
        with redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(main(args), 2)
            self.assertEqual(main(args + ["--allow-partial"]), 0)
        self.assertEqual(json.loads(stdout.getvalue()), replay(sample()[:3], allow_partial=True))
        self.assertEqual(json.loads(stderr.getvalue()), {"error": "invalid_or_unreadable_trace"})
        for target in (self.saved, self.prefix, self.suffix):
            original = target.read_bytes()
            target.write_text("PRIVATE_DO_NOT_ECHO", encoding="utf-8")
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(main(args), 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(json.loads(stderr.getvalue()), {"error": "invalid_or_unreadable_trace"})
            target.write_bytes(original)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(["snapshot", str(self.directory / "PRIVATE_DO_NOT_ECHO")]), 2)

    def test_snapshot_reader_rejects_torn_nonfinite_duplicate_and_oversized_json(self):
        original = self.saved.read_bytes()
        duplicate = original.replace(b'"snapshot_version":1', b'"snapshot_version":1,"snapshot_version":1')
        duplicate_identity = original.replace(b'"lookup":{"actor":"researcher","status":"running"}',
                                             b'"lookup":{"actor":"researcher","status":"running"},'
                                             b'"lookup":{"actor":"researcher","status":"running"}')
        for raw in (b"\xff", b"", b"[]", original[:-3], b'{"state":NaN}',
                    b'{"state":1e309}', duplicate, duplicate_identity, original + b"{}"):
            self.saved.write_bytes(raw)
            with self.subTest(raw=raw), self.assertRaises(TraceError):
                read_snapshot(self.saved)
        self.saved.write_bytes(original)
        with self.assertRaises(TraceError):
            read_snapshot(self.saved, max_bytes=len(original) - 1)
        self.assertEqual(read_snapshot(self.saved, max_bytes=len(original)), snapshot(sample()[:3]))
        for limit in (0, -1, True, 1.5):
            with self.assertRaises(TraceError):
                read_snapshot(self.saved, max_bytes=limit)

    def test_snapshot_state_shape_and_relations_fail_closed(self):
        saved = snapshot(sample()[:6])
        mutations = [
            lambda s: s.update(extra="SECRET"),
            lambda s: s.update(state=[]),
            lambda s: s["state"].update(extra="SECRET"),
            lambda s: s["state"].update(run="https://private.example"),
            lambda s: s["state"].update(status=[]),
            lambda s: s["state"].update(active={}),
            lambda s: s["state"].update(tools=[]),
            lambda s: s["state"]["steps"].update(second={"actor": "researcher", "status": "running"}),
            lambda s: s["state"]["steps"]["lookup"].update(status="ok"),
            lambda s: s["state"]["tools"]["call1"].update(actor="other"),
            lambda s: s["state"]["tools"]["call1"].update(step="missing"),
            lambda s: s["state"]["tools"]["call1"].update(status="running"),
            lambda s: s["state"]["tools"]["call1"].update(digest="bad"),
            lambda s: s["state"]["checkpoints"].update(missing=["a" * 64]),
            lambda s: s["state"]["checkpoints"].update(lookup=[]),
            lambda s: s["state"]["checkpoints"].update(lookup="a" * 64),
            lambda s: s["state"]["usage"].update(input_tokens=True),
            lambda s: s["state"]["usage"].update(output_tokens=2**53),
            lambda s: s["state"].update(status="completed"),
        ]
        for mutate in mutations:
            changed = copy.deepcopy(saved)
            mutate(changed)
            self.saved.write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaises(TraceError):
                read_snapshot(self.saved)
            with self.assertRaises(TraceError):
                serialize_snapshot(changed)
            with self.assertRaises(TraceError):
                ReplayState.from_snapshot(changed, sample()[:6])


if __name__ == "__main__":
    unittest.main()
