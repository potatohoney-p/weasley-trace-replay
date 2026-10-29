import copy
import json
from pathlib import Path
import tempfile
import unittest

from demos.scaling import Workload, events
from weasley_trace_replay import ReplayState, TraceError, materialize_report, read_report
from weasley_trace_replay.sqlite_history import SQLiteReplayState


class ReportReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = SQLiteReplayState(self.root / 'history.sqlite')
        self.addCleanup(self.state.close)
        self.source = events(Workload(2, 2, 2))

    def records(self):
        self.state.extend(self.source)
        return list(self.state.iter_report())

    def test_all_prefixes_roundtrip_with_independent_memory_replay(self):
        memory = ReplayState()
        for event in self.source:
            self.state.append(event)
            memory.append(event)
            records = self.state.iter_report(allow_partial=True)
            self.assertEqual(materialize_report(records), memory.report(allow_partial=True))

    def test_detached_and_single_use_source(self):
        records = self.records()
        expected = copy.deepcopy(records)
        report = materialize_report(iter(records))
        records[0]['usage']['input_tokens'] = 99
        self.assertNotEqual(report['usage'], records[0]['usage'])
        report['steps'][next(iter(report['steps']))]['actor'] = 'changed'
        self.assertEqual(records[1:], expected[1:])

    def test_missing_terminal_extra_terminal_trailing_and_empty_fail(self):
        records = self.records()
        for invalid in ([], records[:-1], records + [records[-1]], records + [{}], records[:1]):
            with self.assertRaises(TraceError): materialize_report(invalid)

    def test_versions_counts_shapes_and_nonobjects_fail(self):
        records = self.records()
        mutations = [(0, 'format_version', True), (0, 'report_version', 2),
                     (0, 'events', False), (0, 'run', 'bad run'),
                     (0, 'trace_sha256', 'bad'), (0, 'usage', {'input_tokens': True, 'output_tokens': 0}),
                     (-1, 'complete', 1), (-1, 'counts', {'steps': True, 'tools': 0, 'checkpoints': 0}),
                     (1, 'extra', 1), (1, 'value', []), (1, 'kind', [])]
        for index, key, value in mutations:
            bad = copy.deepcopy(records); bad[index][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(TraceError): materialize_report(bad)
        for bad in (None, [], 1, 'text'):
            with self.assertRaises(TraceError): materialize_report([bad])

    def test_reordered_duplicate_ids_and_sections_fail(self):
        records = self.records()
        steps = [i for i, row in enumerate(records) if row.get('kind') == 'step']
        tools = [i for i, row in enumerate(records) if row.get('kind') == 'tool']
        for a, b in ((steps[0], steps[1]), (steps[0], tools[0])):
            bad = copy.deepcopy(records); bad[a], bad[b] = bad[b], bad[a]
            with self.assertRaises(TraceError): materialize_report(bad)
        bad = copy.deepcopy(records); bad[steps[1]]['id'] = bad[steps[0]]['id']
        with self.assertRaises(TraceError): materialize_report(bad)

    def test_ownership_and_state_consistency_fail(self):
        records = self.records()
        tool = next(i for i,r in enumerate(records) if r.get('kind') == 'tool')
        checkpoint = next(i for i,r in enumerate(records) if r.get('kind') == 'checkpoint')
        for key, value in (('step', 'missing'), ('actor', 'another'), ('status', 'running')):
            bad = copy.deepcopy(records); bad[tool]['value'][key] = value
            with self.assertRaises(TraceError): materialize_report(bad)
        bad = copy.deepcopy(records); bad[checkpoint]['step'] = 'missing'
        with self.assertRaises(TraceError): materialize_report(bad)
        bad = copy.deepcopy(records); bad[1]['value']['status'] = 'error'
        with self.assertRaises(TraceError): materialize_report(bad)

    def test_failed_cancelled_and_running_reports_allowed(self):
        prefix = self.source[:3]
        for status in ('running', 'failed', 'cancelled'):
            with SQLiteReplayState(self.root / (status + '.sqlite')) as state:
                state.extend(prefix)
                if status != 'running':
                    state.append(dict(v=1, seq=3, run=prefix[0]['run'], type='run_finished', status=status))
                self.assertEqual(materialize_report(state.iter_report(allow_partial=True)),
                                 state.report(allow_partial=True))

    def test_limits_and_source_failure_do_not_return_partial_result(self):
        records = self.records()
        self.assertEqual(materialize_report(records, max_records=len(records))['version'], 1)
        for limit in (True, 1, 0, len(records)-1):
            with self.assertRaises(TraceError): materialize_report(records, max_records=limit)
        def broken():
            yield from records
            raise OSError('source failed after terminal')
        with self.assertRaises(OSError): materialize_report(broken())

    def test_file_json_limits_duplicate_keys_and_torn_tail(self):
        records = self.records()
        path = self.root / 'report.jsonl'
        path.write_text(''.join(json.dumps(r)+'\n' for r in records), encoding='utf-8')
        self.assertEqual(read_report(path), materialize_report(records))
        with self.assertRaises(TraceError): read_report(path, max_line_bytes=10)
        with self.assertRaises(TraceError): read_report(path, max_records=2)
        for content in ('{"format":1,"format":2}\n', 'NaN\n', '\n', '{"complete":'):
            path.write_text(content)
            with self.assertRaises(TraceError): read_report(path)


if __name__ == '__main__': unittest.main()
