"""Synthetic, export-only Python allocation probe; no provider or timing claim."""
import hashlib
import json
from pathlib import Path
import platform
import tempfile
import tracemalloc

from demos.scaling import Workload, events, expected_report
from weasley_trace_replay.sqlite_history import SQLiteReplayState


def measure(operation):
    tracemalloc.start()
    try:
        operation()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def main():
    if not __debug__:
        raise RuntimeError('run validation with assertions enabled')
    samples = []
    for rounds in (10, 100):
        workload = Workload(rounds, 2, 2)
        source = events(workload)
        with tempfile.TemporaryDirectory() as directory:
            with SQLiteReplayState(Path(directory) / 'history.sqlite') as state:
                state.extend(source)
                oracle = expected_report(workload, source)
                assert state.report() == oracle
                expected = dict(steps=rounds*2, tools=rounds*4, checkpoints=rounds*2)
                expected_rows = [dict(format='weasley-replay-report-jsonl', format_version=1,
                    report_version=1, **{key: oracle[key] for key in
                    ('run', 'status', 'events', 'trace_sha256', 'usage')})]
                for kind in ('step', 'tool'):
                    expected_rows.extend(dict(kind=kind, id=key, value=value)
                        for key, value in sorted(oracle[kind + 's'].items()))
                expected_rows.extend(dict(kind='checkpoint', step=step, digest=digest)
                    for step, digests in sorted(oracle['checkpoints'].items()) for digest in digests)
                expected_rows.append(dict(complete=True, counts=expected))
                expected_digest = hashlib.sha256(''.join(json.dumps(row, sort_keys=True) + '\n'
                    for row in expected_rows).encode()).hexdigest()
                def streamed():
                    digest = hashlib.sha256()
                    counts = dict(steps=0, tools=0, checkpoints=0)
                    complete = False
                    for row in state.iter_report():
                        # Serialize then discard each physical row, like CLI.
                        digest.update((json.dumps(row, sort_keys=True) + '\n').encode())
                        if 'kind' in row:
                            counts[row['kind'] + 's'] += 1
                        if row.get('complete'):
                            assert row['counts'] == expected == counts
                            complete = True
                    assert complete
                    assert digest.hexdigest() == expected_digest
                def materialized():
                    json.dumps(state.report(), sort_keys=True)
                # First traversal warms SQLite and imports before measurement.
                streamed()
                materialized()
                samples.append(dict(events=len(source), rounds=rounds,
                    full_json_peak_bytes=[measure(materialized) for _ in range(3)],
                    incremental_jsonl_peak_bytes=[measure(streamed) for _ in range(3)]))
    root = Path(__file__).resolve().parents[1]
    paths = ['weasley_trace_replay/sqlite_history.py', 'demos/incremental_export.py']
    print(json.dumps(dict(scope='Synthetic export-only traced Python allocation; not RSS/native memory or timing.',
        exclusions='Input construction, database ingestion, expected-report oracle and warm-up occur before measurement.',
        python=platform.python_version(), platform=platform.platform(),
        source_sha256={p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p in paths},
        samples=samples), indent=2))


if __name__ == '__main__':
    main()
