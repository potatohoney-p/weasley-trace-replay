"""Bounded synthetic acceptance checks for explicit comparison policies."""

import copy
import hashlib
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import tempfile
import unittest

from weasley_trace_replay import (TraceError, compare, compare_with_policy, read_events,
                                  read_policy, replay, validate_policy)
from weasley_trace_replay.__main__ import main

ROOT = Path(__file__).resolve().parents[1]


def policy(**limits):
    return {"policy_version": 1, "usage_tolerances": limits}


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.left = replay(read_events(ROOT / "examples/completed.jsonl"))
        self.right = copy.deepcopy(self.left)

    def test_empty_policy_is_exact_and_digest_is_canonical(self):
        rule = policy()
        for right in (self.right, replay(read_events(ROOT / "examples/drift.jsonl"))):
            result = compare_with_policy(self.left, right, rule)
            self.assertEqual(compare(self.left, right), {
                "equivalent": result["equivalent"], "changes": result["changes"]})
            self.assertEqual(result["exact_equivalent"], result["equivalent"])
            self.assertEqual(result["tolerated_changes"], [])
        expected = hashlib.sha256(b'{"policy_version":1,"usage_tolerances":{}}').hexdigest()
        self.assertEqual(result["policy_sha256"], expected)
        reordered = {"usage_tolerances": {}, "policy_version": 1}
        self.assertEqual(result["policy_sha256"], compare_with_policy(self.left, right, reordered)["policy_sha256"])

    def test_absolute_boundaries_both_directions(self):
        for before in (0, 10, 2**53 - 1):
            for after in (0, 1, 9, 10, 11, 2**53 - 1):
                for limit in (0, 1, 10, 2**53 - 1):
                    with self.subTest(before=before, after=after, limit=limit):
                        self.left["usage"]["input_tokens"] = before
                        self.right["usage"]["input_tokens"] = after
                        result = compare_with_policy(self.left, self.right, policy(input_tokens=limit))
                        self.assertEqual(result["equivalent"], abs(after - before) <= limit)
                        self.assertEqual(result["exact_equivalent"], before == after)
                        self.assertEqual(len(result["changes"]) + len(result["tolerated_changes"]), int(before != after))

    def test_tolerated_changes_are_explicit_and_unlisted_usage_is_exact(self):
        self.right["usage"]["input_tokens"] += 3
        self.right["usage"]["output_tokens"] += 1
        result = compare_with_policy(self.left, self.right, policy(input_tokens=3))
        self.assertFalse(result["equivalent"])
        self.assertFalse(result["exact_equivalent"])
        self.assertEqual([c["path"] for c in result["changes"]], ["/usage/output_tokens"])
        self.assertEqual(result["tolerated_changes"], [{
            "path": "/usage/input_tokens", "before": self.left["usage"]["input_tokens"],
            "after": self.right["usage"]["input_tokens"], "absolute_delta": 3, "absolute_tolerance": 3}])

    def test_every_non_usage_observation_remains_exact(self):
        for key in ("status", "steps", "tools", "checkpoints", "unfinished_steps", "unfinished_tools"):
            with self.subTest(key=key):
                right = copy.deepcopy(self.left)
                if key == "status": right[key] = "failed"
                elif isinstance(right[key], dict): right[key] = {"changed": "value"}
                else: right[key] = ["changed"]
                right["usage"]["input_tokens"] += 1
                result = compare_with_policy(self.left, right, policy(input_tokens=2**53 - 1, output_tokens=2**53 - 1))
                self.assertFalse(result["equivalent"])
                self.assertTrue(all(not c["path"].startswith("/usage/") for c in result["changes"]))

    def test_real_outcome_drift_cannot_be_tolerated(self):
        changed = replay(read_events(ROOT / "examples/drift.jsonl"))
        result = compare_with_policy(self.left, changed, policy(input_tokens=2**53 - 1, output_tokens=2**53 - 1))
        self.assertEqual(result["changes"], compare(self.left, changed)["changes"])
        self.assertFalse(result["equivalent"])

    def test_input_and_returned_values_are_detached(self):
        rule = policy(input_tokens=3)
        self.right["usage"]["input_tokens"] += 1
        before = copy.deepcopy((self.left, self.right, rule))
        result = compare_with_policy(self.left, self.right, rule)
        result["tolerated_changes"][0]["after"] = -1
        self.assertEqual((self.left, self.right, rule), before)

    def test_invalid_policy_shapes_and_limits(self):
        invalid = [None, [], True, {}, {"policy_version": 1},
                   {**policy(), "ignored": True}, policy(status=1), policy(usage=1)]
        for version in (True, 1.0, 2, "1", None):
            invalid.append({**policy(), "policy_version": version})
        for limits in ([], None, True, "input_tokens"):
            invalid.append({**policy(), "usage_tolerances": limits})
        for value in (True, False, -1, 2**53, 0.0, 1.0, float("nan"), float("inf"), "1", None, [], {}):
            invalid.append(policy(input_tokens=value))
        for rule in invalid:
            with self.subTest(rule=rule), self.assertRaises(TraceError):
                compare_with_policy(self.left, self.right, rule)

    def test_invalid_or_missing_usage_never_becomes_zero(self):
        for usage in (None, {}, {"input_tokens": 0}, {"output_tokens": 0},
                      {"input_tokens": 0, "output_tokens": 0, "extra": 0}):
            right = {**self.right, "usage": usage}
            with self.assertRaises(TraceError): compare_with_policy(self.left, right, policy())
        for value in (True, False, -1, 2**53, 0.0, float("nan"), float("inf"), "0", None):
            right = copy.deepcopy(self.right)
            right["usage"]["input_tokens"] = value
            with self.assertRaises(TraceError): compare_with_policy(self.left, right, policy(input_tokens=2**53 - 1))
        right = {k: v for k, v in self.right.items() if k != "usage"}
        with self.assertRaises(TraceError): compare_with_policy(self.left, right, policy())

    def test_policy_reader_strict_json_size_and_utf8(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            valid = b'{"policy_version":1,"usage_tolerances":{"input_tokens":3}}'
            path.write_bytes(valid)
            self.assertEqual(read_policy(path, max_bytes=len(valid)), policy(input_tokens=3))
            with self.assertRaises(TraceError): read_policy(path, max_bytes=len(valid)-1)
            for limit in (True, 0, -1, 1.0):
                with self.assertRaises(TraceError): read_policy(path, max_bytes=limit)
            for raw in (b'', b'null', b'[]', b'\xff', valid+b'{}', b'{"policy_version":1,"policy_version":1,"usage_tolerances":{}}',
                        b'{"policy_version":1,"usage_tolerances":{"input_tokens":1,"input_tokens":2}}',
                        b'{"policy_version":1,"usage_tolerances":{"input_tokens":NaN}}',
                        b'{"policy_version":1,"usage_tolerances":{"input_tokens":1e999}}',
                        b'['*2000+b']'*2000):
                path.write_bytes(raw)
                with self.subTest(raw=raw[:80]), self.assertRaises(TraceError): read_policy(path)

    def test_digest_identifies_allowlist_even_when_no_changes(self):
        digests = {compare_with_policy(self.left, self.right, p)["policy_sha256"]
                   for p in (policy(), policy(input_tokens=0), policy(input_tokens=1), policy(output_tokens=1))}
        self.assertEqual(len(digests), 4)

    def test_policy_does_not_change_existing_identity_and_order_semantics(self):
        self.right["run"] = "other"
        self.right["events"] += 1
        self.right["trace_sha256"] = "f"*64
        self.assertTrue(compare_with_policy(self.left, self.right, policy())["exact_equivalent"])

    def test_absent_usage_events_and_explicit_zero_preserve_legacy_semantics(self):
        events = list(read_events(ROOT / "examples/completed.jsonl"))
        self.assertFalse(any(event["type"] == "usage" for event in events))
        before = replay(events)
        index = next(i for i, event in enumerate(events) if event["type"] == "step_started")
        events.insert(index+1, {**events[index], "type": "usage", "input_tokens": 0, "output_tokens": 0})
        for seq, event in enumerate(events): event["seq"] = seq
        after = replay(events)
        self.assertNotEqual(before["trace_sha256"], after["trace_sha256"])
        self.assertTrue(compare(before, after)["equivalent"])
        self.assertTrue(compare_with_policy(before, after, policy())["exact_equivalent"])

    def test_cli_exit_codes_content_free_policy_errors_and_partial(self):
        def invoke(*args):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err): code = main(list(args))
            return code, out.getvalue(), err.getvalue()
        completed = str(ROOT / "examples/completed.jsonl")
        interrupted = str(ROOT / "examples/interrupted.jsonl")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_text(json.dumps(policy(input_tokens=100)))
            candidate = Path(directory) / "candidate.jsonl"
            events = list(read_events(completed))
            index = next(i for i, event in enumerate(events) if event["type"] == "step_started")
            usage = {**events[index], "type": "usage", "input_tokens": 100, "output_tokens": 0}
            events.insert(index + 1, usage)
            for seq, event in enumerate(events): event["seq"] = seq
            candidate.write_text("".join(json.dumps(event)+"\n" for event in events))
            self.assertEqual(invoke("diff", completed, str(candidate))[0], 1)
            code, out, _ = invoke("diff", completed, str(candidate), "--policy", str(path))
            self.assertEqual(code, 0)
            self.assertFalse(json.loads(out)["exact_equivalent"])
            usage["input_tokens"] += 1
            candidate.write_text("".join(json.dumps(event)+"\n" for event in events))
            self.assertEqual(invoke("diff", completed, str(candidate), "--policy", str(path))[0], 1)
            self.assertEqual(invoke("diff", completed, completed, "--policy", str(path))[0], 0)
            self.assertEqual(invoke("diff", completed, str(ROOT / "examples/drift.jsonl"), "--policy", str(path))[0], 1)
            self.assertEqual(invoke("diff", interrupted, interrupted, "--policy", str(path))[0], 2)
            self.assertEqual(invoke("diff", interrupted, interrupted, "--policy", str(path), "--allow-partial")[0], 0)
            path.write_text('{"PRIVATE_SENTINEL":"secret"}')
            code, out, err = invoke("diff", completed, completed, "--policy", str(path))
            self.assertEqual((code, out), (2, ""))
            self.assertEqual(json.loads(err), {"error": "invalid_or_unreadable_trace"})
            self.assertNotIn("PRIVATE_SENTINEL", err)
            code, out, err = invoke("diff", completed, completed, "--policy", str(path)+"missing")
            self.assertEqual((code, out), (2, ""))
            self.assertNotIn(directory, err)


if __name__ == "__main__":
    unittest.main()
