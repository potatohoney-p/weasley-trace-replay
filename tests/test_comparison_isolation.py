"""Comparison evidence must not share mutable observations with replay reports."""

import copy
import unittest

from weasley_trace_replay import compare, compare_with_policy, replay


def report(digest=None, *, pending_call=False, tokens=0):
    records = [{"type": "run_started"}]
    if digest is not None:
        records.append(dict(type="step_started", actor="actor", step="work"))
        if pending_call:
            records.append(dict(type="tool_started", actor="actor", step="work",
                                call="call", tool="lookup"))
        records.append(dict(type="checkpoint", actor="actor", step="work", digest=digest))
        records.append(dict(type="usage", actor="actor", step="work",
                            input_tokens=tokens, output_tokens=0))
    return replay([dict(v=1, seq=index, run="run", **record)
                   for index, record in enumerate(records)], allow_partial=True)


def with_policy(left, right):
    return compare_with_policy(left, right, {
        "policy_version": 1, "usage_tolerances": {"input_tokens": 1}})


class ComparisonIsolationTests(unittest.TestCase):
    def test_editing_checkpoint_evidence_cannot_erase_drift_or_change_reports(self):
        for compare_fn in (compare, with_policy):
            with self.subTest(api=compare_fn.__name__):
                left = report("a" * 64)
                right = report("b" * 64, tokens=int(compare_fn is with_policy))
                original = copy.deepcopy((left, right))
                result = compare_fn(left, right)
                expected = copy.deepcopy(result)
                checkpoint = next(change for change in result["changes"]
                                  if change["path"] == "/checkpoints/work")
                checkpoint["after"][0] = checkpoint["before"][0]
                self.assertFalse(compare_fn(left, right)["equivalent"])
                self.assertEqual((left, right), original)
                self.assertEqual(compare_fn(left, right), expected)
                # The other side is owned by the returned result as well.
                checkpoint["before"].clear()
                self.assertEqual((left, right), original)

    def test_added_removed_maps_and_unfinished_lists_are_detached(self):
        for compare_fn in (compare, with_policy):
            for reverse in (False, True):
                with self.subTest(api=compare_fn.__name__, reverse=reverse):
                    left, right = report(), report("a" * 64, pending_call=True)
                    if reverse:
                        left, right = right, left
                    original = copy.deepcopy((left, right))
                    result = compare_fn(left, right)
                    expected = copy.deepcopy(result)
                    self.assertEqual({change["path"] for change in result["changes"]}, {
                        "/steps/work", "/tools/call", "/checkpoints/work",
                        "/unfinished_steps", "/unfinished_tools"})
                    for change in result["changes"]:
                        for side in ("before", "after"):
                            if isinstance(change[side], (dict, list)):
                                change[side].clear()
                    self.assertEqual((left, right), original)
                    self.assertEqual(compare_fn(left, right), expected)

    def test_later_report_edits_cannot_rewrite_captured_comparison_evidence(self):
        pairs = [(report("a" * 64), report("b" * 64, tokens=1)),
                 (report(), report("a" * 64, pending_call=True))]
        for compare_fn in (compare, with_policy):
            for reports in pairs:
                with self.subTest(api=compare_fn.__name__, pending=bool(reports[1]["tools"])):
                    left, right = copy.deepcopy(reports)
                    result = compare_fn(left, right)
                    expected = copy.deepcopy(result)
                    for source in (left, right):
                        for entry in source["steps"].values():
                            entry["actor"] = "changed"
                        for entry in source["tools"].values():
                            entry["tool"] = "changed"
                        for checkpoints in source["checkpoints"].values():
                            checkpoints.clear()
                        source["unfinished_steps"].clear()
                        source["unfinished_tools"].clear()
                        source["usage"]["input_tokens"] = 99
                    self.assertEqual(result, expected)


if __name__ == "__main__":
    unittest.main()
