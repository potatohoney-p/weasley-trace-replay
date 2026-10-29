import copy
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from demos.causal_campaign import SEEDS, _number, graph, oracle, records as causal_records, schedule
from demos.scaling import Workload, events, expected_report, verify_report
from weasley_trace_replay import ReplayState, TraceError
from weasley_trace_replay.sqlite_history import SQLiteReplayState, HistoryStorageError


class SQLiteHistoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.serial = 0

    def path(self):
        self.serial += 1
        return self.root / f"history-{self.serial}.sqlite"

    def test_every_prefix_matches_memory_and_final_independent_oracle(self):
        workload = Workload(3, 3, 2)
        records = events(workload)
        memory = ReplayState()
        with SQLiteReplayState(self.path()) as disk:
            for record in records:
                memory.append(record)
                disk.append(record)
                self.assertEqual(disk.snapshot(), memory.snapshot())
                verify_report(disk.report(allow_partial=True), memory.report(allow_partial=True))
            verify_report(disk.report(), expected_report(workload, records))

    def test_causal_schedules_match_independent_prefix_oracle(self):
        for index, seed in enumerate(SEEDS):
            nodes = graph(seed, 3 + index)
            for schedule_index in range(4):
                order = schedule(nodes, _number(seed, schedule_index))
                memory = ReplayState()
                with SQLiteReplayState(self.path()) as disk:
                    for stop, record in enumerate(causal_records(order), 1):
                        disk.append(record)
                        memory.append(record)
                        report = disk.report(allow_partial=True)
                        expected = oracle(order[:stop])
                        self.assertEqual({key: report[key] for key in expected}, expected)
                        verify_report(report, memory.report(allow_partial=True))

    def test_finished_ids_stay_reserved_after_verified_reconstruction(self):
        records = events(Workload(1, 1, 1))[:-1]
        memory = ReplayState()
        memory.extend(records)
        with SQLiteReplayState.from_snapshot(memory.snapshot(), records, self.path()) as disk:
            duplicate = dict(records[1], seq=len(records))
            with self.assertRaisesRegex(TraceError, "step identifier reused"):
                disk.append(duplicate)
            disk.append(dict(duplicate, step="new"))
            before = disk.snapshot()
            with self.assertRaisesRegex(TraceError, "call identifier reused"):
                disk.append(dict(records[2], seq=len(records) + 1, step="new"))
            self.assertEqual(disk.snapshot(), before)
            disk.append(dict(records[-1], seq=len(records) + 1, step="new"))
            self.assertEqual(len(disk.snapshot()["state"]["tools"]), 1)

    def test_verified_reconstruction_at_pending_historical_and_terminal_boundaries(self):
        workload = Workload(2, 2, 2)
        records = events(workload)
        memory = ReplayState()
        for boundary, record in enumerate(records, 1):
            memory.append(record)
            with SQLiteReplayState.from_snapshot(memory.snapshot(), iter(records[:boundary]), self.path()) as restored:
                restored.extend(iter(records[boundary:]))
                verify_report(restored.report(), expected_report(workload, records))

    def test_reconstruction_rejects_tampered_state_hash_count_and_prefix(self):
        records = events(Workload(2, 2, 1))
        memory = ReplayState()
        memory.extend(records[:10])
        saved = memory.snapshot()
        variants = []
        for field, value in (("prefix_sha256", "0" * 64), ("prefix_events", 9)):
            variants.append(dict(saved, **{field: value}))
        changed = copy.deepcopy(saved)
        changed["state"]["tools"][next(iter(changed["state"]["tools"]))]["tool"] = "changed"
        variants.append(changed)
        for invalid in variants:
            with self.subTest(invalid=invalid), self.assertRaises(TraceError):
                SQLiteReplayState.from_snapshot(invalid, iter(records[:10]), self.path())
        with self.assertRaises(TraceError):
            SQLiteReplayState.from_snapshot(saved, iter(records[:9]), self.path())

    def test_invalid_lifecycle_events_are_atomic_and_do_not_prevent_valid_suffix(self):
        records = events(Workload(2, 2, 2))
        memory = ReplayState()
        with SQLiteReplayState(self.path()) as disk:
            for record in records:
                # Schema/sequence, run identity, actor/step ownership, historical
                # IDs, duplicate results and pending/completed checks at each boundary.
                candidates = [dict(record, seq=True), dict(record, run="other")]
                for kind in ("run_started", "run_finished", "step_started", "step_finished",
                             "tool_started", "tool_finished", "checkpoint", "usage"):
                    candidate = next((e for e in records if e["type"] == kind), None)
                    candidates.append(dict(candidate, seq=record["seq"]))
                for candidate in candidates:
                    oracle = ReplayState()
                    oracle.extend(records[:record["seq"]])
                    try:
                        oracle.append(candidate)
                    except TraceError as expected:
                        before = disk.snapshot() if record["seq"] else None
                        with self.assertRaises(TraceError) as actual:
                            disk.append(candidate)
                        self.assertEqual(str(actual.exception), str(expected))
                        if before is not None:
                            self.assertEqual(before, disk.snapshot())
                memory.append(record)
                disk.append(record)
                self.assertEqual(disk.snapshot(), memory.snapshot())

    def test_usage_overflow_rolls_back_both_fields_and_hash(self):
        records = events(Workload(1, 1, 1))
        with SQLiteReplayState(self.path()) as disk:
            disk.extend(records[:3])
            usage = dict(records[4], seq=3, input_tokens=0, output_tokens=2**53 - 1)
            disk.append(usage)
            before = disk.snapshot()
            with self.assertRaisesRegex(TraceError, "aggregate usage"):
                disk.append(dict(usage, seq=4, input_tokens=7, output_tokens=1))
            self.assertEqual(disk.snapshot(), before)
            disk.append(dict(usage, seq=4, input_tokens=7, output_tokens=0))
            self.assertEqual(disk.snapshot()["state"]["usage"]["input_tokens"], 7)

    def test_checkpoint_order_case_sensitive_ids_and_detached_results(self):
        records = [dict(v=1, seq=0, run="run", type="run_started")]
        for step in ("Step", "step"):
            for kind, fields in (("step_started", {}), ("checkpoint", {"digest": "b" * 64}),
                                 ("checkpoint", {"digest": "a" * 64}),
                                 ("step_finished", {"status": "ok"})):
                records.append(dict(v=1, seq=len(records), run="run", type=kind, actor="actor", step=step, **fields))
        with SQLiteReplayState(self.path()) as disk:
            disk.extend(records)
            saved = disk.snapshot()
            report = disk.report(allow_partial=True)
            self.assertEqual(report["checkpoints"]["Step"], ["b" * 64, "a" * 64])
            report["checkpoints"]["Step"].clear()
            saved["state"]["steps"].clear()
            self.assertEqual(len(disk.snapshot()["state"]["steps"]), 2)
            self.assertEqual(len(disk.report(allow_partial=True)["checkpoints"]["Step"]), 2)

    def test_empty_partial_terminal_and_extend_failure_contract(self):
        for terminal in ("failed", "cancelled"):
            with SQLiteReplayState(self.path()) as disk:
                with self.assertRaisesRegex(TraceError, "empty"):
                    disk.snapshot()
                with self.assertRaisesRegex(TraceError, "empty"):
                    disk.report(allow_partial=True)
                records = events(Workload(1, 1, 1))[:3]
                with self.assertRaises(TraceError):
                    disk.extend(records + [dict(records[-1], seq=3)])
                with self.assertRaisesRegex(TraceError, "incomplete"):
                    disk.report()
                disk.append(dict(v=1, seq=3, run=records[0]["run"], type="run_finished", status=terminal))
                self.assertEqual(len(disk.report()["unfinished_tools"]), 1)
                with self.assertRaisesRegex(TraceError, "terminal"):
                    disk.append(dict(records[0], seq=4))

    def test_existing_file_is_never_opened_or_overwritten_and_close_is_final(self):
        path = self.path()
        path.write_bytes(b"preserve")
        with self.assertRaises(FileExistsError):
            SQLiteReplayState(path)
        self.assertEqual(path.read_bytes(), b"preserve")
        disk = SQLiteReplayState(self.path())
        disk.close()
        disk.close()
        for operation in (lambda: disk.append(events(Workload(1))[0]), disk.report, disk.snapshot):
            with self.assertRaises(HistoryStorageError):
                operation()

    def test_relative_paths_use_real_database_files_including_sqlite_special_name(self):
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            names = ["relative.sqlite"]
            if os.name != "nt":  # A colon is not a legal Windows filename.
                names.append(":memory:")
            for name in names:
                with SQLiteReplayState(name) as disk:
                    disk.append(events(Workload(1))[0])
                    self.assertEqual(disk.report(allow_partial=True)["events"], 1)
                self.assertTrue(Path(name).read_bytes().startswith(b"SQLite format 3\x00"))
        finally:
            os.chdir(previous)

    def test_storage_faults_fail_stop_then_verified_prefix_rebuild(self):
        records = events(Workload(1, 1, 1))
        # Fault before commit, after a successful but unacknowledged commit, and
        # rollback failure after a contract rejection all invalidate the handle.
        for fault in ("write", "commit", "rollback"):
            with self.subTest(fault=fault), SQLiteReplayState(self.path()) as disk:
                disk.extend(records[:3])
                saved = disk.snapshot()
                real = disk._db

                class FaultConnection:
                    def execute(self, sql, parameters=()):
                        if fault == "write" and sql.startswith("INSERT INTO checkpoints"):
                            real.execute(sql, parameters)
                            raise sqlite3.OperationalError("injected after write")
                        if fault == "commit" and sql == "COMMIT":
                            real.execute(sql, parameters)
                            raise sqlite3.OperationalError("injected lost acknowledgement")
                        if fault == "rollback" and sql == "ROLLBACK":
                            raise sqlite3.OperationalError("injected rollback failure")
                        return real.execute(sql, parameters)

                    def close(self):
                        real.close()

                disk._db = FaultConnection()
                record = dict(records[1], seq=3) if fault == "rollback" else records[3]
                with self.assertRaises(HistoryStorageError):
                    disk.append(record)
                for operation in (disk.snapshot, disk.report, lambda: disk.append(records[3])):
                    with self.assertRaises(HistoryStorageError):
                        operation()
                with SQLiteReplayState.from_snapshot(saved, records[:3], self.path()) as rebuilt:
                    rebuilt.extend(records[3:])
                    verify_report(rebuilt.report(), expected_report(Workload(1, 1, 1), records))

    def test_read_error_invalidates_handle(self):
        with SQLiteReplayState(self.path()) as disk:
            disk.append(events(Workload(1))[0])
            disk._db.set_authorizer(lambda *args: sqlite3.SQLITE_DENY)
            with self.assertRaises(HistoryStorageError):
                disk.report(allow_partial=True)
            with self.assertRaises(HistoryStorageError):
                disk.snapshot()


if __name__ == "__main__":
    unittest.main()
