"""Default in-memory append staging: isolation and history-copy regressions."""

import copy
import unittest
from unittest.mock import patch

from weasley_trace_replay import ReplayState, TraceError
from test_replay import sample


class StagingTests(unittest.TestCase):
    def test_append_does_not_deepcopy_historical_state(self):
        state = ReplayState()
        # Each event is already contract-validated and holds only scalar values.
        # Public exports still deep-copy; only append must avoid history traversal.
        with patch("weasley_trace_replay.replay.copy.deepcopy", side_effect=AssertionError("history copied")):
            state.extend(sample())
        self.assertEqual(state.report()["status"], "completed")

    def test_all_transition_kinds_leave_previous_staging_view_unchanged(self):
        events = sample()
        # Repeated checkpoint exercises the existing-list copy, not just setdefault.
        events.insert(5, dict(events[4], digest="b" * 64))
        for seq, event in enumerate(events):
            event["seq"] = seq
        state = ReplayState()
        views = []
        for event in events:
            views.append((state._state, copy.deepcopy(state._state)))
            state.append(event)
            for prior, expected in views:
                self.assertEqual(prior, expected)
        saved = state.snapshot()
        state.report()["tools"].clear()
        export = state.snapshot()
        export["state"]["checkpoints"]["lookup"].clear()
        self.assertEqual(state.snapshot(), saved)

    def test_usage_overflow_and_terminal_rejection_preserve_shared_history(self):
        events = sample()[:6]
        events[5]["output_tokens"] = 2**53 - 1
        state = ReplayState()
        state.extend(events)
        prior = state._state
        expected = copy.deepcopy(prior)
        before = state.snapshot()
        bad = [dict(events[-1], seq=6, input_tokens=1, output_tokens=1),
               dict(v=1, seq=6, run=events[0]["run"], type="run_finished", status="completed")]
        for event in bad:
            with self.assertRaises(TraceError):
                state.append(event)
            self.assertEqual(prior, expected)
            self.assertEqual(state.snapshot(), before)
        state.append(dict(events[-1], seq=6, input_tokens=1, output_tokens=0))
        self.assertIs(state._state["steps"], prior["steps"])
        self.assertIs(state._state["tools"], prior["tools"])
        self.assertIs(state._state["checkpoints"], prior["checkpoints"])
        self.assertEqual(prior, expected)


if __name__ == "__main__":
    unittest.main()
