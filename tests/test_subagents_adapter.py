"""Synthetic projections only; no source runtime or provider is executed."""
import copy
import hashlib
import json
import unittest

from weasley_trace_replay import TraceError, compare, replay, resume, snapshot
from weasley_trace_replay.subagents import adapt_synthetic_subagents


def fixture():
    return [
        {"type": "subagent.run.started", "lifecycleArtifactVersion": 3, "runId": "synthetic-run"},
        {"type": "subagent.step.started", "runId": "synthetic-run", "stepIndex": 0, "agent": "worker"},
        {"type": "message_end", "subagentSource": "child", "subagentRunId": "synthetic-run",
         "subagentStepIndex": 0, "subagentAgent": "worker", "message": {"role": "assistant",
         "content": [{"type": "toolCall", "id": "call/one", "name": "read"}], "usage": {"input": 12, "output": 3}}},
        {"type": "tool_result_end", "subagentSource": "child", "subagentRunId": "synthetic-run",
         "subagentStepIndex": 0, "subagentAgent": "worker", "message": {"role": "toolResult",
         "toolCallId": "call/one", "toolName": "read", "isError": False,
         "content": [{"type": "text", "text": "synthetic result"}]}},
        {"type": "subagent.step.completed", "runId": "synthetic-run", "stepIndex": 0, "agent": "worker", "exitCode": 0},
        {"type": "subagent.run.completed", "lifecycleArtifactVersion": 3, "runId": "synthetic-run", "status": "complete"},
    ]


class SubagentsAdapterTests(unittest.TestCase):
    def test_literal_observation_and_no_input_mutation(self):
        source = fixture()
        before = copy.deepcopy(source)
        result = adapt_synthetic_subagents(source)
        events = result["events"]
        self.assertEqual(source, before)
        self.assertEqual([e["type"] for e in events], ["run_started", "step_started", "usage", "tool_started", "tool_finished", "step_finished", "run_finished"])
        self.assertEqual([e["seq"] for e in events], list(range(7)))
        report = replay(events)
        self.assertEqual(report["usage"], {"input_tokens": 12, "output_tokens": 3})
        self.assertEqual(report["status"], "completed")
        self.assertEqual(next(iter(report["tools"].values()))["digest"], hashlib.sha256(b'[{"text":"synthetic result","type":"text"}]').hexdigest())
        self.assertNotIn("synthetic result", json.dumps(result))
        self.assertNotIn("call/one", json.dumps(result))
        self.assertEqual(result, adapt_synthetic_subagents(source))
        source[3]["message"]["content"][0]["text"] = "changed"
        self.assertEqual(report, replay(events))

    def test_every_prefix_preserves_pending_work_and_resume(self):
        full = adapt_synthetic_subagents(fixture())["events"]
        for boundary in range(1, len(fixture()) + 1):
            prefix = adapt_synthetic_subagents(fixture()[:boundary])["events"]
            self.assertEqual(prefix, full[:len(prefix)])
            self.assertEqual(resume(snapshot(prefix), prefix, full[len(prefix):]), replay(full))
        pending = replay(adapt_synthetic_subagents(fixture()[:3])["events"], allow_partial=True)
        self.assertEqual(pending["status"], "running")
        self.assertEqual(len(pending["unfinished_tools"]), 1)
        with self.assertRaises(TraceError):
            replay(adapt_synthetic_subagents(fixture()[:3])["events"])

    def test_result_drift_and_block_boundaries(self):
        a, b = fixture(), fixture()
        a[3]["message"]["content"] = [{"type": "text", "text": "ab"}, {"type": "text", "text": "c"}]
        b[3]["message"]["content"] = [{"type": "text", "text": "a"}, {"type": "text", "text": "bc"}]
        self.assertFalse(compare(replay(adapt_synthetic_subagents(a)["events"]), replay(adapt_synthetic_subagents(b)["events"]))["equivalent"])

    def test_missing_usage_is_not_synthesized(self):
        source = fixture()
        source[2]["message"]["usage"] = None
        self.assertNotIn("usage", [e["type"] for e in adapt_synthetic_subagents(source)["events"]])

    def test_concurrent_same_named_agents_and_local_call_ids(self):
        one = fixture()
        two = copy.deepcopy(one[1:5])
        for record in two:
            record["subagentStepIndex" if "subagentStepIndex" in record else "stepIndex"] = 1
        source = [one[0], one[1], two[0], one[2], two[1], two[2], one[3], two[3], one[4], one[5]]
        report = replay(adapt_synthetic_subagents(source)["events"])
        self.assertEqual(len(report["tools"]), 2)
        self.assertEqual(len({s["actor"] for s in report["steps"].values()}), 2)
        alternate = [one[0], *one[1:5], *two, one[5]]
        self.assertTrue(compare(report, replay(adapt_synthetic_subagents(alternate)["events"]))["equivalent"])

    def test_failed_or_stopped_run_retains_unfinished_tools(self):
        for status, expected in [("failed", "failed"), ("stopped", "cancelled")]:
            source = fixture()[:3] + [fixture()[-1]]
            source[-1]["status"] = status
            report = replay(adapt_synthetic_subagents(source)["events"])
            self.assertEqual(report["status"], expected)
            self.assertEqual(len(report["unfinished_tools"]), 1)

    def test_rejects_ambiguous_or_unsupported_records(self):
        mutations = [
            lambda s: s[0].update(lifecycleArtifactVersion=True),
            lambda s: s[0].update(cwd="raw path"),
            lambda s: s[1].update(stepIndex=True),
            lambda s: s[2].update(subagentAgent="other"),
            lambda s: s[2].update(subagentRunId="other"),
            lambda s: s[2]["message"].update(usage={"input": True, "output": 3}),
            lambda s: s[2]["message"]["content"][0].update(arguments={}),
            lambda s: s[3]["message"].update(toolCallId="unknown"),
            lambda s: s[3]["message"].update(toolName="other"),
            lambda s: s[3]["message"].update(isError=None),
            lambda s: s[3]["message"]["content"][0].update(text="\ud800"),
            lambda s: s[4].update(exitCode=1),
            lambda s: s[-1].update(status="paused"),
            lambda s: s.insert(3, {"type": "tool_execution_start", "toolName": "read"}),
            lambda s: s.insert(3, {"type": "subagent.events.truncated"}),
            lambda s: s.insert(4, copy.deepcopy(s[3])),
            lambda s: s.insert(3, copy.deepcopy(s[2])),
            lambda s: s.pop(3),
            lambda s: s.append(copy.deepcopy(s[2])),
        ]
        for mutate in mutations:
            source = fixture()
            mutate(source)
            with self.subTest(source=source), self.assertRaises(TraceError):
                adapt_synthetic_subagents(source)

    def test_noop_child_record_after_step_or_run_terminal_rejected(self):
        for position in (5, 6):
            source = fixture()
            child = copy.deepcopy(source[2])
            child["message"].update(content=[], usage=None)
            source.insert(position, child)
            with self.assertRaises(TraceError):
                adapt_synthetic_subagents(source)

    def test_step_failure_explicit_outcome(self):
        source = fixture()
        source[4].update(type="subagent.step.failed", exitCode=1)
        source[-1]["status"] = "failed"
        report = replay(adapt_synthetic_subagents(source)["events"])
        self.assertEqual(next(iter(report["steps"].values()))["status"], "error")
