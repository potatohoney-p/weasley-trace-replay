import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from weasley_trace_replay import ReplayState, TraceError, compare, replay, resume, snapshot
from weasley_trace_replay.policy import compare_with_policy
from weasley_trace_replay.__main__ import main


def trace(observed=(), amount=0):
    records = [{"type": "run_started"}]
    for step in ("one", "two"):
        records.append(dict(type="step_started", actor="worker", step=step))
        if step in observed:
            records.append(dict(type="usage", actor="worker", step=step,
                                input_tokens=amount, output_tokens=0))
        records.append(dict(type="step_finished", actor="worker", step=step, status="ok"))
    records.append(dict(type="run_finished", status="completed"))
    return [dict(v=1, seq=i, run="demo", **record) for i, record in enumerate(records)]


class UsageObservationTests(unittest.TestCase):
    def test_default_stays_v1(self):
        result = replay(trace())
        self.assertEqual(result["version"], 1)
        self.assertNotIn("usage_observation", result)
        self.assertEqual(result, replay(trace(), report_version=1))

    def test_absent_and_zero_are_distinct_only_when_opted_in(self):
        self.assertTrue(compare(replay(trace()), replay(trace(["one"])))['equivalent'])
        a, b = (replay(t, report_version=2) for t in (trace(), trace(["one"])))
        self.assertEqual(a['usage'], b['usage'])
        self.assertFalse(compare(a, b)['equivalent'])
        self.assertEqual(b['usage_observation'], {
            'recorded_steps': ['one'], 'unrecorded_steps': ['two']})

    def test_partial_and_no_steps_not_claimed_complete(self):
        self.assertEqual(replay(trace()[:1], allow_partial=True, report_version=2)
                         ['usage_observation'], {'recorded_steps': [], 'unrecorded_steps': []})
        self.assertEqual(replay(trace()[:2], allow_partial=True, report_version=2)
                         ['usage_observation']['unrecorded_steps'], ['one'])

    def test_policy_cannot_tolerate_missing_observation(self):
        a = replay(trace(), report_version=2)
        b = replay(trace(['one'], amount=2), report_version=2)
        result = compare_with_policy(a, b, {'policy_version': 1,
            'usage_tolerances': {'input_tokens': 100}})
        self.assertFalse(result['equivalent'])
        self.assertEqual(len(result['tolerated_changes']), 1)
        self.assertTrue(all(c['path'].startswith('/usage_observation/') for c in result['changes']))

    def test_policy_still_tolerates_totals_with_same_presence(self):
        result = compare_with_policy(replay(trace(['one'], 1), report_version=2),
            replay(trace(['one'], 2), report_version=2),
            {'policy_version': 1, 'usage_tolerances': {'input_tokens': 1}})
        self.assertTrue(result['equivalent'])

    def test_old_snapshot_reconstructs_presence_at_every_boundary(self):
        data = trace(['one', 'two'])
        for n in range(1, len(data) + 1):
            with self.subTest(n=n):
                saved = snapshot(data[:n])
                self.assertNotIn('usage_observation', saved['state'])
                self.assertEqual(saved['snapshot_version'], 1)
                self.assertEqual(resume(saved, data[:n], data[n:], report_version=2),
                                 replay(data, report_version=2))

    def test_rejected_overflow_does_not_add_presence(self):
        data = trace(['one'], 2**53 - 1)
        state = ReplayState()
        state.extend(data[:5])  # first step ended, second started
        before = state.report(allow_partial=True, report_version=2)
        bad = dict(v=1, seq=5, run='demo', type='usage', actor='worker', step='two',
                   input_tokens=1, output_tokens=0)
        with self.assertRaises(TraceError):
            state.append(bad)
        self.assertEqual(before, state.report(allow_partial=True, report_version=2))

    def test_detached_report_cannot_change_presence(self):
        state = ReplayState(); state.extend(trace(['one']))
        report = state.report(report_version=2)
        report['usage_observation']['recorded_steps'].append('two')
        self.assertEqual(state.report(report_version=2)['usage_observation']['recorded_steps'], ['one'])

    def test_strict_versions_and_mixed_comparison(self):
        for version in (True, 0, 3, '2', None, 2.0):
            with self.subTest(version=version), self.assertRaises(TraceError):
                replay(trace(), report_version=version)
        with self.assertRaises(TraceError):
            compare(replay(trace()), replay(trace(), report_version=2))

    def test_invalid_observation_rejected(self):
        original = replay(trace(['one']), report_version=2)
        values = [None, {}, {'recorded_steps': [], 'unrecorded_steps': []},
                  {'recorded_steps': ['one', 'one'], 'unrecorded_steps': ['two']},
                  {'recorded_steps': ['two', 'one'], 'unrecorded_steps': []},
                  {'recorded_steps': ['one'], 'unrecorded_steps': ['one', 'two']},
                  {'recorded_steps': [True], 'unrecorded_steps': ['two']}]
        for value in values:
            with self.subTest(value=value):
                bad = copy.deepcopy(original); bad['usage_observation'] = value
                with self.assertRaises(TraceError): compare(bad, original)

    def test_v1_cannot_silently_ignore_presence(self):
        bad = replay(trace()); bad['usage_observation'] = {}
        with self.assertRaises(TraceError): compare(bad, replay(trace()))

    def test_duplicate_usage_records_are_presence_not_completeness(self):
        data = trace(['one']); data.insert(3, dict(data[2]))
        for i, event in enumerate(data): event['seq'] = i
        self.assertTrue(compare(replay(data, report_version=2),
                                replay(trace(['one']), report_version=2))['equivalent'])

    def test_cli_replay_diff_and_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def write(name, data):
                p = root / name
                p.write_text(''.join(json.dumps(e) + '\n' for e in data), encoding='utf-8')
                return str(p)
            a = write('a.jsonl', trace()); b = write('b.jsonl', trace(['one']))
            out = io.StringIO()
            with redirect_stdout(out): result = main(['diff', a, b, '--report-version', '2'])
            self.assertEqual(result, 1)
            self.assertFalse(json.loads(out.getvalue())['equivalent'])
            out = io.StringIO()
            with redirect_stdout(out): result = main(['replay', a, '--report-version', '2'])
            self.assertEqual(json.loads(out.getvalue())['version'], 2)
            data = trace(['one']); prefix = write('prefix', data[:3]); suffix = write('suffix', data[3:])
            saved = root / 'snapshot'; saved.write_text(json.dumps(snapshot(data[:3])), encoding='utf-8')
            out = io.StringIO()
            with redirect_stdout(out): result = main(['resume', str(saved), prefix, suffix, '--report-version', '2'])
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(out.getvalue()), replay(data, report_version=2))
