"""Opt-in exact disk history, materialized snapshots and incremental reports."""

from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import closing

from .replay import TraceError, _canonical, _require, _validate, _validate_snapshot, _report_version


class HistoryStorageError(RuntimeError):
    """History is closed or uncertain; rebuild from a verified prefix in a new file."""


class SQLiteReplayState:
    """Replay without growing Python history maps during append.

    Owns a NEW database exclusively for this object's lifetime. Never reopens a
    persisted database, trusts its contents as a snapshot, or executes tools.
    Each event uses a transaction; invalid events roll back. Any storage error
    invalidates the object, including an unacknowledged commit. Full reports and
    snapshots use O(history) Python memory; iter_report streams observations.
    Not thread-safe or process recovery.
    """

    def __init__(self, path: str | Path) -> None:
        self._db = None
        self._closed = True
        self._digest = hashlib.sha256()
        self._count = 0
        self._reporting = False
        # Absolute filenames cannot be interpreted as SQLite's ':memory:' name.
        # Do not resolve symlinks: exclusive creation must reject the link itself.
        path = Path(path).absolute()
        # Refuse existing files, including existing databases and symlinks.
        # Creation errors may leave a new empty/partial file for caller inspection.
        with path.open("xb"):
            pass
        try:
            self._db = sqlite3.connect(path, isolation_level=None)
            self._db.execute("PRAGMA journal_mode=DELETE")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA cache_size=-2048")
            self._db.execute("PRAGMA mmap_size=0")
            self._db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE meta (run TEXT, status TEXT NOT NULL,
                                   input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL);
                INSERT INTO meta VALUES (NULL, 'running', 0, 0);
                CREATE TABLE steps (step TEXT PRIMARY KEY, actor TEXT NOT NULL, status TEXT NOT NULL);
                CREATE INDEX steps_active ON steps(actor) WHERE status = 'running';
                CREATE INDEX steps_status ON steps(status);
                CREATE TABLE usage_steps (step TEXT PRIMARY KEY);
                CREATE TABLE tools (call TEXT PRIMARY KEY, actor TEXT NOT NULL, step TEXT NOT NULL,
                                    tool TEXT NOT NULL, status TEXT NOT NULL, digest TEXT);
                CREATE INDEX tools_pending ON tools(step) WHERE status = 'running';
                CREATE TABLE checkpoints (seq INTEGER PRIMARY KEY, step TEXT NOT NULL, digest TEXT NOT NULL);
                CREATE INDEX checkpoints_step_order ON checkpoints(step, seq);
                COMMIT;
            """)
            self._closed = False
        except sqlite3.Error as exc:
            self.close()
            raise HistoryStorageError("cannot initialize replay history") from exc
        except BaseException:
            self.close()
            raise

    def _ensure_open(self) -> None:
        if self._closed:
            raise HistoryStorageError("history unavailable; rebuild into a new database")

    def close(self) -> None:
        """Release the connection; keep the database file, which cannot be resumed."""
        self._closed = True
        db, self._db = self._db, None
        if db is not None:
            try:
                db.close()
            except sqlite3.Error as exc:
                raise HistoryStorageError("cannot close replay history") from exc

    def __enter__(self) -> SQLiteReplayState:
        self._ensure_open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _invalidate(self) -> None:
        try:
            self.close()
        except HistoryStorageError:
            pass

    def append(self, event: dict) -> None:
        """Append atomically; a storage failure makes all subsequent reads fail."""
        self._ensure_open()
        _require(not self._reporting, "cannot append during incremental report; consume or close it first")
        try:
            _validate(event, self._count)
            digest = self._digest.copy()
            digest.update(_canonical(event) + b"\n")
            count = self._count + 1
        except TraceError as exc:
            raise TraceError(f"event {self._count}: {exc}") from exc
        try:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._transition(event)
            except TraceError:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")
            self._digest, self._count = digest, count
        except TraceError as exc:
            raise TraceError(f"event {self._count}: {exc}") from exc
        except sqlite3.Error as exc:
            # Do not infer whether a failed COMMIT changed disk. The Python hash
            # and SQLite transaction are separate resources, so fail closed.
            self._invalidate()
            raise HistoryStorageError("history append failed; rebuild from verified prefix") from exc
        except BaseException:
            self._invalidate()
            raise

    def extend(self, events: Iterable[dict]) -> None:
        """Retain valid earlier events if a later event is rejected."""
        self._ensure_open()
        for event in events:
            self.append(event)

    def _one(self, sql: str, parameters=()):
        return self._db.execute(sql, parameters).fetchone()

    def _transition(self, event: dict) -> None:
        kind = event["type"]
        run, status, inputs, outputs = self._one("SELECT * FROM meta")
        _require(status == "running", "events after terminal run")
        if run is None:
            _require(kind == "run_started", "first event must start a run")
            self._db.execute("UPDATE meta SET run = ?", (event["run"],))
        else:
            _require(kind != "run_started", "duplicate run start")
            _require(event["run"] == run, "mixed run identifiers")
        if kind == "step_started":
            actor, step = event["actor"], event["step"]
            _require(self._one("SELECT step FROM steps WHERE actor = ? AND status = 'running'", (actor,)) is None,
                     "actor already has an active step")
            _require(self._one("SELECT step FROM steps WHERE step = ?", (step,)) is None, "step identifier reused")
            self._db.execute("INSERT INTO steps VALUES (?, ?, 'running')", (step, actor))
        elif kind not in {"run_started", "run_finished"}:
            actor, step = event["actor"], event["step"]
            active = self._one("SELECT step FROM steps WHERE actor = ? AND status = 'running'", (actor,))
            _require(active is not None and active[0] == step, "event has no matching active step")
            if kind == "tool_started":
                _require(self._one("SELECT call FROM tools WHERE call = ?", (event["call"],)) is None,
                         "call identifier reused")
                self._db.execute("INSERT INTO tools VALUES (?, ?, ?, ?, 'running', NULL)",
                                 (event["call"], actor, step, event["tool"]))
            elif kind == "tool_finished":
                call = self._one("SELECT actor, step, status FROM tools WHERE call = ?", (event["call"],))
                _require(call is not None and call[:2] == (actor, step), "tool result has no matching call")
                _require(call[2] == "running", "tool result already recorded")
                self._db.execute("UPDATE tools SET status = ?, digest = ? WHERE call = ?",
                                 (event["status"], event["digest"], event["call"]))
            elif kind == "checkpoint":
                self._db.execute("INSERT INTO checkpoints VALUES (?, ?, ?)", (event["seq"], step, event["digest"]))
            elif kind == "usage":
                inputs += event["input_tokens"]
                outputs += event["output_tokens"]
                _require(max(inputs, outputs) <= 2**53 - 1, "aggregate usage exceeds exact integer range")
                self._db.execute("UPDATE meta SET input_tokens = ?, output_tokens = ?", (inputs, outputs))
                self._db.execute("INSERT OR IGNORE INTO usage_steps VALUES (?)", (step,))
            elif kind == "step_finished":
                _require(self._one("SELECT 1 FROM tools WHERE step = ? AND status = 'running' LIMIT 1", (step,)) is None,
                         "step has unfinished tools")
                self._db.execute("UPDATE steps SET status = ? WHERE step = ?", (event["status"], step))
        elif kind == "run_finished":
            self._db.execute("UPDATE meta SET status = ?", (event["status"],))
            if event["status"] == "completed":
                _require(self._one("SELECT 1 FROM steps WHERE status = 'running' LIMIT 1") is None,
                         "completed run has unfinished steps")
                _require(self._one("SELECT 1 FROM steps WHERE status != 'ok' LIMIT 1") is None,
                         "completed run has unsuccessful steps")

    def _state(self) -> dict:
        self._ensure_open()
        try:
            run, status, inputs, outputs = self._one("SELECT * FROM meta")
            _require(run is not None, "trace is empty")
            steps = {step: dict(actor=actor, status=status)
                     for step, actor, status in self._db.execute("SELECT * FROM steps ORDER BY step")}
            tools = {}
            for call, actor, step, tool, call_status, digest in self._db.execute("SELECT * FROM tools ORDER BY call"):
                tools[call] = dict(actor=actor, step=step, tool=tool, status=call_status)
                if digest is not None:
                    tools[call]["digest"] = digest
            checkpoints = {}
            for step, digest in self._db.execute("SELECT step, digest FROM checkpoints ORDER BY seq"):
                checkpoints.setdefault(step, []).append(digest)
            return dict(run=run, status=status, steps=steps, tools=tools,
                        active={entry["actor"]: step for step, entry in steps.items() if entry["status"] == "running"},
                        checkpoints=dict(sorted(checkpoints.items())),
                        usage=dict(input_tokens=inputs, output_tokens=outputs))
        except sqlite3.Error as exc:
            self._invalidate()
            raise HistoryStorageError("history read failed; rebuild from verified prefix") from exc

    def report(self, *, allow_partial: bool = False, report_version: int = 1) -> dict:
        """Materialize a detached v1/v2 report, using O(history) memory."""
        _report_version(report_version)
        state = self._state()
        _require(allow_partial or state["status"] != "running",
                 "trace is incomplete; use allow_partial for an interrupted run")
        result = dict(version=report_version, run=state["run"], status=state["status"], events=self._count,
                    trace_sha256=self._digest.hexdigest(), steps=state["steps"], tools=state["tools"],
                    checkpoints=state["checkpoints"], usage=state["usage"],
                    unfinished_steps=sorted(state["active"].values()),
                    unfinished_tools=sorted(call for call, entry in state["tools"].items() if entry["status"] == "running"))

        if report_version == 2:
            try:
                recorded = {row[0] for row in self._db.execute("SELECT step FROM usage_steps ORDER BY step")}
            except sqlite3.Error as exc:
                self._invalidate()
                raise HistoryStorageError("history usage read failed; rebuild from verified prefix") from exc
            result["usage_observation"] = dict(recorded_steps=sorted(recorded),
                                               unrecorded_steps=sorted(state["steps"].keys() - recorded))
        return result

    def iter_report(self, *, allow_partial: bool = False, report_version: int = 1) -> Iterator[dict]:
        """Stream detached v1/v2 observations without materializing history maps.

        A single read transaction pins the export. Consume or close the iterator
        before appending or starting another export. The final complete marker
        is required; earlier records alone are not a complete report. No snapshot
        is exported and no existing database is reopened or trusted.
        """
        _report_version(report_version)
        self._ensure_open()
        _require(not self._reporting, "an incremental report is already active")
        self._reporting = True
        transaction = False
        counts = dict(steps=0, tools=0, checkpoints=0)
        try:
            self._db.execute("BEGIN")
            transaction = True
            run, status, inputs, outputs = self._one("SELECT * FROM meta")
            _require(run is not None, "trace is empty")
            _require(allow_partial or status != "running",
                     "trace is incomplete; use allow_partial for an interrupted run")
            yield dict(format="weasley-replay-report-jsonl", format_version=report_version,
                       report_version=report_version, run=run, status=status, events=self._count,
                       trace_sha256=self._digest.hexdigest(),
                       usage=dict(input_tokens=inputs, output_tokens=outputs))
            self._ensure_open()
            query = ("SELECT * FROM steps ORDER BY step" if report_version == 1 else
                     "SELECT s.step, s.actor, s.status, u.step IS NOT NULL FROM steps s "
                     "LEFT JOIN usage_steps u ON u.step = s.step ORDER BY s.step")
            with closing(self._db.execute(query)) as rows:
                for entry in rows:
                    step, actor, step_status = entry[:3]
                    counts["steps"] += 1
                    row = dict(kind="step", id=step, value=dict(actor=actor, status=step_status))
                    if report_version == 2:
                        row["usage_recorded"] = bool(entry[3])
                    yield row
                    self._ensure_open()
            with closing(self._db.execute("SELECT * FROM tools ORDER BY call")) as rows:
                for call, actor, step, tool, call_status, digest in rows:
                    value = dict(actor=actor, step=step, tool=tool, status=call_status)
                    if digest is not None:
                        value["digest"] = digest
                    counts["tools"] += 1
                    yield dict(kind="tool", id=call, value=value)
                    self._ensure_open()
            # One checkpoint per event, not one unbounded list per step.
            with closing(self._db.execute(
                "SELECT step, digest FROM checkpoints ORDER BY step, seq"
            )) as rows:
                for step, digest in rows:
                    counts["checkpoints"] += 1
                    yield dict(kind="checkpoint", step=step, digest=digest)
                    self._ensure_open()
        except sqlite3.Error as exc:
            self._invalidate()
            raise HistoryStorageError("history export failed; rebuild from verified prefix") from exc
        finally:
            try:
                if transaction and not self._closed:
                    self._db.execute("ROLLBACK")
            except sqlite3.Error as exc:
                self._invalidate()
                raise HistoryStorageError("history export cleanup failed; rebuild from verified prefix") from exc
            finally:
                self._reporting = False
        # Cleanup succeeds before any consumer can observe completion.
        yield dict(complete=True, counts=counts)

    def snapshot(self) -> dict:
        """Materialize the existing snapshot format, including exact history."""
        state = self._state()
        return dict(format="weasley-replay-snapshot", snapshot_version=1, trace_version=1,
                    prefix_events=self._count, prefix_sha256=self._digest.hexdigest(), state=state)

    @classmethod
    def from_snapshot(cls, saved: dict, prefix: Iterable[dict], path: str | Path) -> SQLiteReplayState:
        """Reconstruct into a NEW file and verify every saved field against prefix.

        Never install serialized state or seed a hash from its digest. Prefix
        replay remains necessary, and snapshot verification materializes history.
        """
        _validate_snapshot(saved)
        restored = cls(path)
        try:
            restored.extend(prefix)
            _require(_canonical(restored.snapshot()) == _canonical(saved),
                     "snapshot does not match verified prefix")
            return restored
        except BaseException:
            restored._invalidate()
            raise
