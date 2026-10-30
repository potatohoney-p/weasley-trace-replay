"""Record-sized comparison of validated, live SQLite replay histories."""

from contextlib import ExitStack, closing
import copy
from itertools import zip_longest

from .replay import _require
from .sqlite_history import SQLiteReplayState


def _observations(state: SQLiteReplayState, allow_partial: bool):
    # Only consume our validated live producer, never arbitrary JSONL records.
    # The producer releases its read transaction before yielding completion.
    with closing(state.iter_report(allow_partial=allow_partial)) as stream:
        header = next(stream)
        yield {"kind": "summary", "status": header["status"], "usage": header["usage"]}
        complete = False
        for record in stream:
            if record.get("complete") is True:
                _require(not complete, "duplicate report completion")
                complete = True
            else:
                _require(not complete, "record after report completion")
                yield record
        _require(complete, "incomplete history export")


def compare_histories(left: SQLiteReplayState, right: SQLiteReplayState, *,
                      allow_partial: bool = False) -> dict:
    """Compare v1 observations without materializing history-sized reports.

    Inputs must be two distinct, live, exclusively owned SQLiteReplayState
    objects. Both are fully read, even after a mismatch; late read or cleanup
    failures raise instead of returning a conclusion. Input states remain owned
    by the caller. Run IDs, event counts and trace hashes are ignored, matching
    ordinary v1 compare(). Ordered checkpoint sequences and pending work remain
    significant. Missing usage and explicit zero retain the v1 ambiguity.

    Retains only one producer record per side and the first differing pair.
    Return evidence is detached. This is not a complete path-wise diff, arbitrary
    report validator, tolerance policy, or whole-process constant-memory claim.
    """
    _require(isinstance(left, SQLiteReplayState) and isinstance(right, SQLiteReplayState),
             "comparison requires SQLite replay states")
    _require(left is not right, "comparison requires distinct SQLite replay states")
    first = None
    counts = {"left": 0, "right": 0}
    with ExitStack() as stack:
        before = stack.enter_context(closing(_observations(left, allow_partial)))
        after = stack.enter_context(closing(_observations(right, allow_partial)))
        for index, (old, new) in enumerate(zip_longest(before, after)):
            counts["left"] += old is not None
            counts["right"] += new is not None
            if first is None and old != new:
                first = {"record_index": index, "before": copy.deepcopy(old),
                         "after": copy.deepcopy(new)}
    return {"comparison_version": 1, "mode": "sqlite-observations-v1",
            "equivalent": first is None, "first_difference": first,
            "compared_records": counts}
