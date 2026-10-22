"""Offline acceptance for one pinned tau2 episode, with optional SQLite parity."""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
import json
from pathlib import Path
import tempfile

from weasley_trace_replay import ReplayState, compare, replay, resume, serialize_snapshot
from weasley_trace_replay.tau2 import adapt_episode, read_episode

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "examples/tau2/task46-trial0.json"
MANIFEST = ROOT / "examples/tau2/provenance.json"
FIXTURE_CANONICAL_SHA = "9fc77bb6ca72f58a42589ffa8bbac89d26fec1b0d0bef53ea2a8ddb523fec636"
MAX_FIXTURE_BYTES = 262144
RUN = "bfaceb85-d727-4de5-b37c-e31fdf90fb39"
CALL = "call_jSwZRs774js2PBliHRee5crf"
TOOL = "transfer_to_human_agents"
RESULT = "Transfer successful"
RESULT_SHA = "bccdc18e7b6ac085553ef044cadec50c310dffd4a86277e0c5c3940aa0e9ce04"
CHANGED_RESULT = "Transfer successful [counterfactual]"
PINNED_SOURCE = {
    "source_url": "https://github.com/sierra-research/tau2-bench/blob/"
                  "05736c27219b769d0be507574dd3135010916644/data/tau2/results/final/"
                  "o4-mini-2025-04-16_airline_default_gpt-4.1-2025-04-14_4trials.json",
    "artifact_revision": "05736c27219b769d0be507574dd3135010916644",
    "artifact_bytes": 7599444,
    "artifact_sha256": "6a3a354c9bce42b52905af096e9cea6f555548acbb17b567fd403c356df7b4ec",
    "original_row_canonical_sha256": "f8878ea1a9b47309d37b1ea76fa1bbeb3d012e7c2918b6e5e90fd4f821c99e96",
    "declared_runner_revision": "c30d59aaa71c65f9b9eb6a8f8636b48945028fcf",
    "runner_source_reproduced": False,
}


class AcceptanceError(ValueError):
    """The fixture, mapping or optional backend failed an acceptance gate."""


class SQLiteUnavailableError(AcceptanceError):
    """An explicitly requested optional backend could not be loaded."""


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def _verify(actual, expected, label: str) -> None:
    # Canonical JSON distinguishes booleans from integer report fields.
    if _canonical(actual) != _canonical(expected):
        raise AcceptanceError(f"{label} mismatch")


def _fixture_bytes(path: Path) -> bytes:
    with path.open("rb") as stream:
        raw = stream.read(MAX_FIXTURE_BYTES + 1)
    if len(raw) > MAX_FIXTURE_BYTES:
        raise AcceptanceError("fixture exceeds byte limit")
    return raw


def expected_events() -> list[dict]:
    """Literal native facts and declared derived boundary; no adapter helpers."""
    base = {"v": 1, "run": RUN}
    scope = {"actor": "assistant", "step": "conversation"}
    return [
        {**base, "seq": 0, "type": "run_started"},
        {**base, "seq": 1, "type": "step_started", **scope},
        {**base, "seq": 2, "type": "tool_started", **scope, "call": CALL, "tool": TOOL},
        {**base, "seq": 3, "type": "tool_finished", **scope, "call": CALL,
         "status": "ok", "digest": RESULT_SHA},
        {**base, "seq": 4, "type": "step_finished", **scope, "status": "ok"},
        {**base, "seq": 5, "type": "run_finished", "status": "completed"},
    ]


def expected_report() -> dict:
    """Independent full report, including an independently canonicalized hash."""
    trace = b"".join(_canonical(event) + b"\n" for event in expected_events())
    return {
        "version": 1, "run": RUN, "status": "completed", "events": 6,
        "trace_sha256": hashlib.sha256(trace).hexdigest(),
        "steps": {"conversation": {"actor": "assistant", "status": "ok"}},
        "tools": {CALL: {"actor": "assistant", "step": "conversation", "tool": TOOL,
                         "status": "ok", "digest": RESULT_SHA}},
        "checkpoints": {}, "usage": {"input_tokens": 0, "output_tokens": 0},
        "unfinished_steps": [], "unfinished_tools": [],
    }


def _sqlite_backend():
    try:
        module = importlib.import_module("weasley_trace_replay.sqlite_history")
        return module.SQLiteReplayState
    except (ImportError, AttributeError) as exc:
        raise SQLiteUnavailableError("SQLite backend requested but unavailable") from exc


def _source_identity(use_sqlite: bool) -> dict:
    paths = ["demos/tau2_compatibility.py", "weasley_trace_replay/tau2.py",
             "weasley_trace_replay/replay.py", "examples/tau2/provenance.json"]
    if use_sqlite:
        paths.append("weasley_trace_replay/sqlite_history.py")
    return {name: hashlib.sha256((ROOT / name).read_text(encoding="utf-8").encode("utf-8")).hexdigest()
            for name in paths}


def _check_prefixes(events: list[dict], expected: dict, sqlite_class=None) -> None:
    with tempfile.TemporaryDirectory(prefix="tau2-parity-") as directory:
        for boundary in range(1, len(events) + 1):
            prefix, suffix = events[:boundary], events[boundary:]
            state = ReplayState()
            state.extend(iter(prefix))
            partial = state.report(allow_partial=True)
            saved = json.loads(serialize_snapshot(state.snapshot()))
            _verify(resume(saved, iter(prefix), iter(suffix)), expected, "prefix resume report")
            if sqlite_class is None:
                continue
            path = Path(directory) / f"prefix-{boundary}.sqlite"
            rebuilt_path = Path(directory) / f"rebuilt-{boundary}.sqlite"
            with sqlite_class(path) as disk:
                disk.extend(iter(prefix))
                _verify(disk.report(allow_partial=True), partial, "SQLite prefix report")
                _verify(disk.snapshot(), saved, "SQLite prefix snapshot")
                disk.extend(iter(suffix))
                _verify(disk.report(), expected, "SQLite completed report")
            with sqlite_class.from_snapshot(saved, iter(prefix), rebuilt_path) as rebuilt:
                _verify(rebuilt.snapshot(), saved, "SQLite verified reconstruction snapshot")
                _verify(rebuilt.report(allow_partial=True), partial, "SQLite verified reconstruction report")
                rebuilt.extend(iter(suffix))
                _verify(rebuilt.report(), expected, "SQLite reconstructed completed report")


def run(fixture: Path = FIXTURE, *, use_sqlite: bool = False) -> dict:
    sqlite_class = _sqlite_backend() if use_sqlite else None
    sources = _source_identity(use_sqlite)
    fixture_bytes = _fixture_bytes(fixture)
    row = read_episode(fixture)
    fixture_canonical_sha = hashlib.sha256(_canonical(row)).hexdigest()
    _verify(fixture_canonical_sha, FIXTURE_CANONICAL_SHA, "pinned fixture fingerprint")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    _verify(manifest["fixture_canonical_sha256"], FIXTURE_CANONICAL_SHA, "manifest fixture fingerprint")
    _verify({key: manifest[key] for key in PINNED_SOURCE}, PINNED_SOURCE, "pinned source provenance")
    _verify({key: row[key] for key in ("id", "task_id", "trial", "seed", "termination_reason")},
            {"id": RUN, "task_id": "46", "trial": 0, "seed": 626729,
             "termination_reason": "user_stop"}, "pinned episode identity")
    _verify([message["role"] for message in row["messages"]],
            ["assistant", "user", "assistant", "tool", "assistant", "user"], "source messages")
    _verify(row["messages"][3]["content"], RESULT, "native tool output")
    _verify(row["messages"][3]["error"], False, "native tool error flag")
    adapted = adapt_episode(row)
    _verify(adapted["adapter"], "tau2-single-call-transfer-v1", "adapter profile")
    _verify(adapted["provenance"], {
        "input_sha256": FIXTURE_CANONICAL_SHA,
        "identity": {"id": RUN, "task_id": "46", "trial": 0, "seed": 626729},
        "source_terminal": "user_stop", "terminal_signal": "###TRANSFER###",
        "usage": {"assistant_observed_turns": [2, 4], "assistant_unreported_turns": [0],
                  "source_assistant_complete": False, "emitted": False,
                  "replay_v1_aggregate": "unreported"},
    }, "adapter provenance")
    _verify(adapted["derived"], {
        "v": 1, "seq": "emitted event order, not native turn indices", "actor": "assistant",
        "step": "conversation", "step_status": "closed episode scope only",
        "run_status": "recorded conversation boundary only",
    }, "derived boundary declarations")
    events = adapted["events"]
    _verify(events, expected_events(), "independent mapped trace")
    report = replay(events)
    expected = expected_report()
    _verify(report, expected, "independent full report")
    _check_prefixes(events, expected, sqlite_class)

    changed = copy.deepcopy(row)
    changed["messages"][3]["content"] = CHANGED_RESULT
    changed_events = adapt_episode(changed)["events"]
    changed_sha = hashlib.sha256(CHANGED_RESULT.encode("utf-8")).hexdigest()
    wanted_changed = expected_events()
    wanted_changed[3]["digest"] = changed_sha
    _verify(changed_events, wanted_changed, "counterfactual mapped trace")
    drift = compare(report, replay(changed_events))
    _verify(drift, {"equivalent": False, "changes": [
        {"path": f"/tools/{CALL}/digest", "before": RESULT_SHA, "after": changed_sha}
    ]}, "counterfactual output drift")
    if sources != _source_identity(use_sqlite) or fixture_bytes != _fixture_bytes(fixture):
        raise AcceptanceError("acceptance inputs changed during run")
    return {
        "acceptance": "tau2-pinned-episode", "protocol_version": 1, "passed": True,
        "fixture_sha256": hashlib.sha256(fixture_bytes).hexdigest(), "sources": sources,
        "fixture_canonical_sha256": fixture_canonical_sha,
        "declared_source": {key: manifest[key] for key in PINNED_SOURCE},
        "source_identity": {"simulation_id": RUN, "task_id": "46", "trial": 0, "seed": 626729},
        "trace_sha256": expected["trace_sha256"],
        "report_sha256": hashlib.sha256(_canonical(expected)).hexdigest(),
        "facts": {"source_messages": 6, "mapped_events": 6, "verified_nonempty_prefixes": 6,
                  "native_call_id_preserved": CALL, "output_utf8_bytes": len(RESULT.encode("utf-8")),
                  "output_sha256": RESULT_SHA, "tool_status": "ok",
                  "usage": "unreported: one assistant message has missing usage",
                  "sqlite_parity": "passed" if use_sqlite else "not requested"},
        "counterfactual_output_drift": drift,
        "provenance": adapted["provenance"], "losses": adapted["losses"], "derived": adapted["derived"],
        "limits": ["Completed denotes a derived supported conversation boundary.",
                   "No claim of human transfer completion or benchmark task success.",
                   "Zero report token totals denote unreported usage, not measured zero consumption.",
                   "Offline mapping and replay only; no provider execution or performance measurement."],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=FIXTURE)
    parser.add_argument("--sqlite", action="store_true", help="require the optional SQLite replay backend")
    args = parser.parse_args(argv)
    try:
        result = run(args.fixture, use_sqlite=args.sqlite)
    except SQLiteUnavailableError:
        print(json.dumps({"passed": False, "error": "sqlite_backend_unavailable"}, sort_keys=True))
        return 1
    except (ValueError, OSError, KeyError, TypeError, RuntimeError):
        print(json.dumps({"passed": False, "error": "invalid_or_failed_acceptance"}, sort_keys=True))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
