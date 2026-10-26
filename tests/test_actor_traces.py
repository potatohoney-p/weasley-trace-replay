import copy
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import itertools
import json
from pathlib import Path
import tempfile
import unittest

from weasley_trace_replay import TraceError, compare, replay
from weasley_trace_replay.__main__ import main


def numbered(body, status="completed", run="synthetic"):
    records = [{"type": "run_started"}, *body]
    if status is not None:
        records.append({"type": "run_finished", "status": status})
    return [dict(v=1, seq=i, run=run, **row) for i, row in enumerate(records)]


def actor_rows(actor="worker", order=("first", "second")):
    shared = {"actor": actor, "step": actor + "Step"}
    rows = [dict(type="step_started", **shared)]
    for call in order:
        rows.extend([dict(type="tool_started", call=actor + call, tool="lookup", **shared),
                     dict(type="tool_finished", call=actor + call, status="ok", digest="a" * 64, **shared)])
    return [*rows, dict(type="step_finished", status="ok", **shared)]


def projected_lists(events):
    """Independent non-hashing equivalence oracle; retains explicit event lists."""
    actors = {}
    for event in events:
        if "actor" in event:
            actors.setdefault(event["actor"], []).append({k: v for k, v in event.items() if k not in ("seq", "run")})
    return actors


class ActorTraceTests(unittest.TestCase):
    def compare(self, *args, **kwargs):
        # Deferred import lets the baseline CLI regression execute as well.
        from weasley_trace_replay import compare_actor_traces
        return compare_actor_traces(*args, **kwargs)

    def assert_oracle(self, left, right, *, partial=False):
        expected = projected_lists(left) == projected_lists(right)
        result = self.compare(iter(left), iter(right), allow_partial=partial)
        observations = compare(replay(left, allow_partial=partial), replay(right, allow_partial=partial))
        self.assertEqual(result["actor_traces"]["equivalent"], expected)
        self.assertEqual(result["observations"], observations)
        self.assertEqual(result["equivalent"], expected and observations["equivalent"])
        return result

    def test_valid_sequential_reorder_is_visible_only_in_opt_in_mode(self):
        left = numbered(actor_rows())
        right = numbered(actor_rows(order=("second", "first")))
        self.assertTrue(compare(replay(left), replay(right))["equivalent"])
        result = self.assert_oracle(left, right)
        self.assertFalse(result["equivalent"])
        self.assertEqual([c["actor"] for c in result["actor_traces"]["changes"]], ["worker"])
        self.assertNotEqual(result["baseline_trace_sha256"], result["candidate_trace_sha256"])

    def test_all_cross_actor_interleavings_preserve_local_sequences(self):
        a, b = actor_rows("A", ("one",)), actor_rows("B", ("one",))
        baseline = numbered(a + b)
        count = 0
        for slots in itertools.combinations(range(len(a) + len(b)), len(a)):
            indexes = set(slots); ai, bi = iter(a), iter(b)
            mixed = [next(ai) if i in indexes else next(bi) for i in range(len(a) + len(b))]
            candidate = numbered(mixed, run="anotherRun")
            self.assertTrue(self.assert_oracle(baseline, candidate)["equivalent"])
            count += 1
        self.assertEqual(count, 70)

    def test_valid_concurrent_same_actor_completion_orders_remain_exact(self):
        rows = actor_rows()
        concurrent = [rows[0], rows[1], rows[3], rows[2], rows[4], rows[5]]
        reversed_ends = [rows[0], rows[1], rows[3], rows[4], rows[2], rows[5]]
        self.assertTrue(compare(replay(numbered(concurrent)), replay(numbered(reversed_ends)))["equivalent"])
        self.assertFalse(self.assert_oracle(numbered(concurrent), numbered(reversed_ends))["equivalent"])

    def test_step_order_and_checkpoint_placement_are_not_discarded(self):
        a = actor_rows(order=())
        b = [{**row, "step": "otherStep"} for row in a]
        self.assertFalse(self.assert_oracle(numbered(a + b), numbered(b + a))["equivalent"])
        rows = actor_rows(order=("first",))
        checkpoint = dict(type="checkpoint", actor="worker", step="workerStep", digest="b" * 64)
        left = numbered([rows[0], checkpoint, *rows[1:]])
        right = numbered([*rows[:-1], checkpoint, rows[-1]])
        self.assertTrue(compare(replay(left), replay(right))["equivalent"])
        self.assertFalse(self.assert_oracle(left, right)["equivalent"])

    def test_usage_segmentation_and_explicit_zero_are_exact(self):
        rows = actor_rows(order=())
        usage = dict(type="usage", actor="worker", step="workerStep", input_tokens=2, output_tokens=0)
        cases = [([usage], [{**usage, "input_tokens": 1}] * 2),
                 ([], [{**usage, "input_tokens": 0}])]
        for before, after in cases:
            left = numbered([rows[0], *before, rows[1]])
            right = numbered([rows[0], *after, rows[1]])
            self.assertTrue(compare(replay(left), replay(right))["equivalent"])
            self.assertFalse(self.assert_oracle(left, right)["equivalent"])

    def test_partial_failed_and_cancelled_traces_retain_pending_evidence(self):
        for status in (None, "failed", "cancelled"):
            left = numbered(actor_rows()[:2], status=status)
            self.assertTrue(self.assert_oracle(left, copy.deepcopy(left), partial=True)["equivalent"])
        with self.assertRaises(TraceError):
            self.compare(numbered(actor_rows()[:2], status=None), numbered(actor_rows()))

    def test_run_terminal_difference_is_not_hidden_by_equal_actor_sequences(self):
        result = self.assert_oracle(numbered([]), numbered([], status="failed"))
        self.assertTrue(result["actor_traces"]["equivalent"])
        self.assertFalse(result["equivalent"])
        self.assertFalse(result["observations"]["equivalent"])

    def test_missing_actors_are_sorted_and_distinct_from_empty_projection(self):
        result = self.assert_oracle(numbered(actor_rows("Z", ()) + actor_rows("A", ())), numbered([]))
        changes = result["actor_traces"]["changes"]
        self.assertEqual([change["actor"] for change in changes], ["A", "Z"])
        self.assertTrue(all(change["after"] is None for change in changes))

    def test_fingerprints_match_independent_literal_canonicalization(self):
        left = numbered(actor_rows())
        right = numbered(actor_rows(order=("second", "first")))
        before = self.compare(left, right)["actor_traces"]["changes"][0]["before"]
        expected_bytes = b"".join((json.dumps(row, sort_keys=True, ensure_ascii=True,
            separators=(",", ":"), allow_nan=False) + "\n").encode("ascii")
            for row in projected_lists(left)["worker"])
        self.assertEqual(before, {"events": 6, "sha256": hashlib.sha256(expected_bytes).hexdigest()})

    def test_one_pass_inputs_immutability_and_detached_result(self):
        left, right = numbered(actor_rows()), numbered(actor_rows(order=("second", "first")))
        saved = copy.deepcopy((left, right))
        class Once:
            def __init__(self, rows): self.rows = rows; self.used = False
            def __iter__(self):
                if self.used: raise AssertionError("input consumed twice")
                self.used = True
                return iter(self.rows)
        result = self.compare(Once(left), Once(right))
        result["actor_traces"]["changes"][0]["before"]["events"] = -1
        self.assertEqual((left, right), saved)
        self.assertEqual(self.compare(left, right)["actor_traces"]["changes"][0]["before"]["events"], 6)

    def test_invalid_prefix_suffix_and_cross_actor_attribution_fail_closed(self):
        valid = numbered(actor_rows())
        cases = [[], [{**valid[0], "seq": 2}], valid + [valid[-1]],
                 [*valid[:2], {**valid[2], "actor": "intruder"}, *valid[3:]]]
        for invalid in cases:
            for left, right in ((valid, invalid), (invalid, valid)):
                with self.assertRaises(TraceError):
                    self.compare(left, right, allow_partial=True)

    def test_cli_mode_exit_policy_and_incompatible_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            a, b = Path(directory) / "a.jsonl", Path(directory) / "b.jsonl"
            def write(path, rows):
                path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            write(a, numbered(actor_rows())); write(b, numbered(actor_rows(order=("second", "first"))))
            for args, expected in (([], 0), (["--actor-traces"], 1)):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err): code = main(["diff", str(a), str(b), *args])
                self.assertEqual(code, expected)
                self.assertEqual(err.getvalue(), "")
                if args: self.assertEqual(json.loads(out.getvalue())["mode"], "actor-local-v1")
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                main(["diff", str(a), str(b), "--actor-traces", "--policy", "unused"])
            self.assertEqual(caught.exception.code, 2)
            write(b, [{"private": "must-not-echo"}])
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err): code = main(["diff", str(a), str(b), "--actor-traces"])
            self.assertEqual(code, 2); self.assertEqual(out.getvalue(), "")
            self.assertNotIn("must-not-echo", err.getvalue())
