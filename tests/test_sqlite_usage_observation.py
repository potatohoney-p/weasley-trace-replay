"""Synthetic independent presence oracles and v2 disk-report parity."""
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from test_incremental_report import materialize
from test_usage_observation import trace
from weasley_trace_replay import ReplayState, TraceError, replay, snapshot
from weasley_trace_replay.__main__ import main
from weasley_trace_replay.sqlite_history import SQLiteReplayState, HistoryStorageError


def assemble_v2(rows):
    assert rows[0]['format_version'] == 2
    assert rows[0]['report_version'] == 2
    result = materialize(rows)
    result['usage_observation'] = {
        'recorded_steps': [r['id'] for r in rows if r.get('kind') == 'step' and r['usage_recorded']],
        'unrecorded_steps': [r['id'] for r in rows if r.get('kind') == 'step' and not r['usage_recorded']],
    }
    return result


class SQLiteUsageObservationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.index = 0

    def state(self):
        self.index += 1
        obj = SQLiteReplayState(self.root / f'{self.index}.sqlite')
        self.addCleanup(obj.close)
        return obj

    def test_every_prefix_against_independent_presence_and_memory_oracle(self):
        for observed in ((), ('one',), ('two',), ('one', 'two')):
            for amount in (0, 7):
                state = self.state()
                data = trace(observed, amount)
                for n, event in enumerate(data, 1):
                    state.append(event)
                    recorded = sorted({e['step'] for e in data[:n] if e['type'] == 'usage'})
                    started = {e['step'] for e in data[:n] if e['type'] == 'step_started'}
                    actual = state.report(allow_partial=True, report_version=2)
                    self.assertEqual(actual['usage_observation'], dict(recorded_steps=recorded,
                        unrecorded_steps=sorted(started - set(recorded))))
                    self.assertEqual(actual, replay(data[:n], allow_partial=True, report_version=2))
                    self.assertEqual(actual, assemble_v2(list(state.iter_report(allow_partial=True, report_version=2))))
                    self.assertEqual(state.report(allow_partial=True), replay(data[:n], allow_partial=True))

    def test_snapshot_preserves_v1_and_reconstructs_presence(self):
        data = trace(('one', 'two'))
        for n in range(1, len(data)+1):
            saved = snapshot(data[:n])
            with SQLiteReplayState.from_snapshot(saved, data[:n], self.root / f'resume{n}.sqlite') as state:
                self.assertEqual(state.snapshot(), saved)
                state.extend(data[n:])
                self.assertEqual(state.report(report_version=2), replay(data, report_version=2))

    def test_rejected_overflow_cannot_mark_usage_or_change_hash(self):
        state = self.state(); state.extend(trace(('one',), 2**53-1)[:5])
        before = state.report(allow_partial=True, report_version=2)
        with self.assertRaises(TraceError):
            state.append(dict(v=1, seq=5, run='demo', type='usage', actor='worker', step='two',
                              input_tokens=1, output_tokens=0))
        self.assertEqual(state.report(allow_partial=True, report_version=2), before)

    def test_duplicate_zero_usage_only_marks_presence_once(self):
        data = trace(('one',)); data.insert(3, dict(data[2]))
        for i, event in enumerate(data): event['seq'] = i
        state = self.state(); state.extend(data)
        self.assertEqual(state.report(report_version=2)['usage_observation'],
                         dict(recorded_steps=['one'], unrecorded_steps=['two']))
        self.assertEqual(state._one('SELECT COUNT(*) FROM usage_steps')[0], 1)

    def test_versions_rejected_before_output_and_do_not_lock_export(self):
        state = self.state(); state.extend(trace())
        for version in (True, False, 0, 3, '2', None, 2.0):
            with self.assertRaises(TraceError): state.report(report_version=version)
            with self.assertRaises(TraceError): next(state.iter_report(report_version=version))
        self.assertTrue(list(state.iter_report(report_version=2))[-1]['complete'])

    def test_stream_never_materializes_and_early_close_unlocks(self):
        state = self.state(); state.extend(trace(('one',))[:3])
        with patch.object(state, '_state', side_effect=AssertionError('materialized')):
            rows = state.iter_report(allow_partial=True, report_version=2)
            next(rows)
            with self.assertRaises(TraceError): state.append(trace(('one',))[3])
            rows.close()
            state.extend(trace(('one',))[3:])
            result = assemble_v2(list(state.iter_report(report_version=2)))
        self.assertEqual(result, replay(trace(('one',)), report_version=2))

    def test_read_and_cleanup_faults_never_complete(self):
        for target in ('LEFT JOIN usage_steps', 'ROLLBACK'):
            state = self.state(); state.extend(trace(('one',)))
            real = state._db
            class Fault:
                def execute(self, sql, args=()):
                    if target in sql: raise sqlite3.OperationalError('injected')
                    return real.execute(sql, args)
                def close(self): real.close()
            state._db = Fault()
            rows = []
            with self.assertRaises(HistoryStorageError): rows.extend(state.iter_report(report_version=2))
            self.assertFalse(any(r.get('complete') for r in rows))
            with self.assertRaises(HistoryStorageError): state.report(report_version=2)

    def test_indexed_presence_join_does_not_sort_in_temp(self):
        state = self.state()
        query = ('SELECT s.step, s.actor, s.status, u.step IS NOT NULL FROM steps s '
                 'LEFT JOIN usage_steps u ON u.step = s.step ORDER BY s.step')
        self.assertNotIn('TEMP B-TREE', str(state._db.execute('EXPLAIN QUERY PLAN '+query).fetchall()))

    def test_cli_version_two_stream_matches_materialized_report(self):
        data = trace(('one',))
        path = self.root / 'input.jsonl'
        path.write_text(''.join(json.dumps(e)+'\n' for e in data))
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(['replay', str(path), '--report-format', 'jsonl', '--report-version', '2'])
        self.assertEqual(code, 0)
        self.assertEqual(assemble_v2([json.loads(line) for line in out.getvalue().splitlines()]),
                         replay(data, report_version=2))
