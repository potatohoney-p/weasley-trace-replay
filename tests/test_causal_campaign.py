"""Regression checks for the causal campaign and its independent failure oracle."""

import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from demos import causal_campaign as campaign
from weasley_trace_replay import TraceError, compare, read_events, replay


class CausalCampaignTests(unittest.TestCase):
    def test_saved_campaign_is_exactly_reproducible(self):
        expected = json.loads(Path("experiments/causal-result.json").read_text(encoding="utf-8"))
        self.assertEqual(campaign.run(), expected)
        self.assertEqual(expected["graphs"], 3)
        self.assertEqual(expected["schedules"], 12)
        self.assertEqual(expected["cancellations"], expected["prefixes"])
        self.assertEqual(expected["incomplete_traces_rejected"], expected["prefixes"])
        self.assertEqual(expected["invalid_cases_rejected"], 60)
        self.assertEqual(expected["drift_cases_detected"], 48)

    def test_graphs_have_substantive_concurrency_and_terminal_coverage(self):
        observed_statuses = set()
        for index, seed in enumerate(campaign.SEEDS):
            graph = campaign.graph(seed, actors=3 + index)
            order = campaign.schedule(graph, seed)
            seen = set()
            for node in order:
                self.assertTrue(set(node.after) <= seen)
                seen.add(node.key)
            ends = [n for n in graph if n.event["type"] == "step_finished"]
            self.assertEqual(len(ends), 2 * (3 + index))
            observed_statuses.update(n.event["status"] for n in ends)
            self.assertTrue(any(len({key.split("-step")[0] for key in n.after}) > 1 for n in graph))
            # All sibling calls must have started before any can return.
            for node in graph:
                if node.event["type"] == "tool_finished":
                    self.assertGreaterEqual(len(node.after), 2)
            self.assertEqual(campaign.oracle(order), campaign._observation(replay(campaign.records(order))))
        self.assertEqual(observed_statuses, {"ok", "error", "cancelled"})

    def test_schedule_and_oracle_do_not_mutate_graph(self):
        graph = campaign.graph(3)
        before = copy.deepcopy(graph)
        first = campaign.schedule(graph, 7)
        self.assertEqual(first, campaign.schedule(graph, 7))
        self.assertNotEqual(first, campaign.schedule(graph, 8))
        campaign.oracle(first)
        self.assertEqual(graph, before)

    def test_bad_graph_inputs_and_unsatisfied_dependencies_fail(self):
        for actors in (True, 2, 9):
            with self.assertRaises(ValueError):
                campaign.graph(3, actors)
        with self.assertRaises(ValueError):
            campaign.graph(True)
        with self.assertRaises(ValueError):
            campaign.schedule([campaign.Node("a", {}, ("b",))], 0)
        with self.assertRaises(ValueError):
            campaign.schedule([campaign.Node("a", {}), campaign.Node("a", {})], 0)
        for kwargs in ({"seeds": ()}, {"seeds": (True,)}, {"schedules": 0}, {"schedules": True}):
            with self.assertRaises(ValueError):
                campaign.run(**kwargs)

    def test_each_invalid_probe_has_an_independent_witness(self):
        order = campaign.schedule(campaign.graph(3), 17)
        for kind, invalid in campaign.invalid_cases(order).items():
            with self.subTest(kind=kind):
                self.assertTrue(campaign._invalid_witness(invalid, kind))
                with self.assertRaises(TraceError):
                    replay(campaign.records(invalid), allow_partial=True)

    def test_wrong_pending_state_is_caught_without_using_compare_as_oracle(self):
        def drop_pending(events, **kwargs):
            state = replay(events, **kwargs)
            state["unfinished_tools"] = []
            return state

        with self.assertRaises(campaign.CampaignFailure) as caught:
            campaign.run(seeds=(3,), schedules=1, replay_fn=drop_pending)
        failure = caught.exception
        self.assertEqual(failure.label, "interrupted_prefix_oracle")
        self.assertEqual([e["type"] for e in failure.trace],
                         ["run_started", "step_started", "tool_started"])
        self.assertNotEqual(campaign.oracle(failure.nodes), campaign._observation(
            drop_pending(failure.trace, allow_partial=True)))

    def test_comparator_that_ignores_drift_is_caught_and_reduced(self):
        with self.assertRaises(campaign.CampaignFailure) as caught:
            campaign.run(seeds=(3,), schedules=1,
                         compare_fn=lambda *_: {"equivalent": True, "changes": []})
        failure = caught.exception
        self.assertEqual(failure.label, "missed_tool_finished_digest")
        self.assertLess(len(failure.trace), failure.original_count)
        self.assertTrue(any(e["type"] == "tool_finished" for e in failure.trace))
        replay(failure.trace, allow_partial=True)
        self.assertFalse(compare(replay(failure.baseline_trace, allow_partial=True),
                                 replay(failure.trace, allow_partial=True))["equivalent"])
        self.assert_failure_artifact(failure)

    def test_false_interleaving_difference_saves_both_equivalent_traces(self):
        with self.assertRaises(campaign.CampaignFailure) as caught:
            campaign.run(seeds=(3,), schedules=1,
                         compare_fn=lambda *_: {"equivalent": False, "changes": []})
        failure = caught.exception
        self.assertEqual(failure.label, "interleaving_equivalence")
        self.assertIsNotNone(failure.baseline_trace)
        self.assertTrue(compare(replay(failure.baseline_trace, allow_partial=True),
                                replay(failure.trace, allow_partial=True))["equivalent"])
        self.assert_failure_artifact(failure)

    def test_invalid_acceptance_reduces_to_reproducing_duplicate_witness(self):
        # Intentionally faulty implementation only in this self-test. It models
        # a regression that silently drops duplicate results before replay.
        def ignore_duplicate(events, **kwargs):
            seen = set()
            filtered = []
            for event in events:
                if event["type"] == "tool_finished":
                    if event["call"] in seen:
                        continue
                    seen.add(event["call"])
                filtered.append(dict(event, seq=len(filtered)))
            return replay(filtered, **kwargs)

        with self.assertRaises(campaign.CampaignFailure) as caught:
            campaign.run(seeds=(3,), schedules=1, replay_fn=ignore_duplicate)
        failure = caught.exception
        self.assertEqual(failure.label, "accepted_duplicate_result")
        self.assertEqual(len(failure.trace), 5)
        self.assertTrue(campaign._invalid_witness(failure.nodes, "duplicate_result"))
        ignore_duplicate(failure.trace, allow_partial=True)
        with self.assertRaises(TraceError):
            replay(failure.trace, allow_partial=True)
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "failure"
            campaign.save_failure(failure, target)
            self.assertEqual(list(read_events(target / "counterexample.jsonl")), failure.trace)
            details = json.loads((target / "failure.json").read_text(encoding="utf-8"))
            self.assertTrue(details["synthetic"])
            with self.assertRaises(FileExistsError):
                campaign.save_failure(failure, target)
            # The command saves the same reproducer and returns a failed gate.
            with patch.object(campaign, "run", side_effect=failure), redirect_stdout(io.StringIO()) as out:
                code = campaign.main(["--failure-dir", str(Path(temp) / "cli-failure")])
            self.assertEqual(code, 1)
            self.assertFalse(json.loads(out.getvalue())["passed"])

    def assert_failure_artifact(self, failure):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "failure"
            with patch.object(campaign, "run", side_effect=failure), redirect_stdout(io.StringIO()) as out:
                self.assertEqual(campaign.main(["--failure-dir", str(target)]), 1)
            result = json.loads(out.getvalue())
            self.assertEqual(result["failure"], failure.label)
            self.assertEqual(result["minimality"], failure.minimality)
            self.assertEqual(result["synthetic_counterexample"], failure.trace)
            self.assertEqual(list(read_events(target / "counterexample.jsonl")), failure.trace)
            metadata = json.loads((target / "failure.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["context"], failure.context)
            self.assertEqual(result["synthetic_baseline"], failure.baseline_trace)
            if failure.baseline_trace is not None:
                self.assertEqual(list(read_events(target / metadata["baseline_file"])), failure.baseline_trace)
                self.assertEqual(list(read_events(target / metadata["candidate_file"])), failure.trace)
            else:
                self.assertFalse((target / "baseline.jsonl").exists())
                self.assertFalse((target / "candidate.jsonl").exists())

    def test_strict_only_rejection_keeps_a_complete_valid_reproducer(self):
        def reject_strict(events, **kwargs):
            state = replay(events, **kwargs)
            if not kwargs.get("allow_partial", False):
                raise TraceError("injected strict-only rejection")
            return state

        with self.assertRaises(campaign.CampaignFailure) as caught:
            campaign.run(seeds=(3,), schedules=1, replay_fn=reject_strict)
        failure = caught.exception
        self.assertEqual(failure.label, "strict_replay_rejection")
        self.assertNotEqual(replay(failure.trace)["status"], "running")
        self.assertTrue(campaign._valid_selection(failure.nodes))
        with self.assertRaises(TraceError):
            reject_strict(failure.trace)
        self.assertIn("1-minimal", failure.minimality)
        self.assert_failure_artifact(failure)

    def test_append_only_rejection_is_a_typed_saved_failure(self):
        strict_calls = 0

        def reject_later_strict(events, **kwargs):
            nonlocal strict_calls
            state = replay(events, **kwargs)
            if not kwargs.get("allow_partial", False):
                strict_calls += 1
                if strict_calls > 1:
                    raise TraceError("injected append rejection")
            return state

        with self.assertRaises(campaign.CampaignFailure) as caught:
            campaign.run(seeds=(3,), schedules=1, replay_fn=reject_later_strict)
        failure = caught.exception
        self.assertEqual(failure.label, "append_replay_rejection")
        self.assertEqual(failure.context["prefix_events"], 1)
        self.assertNotEqual(replay(failure.trace)["status"], "running")
        self.assert_failure_artifact(failure)

    def test_append_mismatch_is_saved_with_repeat_replay_context(self):
        strict_calls = 0

        def nondeterministic_hash(events, **kwargs):
            nonlocal strict_calls
            state = replay(events, **kwargs)
            if not kwargs.get("allow_partial", False):
                strict_calls += 1
                state["trace_sha256"] = f"{strict_calls:064x}"
            return state

        with self.assertRaises(campaign.CampaignFailure) as caught:
            campaign.run(seeds=(3,), schedules=1, replay_fn=nondeterministic_hash)
        failure = caught.exception
        self.assertEqual(failure.label, "append_replay_mismatch")
        self.assertEqual(failure.context["prefix_events"], 1)
        self.assertNotEqual(nondeterministic_hash(failure.trace), nondeterministic_hash(failure.trace))
        self.assert_failure_artifact(failure)

    def test_transient_append_mismatch_preserves_original_without_false_minimality(self):
        strict_calls = 0

        def transient_hash(events, **kwargs):
            nonlocal strict_calls
            state = replay(events, **kwargs)
            if not kwargs.get("allow_partial", False):
                strict_calls += 1
                if strict_calls == 2:
                    state["trace_sha256"] = "0" * 64
            return state

        with self.assertRaises(campaign.CampaignFailure) as caught:
            campaign.run(seeds=(3,), schedules=1, replay_fn=transient_hash)
        failure = caught.exception
        self.assertEqual(failure.label, "append_replay_mismatch")
        self.assertEqual(len(failure.trace), failure.original_count)
        self.assertIn("not reduced", failure.minimality)
        self.assert_failure_artifact(failure)

    def test_transient_strict_rejection_preserves_failure_instead_of_raising_valueerror(self):
        strict_calls = 0

        def reject_once(events, **kwargs):
            nonlocal strict_calls
            state = replay(events, **kwargs)
            if not kwargs.get("allow_partial", False):
                strict_calls += 1
                if strict_calls == 1:
                    raise TraceError("transient strict-only rejection")
            return state

        with self.assertRaises(campaign.CampaignFailure) as caught:
            campaign.run(seeds=(3,), schedules=1, replay_fn=reject_once)
        failure = caught.exception
        self.assertEqual(failure.label, "strict_replay_rejection")
        self.assertIn("not reduced", failure.minimality)
        self.assertEqual(len(failure.trace), failure.original_count)
        self.assert_failure_artifact(failure)

    def test_reducer_preserves_failure_and_reaches_single_deletion_minimality(self):
        predicate = lambda items: 2 in items and 8 in items
        reduced = campaign.minimize(list(range(10)), predicate)
        self.assertEqual(reduced, [2, 8])
        self.assertTrue(predicate(reduced))
        self.assertTrue(all(not predicate(reduced[:i] + reduced[i + 1:]) for i in range(len(reduced))))
        with self.assertRaises(ValueError):
            campaign.minimize([1, 3], predicate)

    def test_successful_cli_never_creates_failure_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "unused"
            with patch.object(campaign, "run", return_value={"passed": True}), redirect_stdout(io.StringIO()):
                self.assertEqual(campaign.main(["--failure-dir", str(directory)]), 0)
            self.assertFalse(directory.exists())


if __name__ == "__main__":
    unittest.main()
