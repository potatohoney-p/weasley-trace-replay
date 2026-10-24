import copy
import json
import unittest

from weasley_trace_replay.assistant import adapt_synthetic_assistant as adapt
from weasley_trace_replay.replay import TraceError, replay, snapshot, resume


def start(nodes=None):
    return {"type": "graph_start", "workflow": "synthetic", "nodes": nodes or ["left", "right"]}


def ns(name, visit=1):
    return {"type": "node_start", "workflow": "synthetic", "node": name, "visit": visit}


def ne(name, error=None):
    return {"type": "node_end", "workflow": "synthetic", "node": name, "error": error}


def end(path, error=None):
    return {"type": "graph_end", "workflow": "synthetic", "path": path,
            "steps": len(path), "error": error}


def fixture():
    return [start(), ns("left"), ns("right"), ne("left"), ne("right"), end(["left", "right"])]


class AssistantAdapterTests(unittest.TestCase):
    def test_parallel_lifecycle_and_no_fabricated_measurements(self):
        result = adapt(fixture(), run_id="run1")
        self.assertEqual(result["report"]["status"], "completed")
        self.assertEqual(len(result["report"]["steps"]), 2)
        self.assertEqual(result["report"]["tools"], {})
        self.assertEqual(result["report"]["checkpoints"], {})
        self.assertEqual(result["report"]["usage"], {"input_tokens": 0, "output_tokens": 0})
        self.assertNotIn("usage", [e["type"] for e in result["events"]])

    def test_cycle_has_distinct_steps_same_actor(self):
        source = [start(), ns("left"), ne("left"), ns("left", 2), ne("left"), end(["left", "left"])]
        result = adapt(source, run_id="run1")
        steps = list(result["report"]["steps"].values())
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0]["actor"], steps[1]["actor"])

    def test_prefixes_preserve_pending_work_and_resume(self):
        source = fixture()
        full = adapt(source, run_id="run1")
        for cut in range(1, len(source) + 1):
            with self.subTest(cut=cut):
                prefix = adapt(source[:cut], run_id="run1")
                events = prefix["events"]
                self.assertEqual(prefix["report"], replay(events, allow_partial=True))
                self.assertEqual(resume(snapshot(events), events, full["events"][len(events):]), full["report"])
        self.assertEqual(len(adapt(source[:3], run_id="run1")["report"]["unfinished_steps"]), 2)
        self.assertEqual(adapt(source[:-1], run_id="run1")["report"]["status"], "running")

    def test_failed_nodes_and_engine_guard_failure(self):
        for source in ([start(), ns("left"), ne("left", "synthetic failure"), end(["left"], "failure")],
                       [start(), end([], "max_steps")]):
            self.assertEqual(adapt(source, run_id="run1")["report"]["status"], "failed")

    def test_errors_are_not_copied_to_output(self):
        secret = "synthetic-private-error"
        source = [start(), ns("left"), ne("left", secret), end(["left"], secret)]
        self.assertNotIn(secret, json.dumps(adapt(source, run_id="run1")))

    def test_result_does_not_alias_input(self):
        source = fixture()
        before = copy.deepcopy(source)
        result = adapt(source, run_id="run1")
        result["events"][0]["run"] = "changed"
        self.assertEqual(source, before)
        self.assertNotEqual(result["events"][0]["run"], result["report"]["run"])

    def test_invalid_identity_and_visit_rejected(self):
        bad = [[], [start(), ns("missing")], [start(), ns("left", True)],
               [start(), ns("left", 2)], [start(), ns("left"), ns("left")],
               [start(), ne("left")], [start(), ns("left"), ne("left"), ns("left")],
               [start(["same", "same"])], [start(["START"])]]
        for source in bad:
            with self.subTest(source=source), self.assertRaises(TraceError):
                adapt(source, run_id="run1")
        for run_id in (None, "", "\ud800"):
            with self.assertRaises(TraceError):
                adapt(fixture(), run_id=run_id)

    def test_invalid_terminal_and_unknown_payload_rejected(self):
        bad = [fixture() + [end(["left", "right"])], [start(), ns("left"), end([])],
               [start(), end(["left"])], [start(), {**end([]), "steps": False}],
               [start(), ns("left"), ne("left", "failure"), end(["left"])],
               [start(), {"type": "tool_start", "workflow": "synthetic", "name": "x"}],
               [start(), {**ns("left"), "content": "not permitted"}],
               [start(), {**ns("left"), "workflow": "other"}],
               [start(), ns("left"), ne("left", False)]]
        for source in bad:
            with self.subTest(source=source), self.assertRaises(TraceError):
                adapt(source, run_id="run1")

    def test_empty_graph_is_valid(self):
        result = adapt([{"type": "graph_start", "workflow": "empty", "nodes": []},
                        {"type": "graph_end", "workflow": "empty", "steps": 0,
                         "path": [], "error": None}], run_id="run1")
        self.assertEqual(result["report"]["status"], "completed")

    def test_late_start_and_mixed_workflow_fail(self):
        for source in ([ns("left")], [start(), start()], [start(), {**end([]), "workflow": "other"}]):
            with self.assertRaises(TraceError):
                adapt(source, run_id="run1")


if __name__ == "__main__":
    unittest.main()
