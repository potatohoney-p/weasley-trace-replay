"""Exact observation parity and fail-closed comparison of live disk histories."""
import copy
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from demos.scaling import Workload, events
from weasley_trace_replay import TraceError, compare, replay
from weasley_trace_replay.history_comparison import compare_histories
from weasley_trace_replay.sqlite_history import SQLiteReplayState, HistoryStorageError


class HistoryComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.serial = 0

    def state(self, source):
        self.serial += 1
        result = SQLiteReplayState(self.root / f'{self.serial}.sqlite')
        self.addCleanup(result.close)
        result.extend(source)
        return result

    def equivalent(self, left, right, partial=False):
        expected = compare(replay(left, allow_partial=partial), replay(right, allow_partial=partial))['equivalent']
        actual = compare_histories(self.state(left), self.state(right), allow_partial=partial)
        self.assertEqual(actual['equivalent'], expected)
        self.assertEqual(actual['first_difference'] is None, expected)
        return actual

    def test_every_prefix_matches_materialized_oracle(self):
        source = events(Workload(2, 2, 2))
        left, right = self.state([]), self.state([])
        for item in source:
            left.append(item)
            right.append(item)
            actual = compare_histories(left, right, allow_partial=True)
            self.assertTrue(actual['equivalent'])
            self.assertEqual(actual['equivalent'], compare(left.report(allow_partial=True), right.report(allow_partial=True))['equivalent'])
            self.assertEqual(actual['compared_records']['left'], actual['compared_records']['right'])
            self.assertFalse(left._reporting or right._reporting)

    def test_ignores_run_event_count_hash_and_global_interleaving(self):
        source = events(Workload(2, 1, 1))
        changed = copy.deepcopy(source)
        for item in changed:
            item['run'] = 'DifferentRun'
        self.equivalent(source, changed)
        # Actor-local traces grouped in a different valid global schedule.
        # Use the actual generator's actor IDs, not a guessed naming convention.
        actors = sorted({e['actor'] for e in source if 'actor' in e}, reverse=True)
        grouped = [source[0]] + [e for actor in actors for e in source if e.get('actor') == actor] + [source[-1]]
        for index, item in enumerate(grouped):
            item = copy.deepcopy(item)
            grouped[index] = item
            item['seq'] = index
        self.equivalent(source, grouped)
        changed = copy.deepcopy(source)
        at = next(i for i, e in enumerate(changed) if e['type'] == 'step_started') + 1
        owner = {key: changed[at-1][key] for key in ('run', 'actor', 'step')}
        changed.insert(at, dict(v=1, seq=0, type='usage', input_tokens=0, output_tokens=0, **owner))
        for i, e in enumerate(changed):
            e['seq'] = i
        self.equivalent(source, changed)

    def test_observation_mutations_match_materialized_oracle(self):
        source = events(Workload(2, 2, 2))
        for kind, field, value in (('tool_finished', 'digest', 'f'*64), ('checkpoint', 'digest', 'e'*64),
                                   ('usage', 'input_tokens', 19), ('run_finished', 'status', 'failed'),
                                   ('tool_started', 'tool', 'DifferentTool')):
            with self.subTest(kind=kind):
                changed = copy.deepcopy(source)
                next(e for e in changed if e['type'] == kind)[field] = value
                self.assertFalse(self.equivalent(source, changed)['equivalent'])

    def test_missing_steps_and_tools_are_visible(self):
        for a, b in ((Workload(1, 1, 1), Workload(2, 1, 1)),
                     (Workload(1, 1, 1), Workload(1, 2, 1)),
                     (Workload(1, 1, 1), Workload(1, 1, 2))):
            self.assertFalse(self.equivalent(events(a), events(b))['equivalent'])

    def test_pending_failed_cancelled_and_checkpoint_order(self):
        source = events(Workload(1, 2, 2))
        self.equivalent(source[:3], source[:4], partial=True)
        for status in ('failed', 'cancelled'):
            changed = copy.deepcopy(source[:3])
            changed.append(dict(v=1, seq=3, run=changed[0]['run'], type='run_finished', status=status))
            self.equivalent(changed, changed)
        changed = copy.deepcopy(source)
        checkpoints = [e for e in changed if e['type'] == 'checkpoint']
        checkpoints[0]['digest'], checkpoints[1]['digest'] = checkpoints[1]['digest'], checkpoints[0]['digest']
        self.equivalent(source, changed)

    def test_same_step_checkpoint_order_and_multiplicity(self):
        def source(digests):
            rows = [dict(v=1, seq=0, run='R', type='run_started'),
                    dict(v=1, seq=1, run='R', type='step_started', actor='A', step='S')]
            rows.extend(dict(v=1, seq=0, run='R', type='checkpoint', actor='A', step='S', digest=d) for d in digests)
            rows.extend([dict(v=1, seq=0, run='R', type='step_finished', actor='A', step='S', status='ok'),
                         dict(v=1, seq=0, run='R', type='run_finished', status='completed')])
            for i, row in enumerate(rows):
                row['seq'] = i
            return rows
        a, b = 'a'*64, 'b'*64
        for before, after in (([a, b], [b, a]), ([a], [a, a]), ([], [a])):
            result = self.equivalent(source(before), source(after))
            self.assertFalse(result['equivalent'])
            self.assertGreaterEqual(result['first_difference']['record_index'], 2)
        self.assertTrue(self.equivalent(source([a, a]), source([a, a]))['equivalent'])

    def test_first_difference_is_detached_and_rest_is_consumed(self):
        a, b = events(Workload(1, 1, 1)), events(Workload(2, 1, 1))
        left, right = self.state(a), self.state(b)
        before = (left.report(), right.report())
        result = compare_histories(left, right)
        self.assertFalse(result['equivalent'])
        result['first_difference']['before']['usage']['input_tokens'] = -999
        self.assertEqual((left.report(), right.report()), before)
        self.assertGreater(result['compared_records']['right'], result['compared_records']['left'])
        self.assertFalse(left._reporting or right._reporting)

    def test_no_full_materializer_is_called_and_states_remain_reusable(self):
        source = events(Workload(2, 2, 2))
        left, right = self.state(source[:3]), self.state(source[:3])
        with patch.object(SQLiteReplayState, '_state', side_effect=AssertionError('materialization forbidden')), \
             patch.object(SQLiteReplayState, 'report', side_effect=AssertionError('materialization forbidden')):
            self.assertTrue(compare_histories(left, right, allow_partial=True)['equivalent'])
        left.extend(source[3:])
        right.extend(source[3:])
        self.assertTrue(compare_histories(left, right)['equivalent'])

    def test_invalid_empty_partial_closed_and_same_state_rejected(self):
        good = self.state(events(Workload(1, 1, 1)))
        for bad in (self.state([]), self.state(events(Workload(1))[:2])):
            with self.assertRaises(TraceError):
                compare_histories(good, bad)
            self.assertFalse(good._reporting)
            self.assertTrue(list(good.iter_report())[-1]['complete'])
        closed = self.state(events(Workload(1)))
        closed.close()
        with self.assertRaises(HistoryStorageError):
            compare_histories(good, closed)
        with self.assertRaises(TraceError):
            compare_histories(good, good)
        with self.assertRaises(TraceError):
            compare_histories(good, {})

    def test_late_read_and_cleanup_failure_after_mismatch_prevent_result(self):
        for side in ('left', 'right'):
            for sql_target in ('SELECT * FROM tools ORDER BY call', 'ROLLBACK'):
                with self.subTest(side=side, sql=sql_target):
                    left, right = self.state(events(Workload(1, 1, 1))), self.state(events(Workload(2, 1, 1)))
                    victim = left if side == 'left' else right
                    healthy = right if side == 'left' else left
                    real = victim._db
                    class FaultConnection:
                        def execute(self, sql, parameters=()):
                            if sql == sql_target:
                                raise sqlite3.OperationalError('private storage detail')
                            return real.execute(sql, parameters)
                        def close(self):
                            real.close()
                    victim._db = FaultConnection()
                    with self.assertRaises(HistoryStorageError):
                        compare_histories(left, right)
                    self.assertFalse(left._reporting or right._reporting)
                    self.assertTrue(list(healthy.iter_report())[-1]['complete'])
                    with self.assertRaises(HistoryStorageError):
                        victim.report()

    def test_existing_export_is_not_closed_by_rejected_comparison(self):
        left, right = self.state(events(Workload(1))), self.state(events(Workload(1)))
        stream = right.iter_report()
        next(stream)
        try:
            with self.assertRaises(TraceError):
                compare_histories(left, right)
            self.assertFalse(left._reporting)
            self.assertTrue(right._reporting)
            self.assertTrue(list(stream)[-1]['complete'])
        finally:
            stream.close()

    def test_incomplete_export_is_rejected_not_equal(self):
        left, right = self.state(events(Workload(1))), self.state(events(Workload(1)))
        original = right.iter_report
        def truncated(**kwargs):
            for record in original(**kwargs):
                if record.get('complete'):
                    return
                yield record
        with patch.object(right, 'iter_report', truncated):
            with self.assertRaises(TraceError):
                compare_histories(left, right)
        self.assertFalse(left._reporting or right._reporting)


if __name__ == '__main__':
    unittest.main()
