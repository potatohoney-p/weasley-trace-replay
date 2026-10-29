"""Synthetic lossless export, failure, ownership and bounded-query checks."""
import copy
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from demos.scaling import Workload, events, expected_report
from weasley_trace_replay import ReplayState, TraceError
from weasley_trace_replay.__main__ import main
from weasley_trace_replay.sqlite_history import SQLiteReplayState, HistoryStorageError

ROOT = Path(__file__).resolve().parents[1]


def materialize(records):
    """Test-only assembler: deliberately uses history-sized memory."""
    header, *body = records
    end = body.pop()
    if end.get('complete') is not True:
        raise ValueError('incomplete report')
    report = {key: header[key] for key in ('run', 'status', 'events', 'trace_sha256', 'usage')}
    report.update(version=header['report_version'], steps={}, tools={}, checkpoints={})
    counts = dict(steps=0, tools=0, checkpoints=0)
    for row in body:
        if row['kind'] == 'checkpoint':
            counts['checkpoints'] += 1
            report['checkpoints'].setdefault(row['step'], []).append(row['digest'])
        else:
            field = row['kind'] + 's'
            counts[field] += 1
            report[field][row['id']] = row['value']
    if counts != end['counts']:
        raise ValueError('record counts disagree')
    report['unfinished_steps'] = sorted(key for key, value in report['steps'].items() if value['status'] == 'running')
    report['unfinished_tools'] = sorted(key for key, value in report['tools'].items() if value['status'] == 'running')
    return report


class IncrementalReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = SQLiteReplayState(self.root / 'history.sqlite')
        self.addCleanup(self.state.close)

    def test_every_prefix_lossless_and_matches_independent_terminal_oracle(self):
        workload = Workload(2, 2, 2)
        source = events(workload)
        memory = ReplayState()
        for event in source:
            self.state.append(event)
            memory.append(event)
            actual = materialize(list(self.state.iter_report(allow_partial=True)))
            self.assertEqual(actual, memory.report(allow_partial=True))
        self.assertEqual(actual, expected_report(workload, source))

    def test_failed_cancelled_and_interrupted_pending_work_preserved(self):
        source = events(Workload(1, 1, 1))[:3]
        self.state.extend(source)
        for status in ('running', 'cancelled', 'failed'):
            with self.subTest(status=status):
                if status != 'running':
                    # Separate objects because terminal runs cannot continue.
                    state = SQLiteReplayState(self.root / f'{status}.sqlite')
                    self.addCleanup(state.close)
                    state.extend(source)
                    state.append(dict(v=1, seq=3, run=source[0]['run'], type='run_finished', status=status))
                else:
                    state = self.state
                self.assertEqual(materialize(list(state.iter_report(allow_partial=True))), state.report(allow_partial=True))

    def test_empty_and_incomplete_fail_before_header_and_unlock(self):
        with self.assertRaises(TraceError):
            next(self.state.iter_report())
        self.state.append(events(Workload(1))[0])
        with self.assertRaises(TraceError):
            next(self.state.iter_report())
        self.assertTrue(list(self.state.iter_report(allow_partial=True))[-1]['complete'])

    def test_live_export_blocks_append_and_second_export_until_close(self):
        source = events(Workload(1, 1, 1))
        self.state.append(source[0])
        before = self.state.snapshot()
        stream = self.state.iter_report(allow_partial=True)
        next(stream)
        with self.assertRaisesRegex(TraceError, 'cannot append'):
            self.state.append(source[1])
        with self.assertRaisesRegex(TraceError, 'already active'):
            next(self.state.iter_report(allow_partial=True))
        self.assertEqual(self.state.snapshot(), before)
        stream.close()
        self.state.extend(source[1:])
        self.assertEqual(materialize(list(self.state.iter_report())), self.state.report())

    def test_export_is_detached_and_does_not_materialize_history(self):
        self.state.extend(events(Workload(2, 2, 1)))
        expected = self.state.report()
        with patch.object(self.state, '_state', side_effect=AssertionError('full state forbidden')):
            rows = list(self.state.iter_report())
        rows[0]['usage']['input_tokens'] = -1
        for row in rows[1:-1]:
            if 'value' in row:
                row['value']['status'] = 'mutated'
        self.assertEqual(self.state.report(), expected)

    def test_checkpoint_rows_do_not_group_large_per_step_lists(self):
        source = events(Workload(1, 1, 1))[:2]
        self.state.extend(source)
        for index in range(200):
            self.state.append(dict(v=1, seq=index+2, run=source[0]['run'], type='checkpoint',
                                   actor=source[1]['actor'], step=source[1]['step'], digest=f'{index:064x}'))
        rows = list(self.state.iter_report(allow_partial=True))
        checkpoints = [row for row in rows if row.get('kind') == 'checkpoint']
        self.assertEqual(len(checkpoints), 200)
        self.assertTrue(all(set(row) == {'kind', 'step', 'digest'} for row in checkpoints))
        self.assertEqual(materialize(rows), self.state.report(allow_partial=True))

    def test_queries_have_no_temporary_sort(self):
        for query in ('SELECT * FROM steps ORDER BY step', 'SELECT * FROM tools ORDER BY call',
                      'SELECT step, digest FROM checkpoints ORDER BY step, seq'):
            plan = str(self.state._db.execute('EXPLAIN QUERY PLAN ' + query).fetchall())
            self.assertNotIn('TEMP B-TREE', plan)

    def test_read_or_cleanup_failure_has_no_completion_and_invalidates(self):
        for target in ('SELECT * FROM tools ORDER BY call', 'ROLLBACK'):
            state = SQLiteReplayState(self.root / ('fault-' + str(len(target)) + '.sqlite'))
            self.addCleanup(state.close)
            state.extend(events(Workload(1, 1, 1)))
            real = state._db
            class FaultConnection:
                def execute(self, sql, parameters=()):
                    if sql == target:
                        raise sqlite3.OperationalError('private fault detail')
                    return real.execute(sql, parameters)
                def close(self):
                    real.close()
            state._db = FaultConnection()
            observed = []
            with self.assertRaises(HistoryStorageError):
                observed.extend(state.iter_report())
            self.assertFalse(any(row.get('complete') for row in observed))
            with self.assertRaises(HistoryStorageError):
                state.report()

    def test_closing_state_during_export_never_yields_completion(self):
        self.state.extend(events(Workload(1, 1, 1)))
        stream = self.state.iter_report()
        next(stream)
        self.state.close()
        with self.assertRaises(HistoryStorageError):
            list(stream)

    def test_cli_jsonl_matches_default_report_and_default_unchanged(self):
        args = [sys.executable, '-m', 'weasley_trace_replay', 'replay', 'examples/completed.jsonl']
        ordinary = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
        streamed = subprocess.run(args + ['--report-format', 'jsonl'], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual((ordinary.returncode, streamed.returncode), (0, 0), streamed.stderr)
        self.assertEqual(streamed.stderr, '')
        self.assertEqual(materialize([json.loads(line) for line in streamed.stdout.splitlines()]), json.loads(ordinary.stdout))

    def test_cli_late_invalid_trace_emits_no_report(self):
        path = self.root / 'bad.jsonl'
        path.write_text((ROOT / 'examples/completed.jsonl').read_text() + '{}\n', encoding='utf-8')
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            result = main(['replay', str(path), '--report-format', 'jsonl'])
        self.assertEqual(result, 2)
        self.assertEqual(out.getvalue(), '')
        self.assertEqual(json.loads(err.getvalue()), {'error': 'invalid_or_unreadable_trace'})

    def test_cli_output_failure_closes_stream_and_temporary_directory(self):
        created = []
        original = tempfile.TemporaryDirectory
        def directory(*args, **kwargs):
            obj = original(*args, **kwargs)
            created.append(Path(obj.name))
            return obj
        class BrokenOutput(io.StringIO):
            def write(self, value):
                raise OSError('simulated output failure')
        with patch('weasley_trace_replay.__main__.tempfile.TemporaryDirectory', side_effect=directory), \
                redirect_stdout(BrokenOutput()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(['replay', 'examples/completed.jsonl', '--report-format', 'jsonl']), 2)
        self.assertTrue(created)
        self.assertTrue(all(not path.exists() for path in created))

    def test_cli_storage_close_failure_withholds_completion(self):
        original = SQLiteReplayState.close
        def fail_after_close(state):
            original(state)
            raise HistoryStorageError('injected close failure')
        out, err = io.StringIO(), io.StringIO()
        with patch.object(SQLiteReplayState, 'close', fail_after_close), redirect_stdout(out), redirect_stderr(err):
            result = main(['replay', 'examples/completed.jsonl', '--report-format', 'jsonl'])
        self.assertEqual(result, 2)
        self.assertFalse(any(json.loads(line).get('complete') for line in out.getvalue().splitlines()))
        self.assertEqual(json.loads(err.getvalue()), {'error': 'invalid_or_unreadable_trace'})

    def test_missing_completion_is_rejected_by_consumer(self):
        self.state.extend(events(Workload(1, 1, 1)))
        rows = list(self.state.iter_report())
        with self.assertRaises(ValueError):
            materialize(rows[:-1])
        changed = copy.deepcopy(rows)
        changed[-1]['counts']['steps'] += 1
        with self.assertRaises(ValueError):
            materialize(changed)
