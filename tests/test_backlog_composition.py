"""Cross-feature contracts introduced by integrating the backlog."""
import copy
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from test_usage_observation import trace
from weasley_trace_replay import compare, replay
from weasley_trace_replay.__main__ import main


class BacklogCompositionTests(unittest.TestCase):
    def test_v2_evidence_is_detached_in_both_directions(self):
        left = replay(trace(), report_version=2)
        right = replay(trace(['one']), report_version=2)
        result = compare(left, right)
        original = copy.deepcopy(result)
        left['usage_observation']['recorded_steps'].append('changed')
        right['usage_observation']['recorded_steps'].append('changed')
        self.assertEqual(result, original)
        left = replay(trace(), report_version=2)
        right = replay(trace(['one']), report_version=2)
        before = copy.deepcopy((left, right))
        result = compare(left, right)
        for change in result['changes']:
            for field in ('before', 'after'):
                if isinstance(change[field], list):
                    change[field].append('changed')
        self.assertEqual((left, right), before)

    def test_v1_only_modes_reject_v2_before_reading_inputs(self):
        cases = [
            ['replay', 'private-path', '--report-format', 'jsonl', '--report-version', '2'],
            ['diff', 'private-path', 'other-path', '--actor-traces', '--report-version', '2'],
        ]
        for args in cases:
            with self.subTest(args=args), patch('weasley_trace_replay.__main__.read_events') as read:
                stdout, stderr = io.StringIO(), io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main(args)
                self.assertEqual(code, 2)
                self.assertEqual(stdout.getvalue(), '')
                self.assertEqual(json.loads(stderr.getvalue()), {'error': 'invalid_or_unreadable_trace'})
                read.assert_not_called()
