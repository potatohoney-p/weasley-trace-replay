"""Seeded synthetic causal graphs, independent state oracles, and failure reduction.

No runtime adapter, provider call, or third-party dependency is used. The graph
is test-only: cross-actor edges are not an extension to the trace v1 contract.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
from typing import Callable

from weasley_trace_replay import TraceError, compare, replay

OBSERVATIONS = ("status", "steps", "tools", "checkpoints", "usage",
                "unfinished_steps", "unfinished_tools")
SEEDS = (3, 17, 101)


def _number(*parts: object) -> int:
    # Avoid depending on Python's process hash seed or random implementation.
    return int.from_bytes(hashlib.sha256(":".join(map(str, parts)).encode()).digest(), "big")


@dataclass(frozen=True)
class Node:
    key: str
    event: dict
    after: tuple[str, ...] = ()


def graph(seed: int, actors: int = 3) -> list[Node]:
    """Build a DAG with actor-local steps, parallel tools, and cross-actor edges."""
    if type(seed) is not int or type(actors) is not int or not 3 <= actors <= 8:
        raise ValueError("an integer seed and 3 to 8 actors are required")
    nodes = [Node("start", {"type": "run_started"})]
    step_ends = []
    for actor_index in range(actors):
        actor = f"actor{actor_index}"
        previous = "start"
        for step_index in range(2):
            step = f"{actor}-step{step_index}"
            scope = {"actor": actor, "step": step}
            start = f"{step}-start"
            after = (previous,)
            if step_index == 1 and actor_index:
                after += (f"actor{actor_index - 1}-step0-end",)
            nodes.append(Node(start, dict(type="step_started", **scope), after))
            call_ids = [f"{step}-call{i}" for i in range(2 + _number(seed, step) % 2)]
            starts = tuple(f"{call}-start" for call in call_ids)
            ends = []
            for i, call in enumerate(call_ids):
                nodes.append(Node(starts[i], dict(type="tool_started", call=call,
                                                 tool=f"tool{i}", **scope), (start,)))
                end = f"{call}-end"
                ends.append(end)
                nodes.append(Node(end, dict(type="tool_finished", call=call,
                                            status=("ok", "error", "cancelled")[i % 3],
                                            digest=f"{_number(seed, call):064x}", **scope), starts))
            for i in range(2):
                checkpoint = f"{step}-checkpoint{i}"
                nodes.append(Node(checkpoint, dict(type="checkpoint", **scope,
                                                  digest=f"{_number(seed, checkpoint):064x}"),
                                  (start if i == 0 else f"{step}-checkpoint0",)))
                usage = f"{step}-usage{i}"
                nodes.append(Node(usage, dict(type="usage", **scope,
                                             input_tokens=1 + _number(seed, usage) % 100,
                                             output_tokens=1 + _number(seed, usage, "out") % 20), (start,)))
            # Some graphs end failed/cancelled steps honestly, others recover
            # error/cancelled tool outcomes inside successful steps.
            status = "ok"
            if actor_index == actors - 1 and step_index == 1 and seed % 2:
                status = ("error", "cancelled", "ok")[(seed // 3) % 3]
            previous = f"{step}-end"
            step_ends.append(previous)
            nodes.append(Node(previous, dict(type="step_finished", status=status, **scope),
                              tuple(ends) + (f"{step}-checkpoint1", f"{step}-usage0", f"{step}-usage1")))
    terminal = "completed" if all(n.event.get("status") == "ok" for n in nodes
                                  if n.event["type"] == "step_finished") else "failed"
    nodes.append(Node("end", dict(type="run_finished", status=terminal), tuple(step_ends)))
    return nodes


def schedule(nodes: list[Node], seed: int) -> list[Node]:
    """Stable hash-priority topological sort; dependencies never get shuffled away."""
    pending = {node.key: node for node in nodes}
    if len(pending) != len(nodes):
        raise ValueError("duplicate graph node")
    output = []
    seen = set()
    while pending:
        ready = [node for node in pending.values() if set(node.after) <= seen]
        if not ready:
            raise ValueError("cyclic graph or missing dependency")
        node = min(ready, key=lambda item: (_number(seed, item.key), item.key))
        output.append(node)
        seen.add(node.key)
        del pending[node.key]
    return output


def records(nodes: list[Node]) -> list[dict]:
    return [dict(v=1, seq=i, run="causal-synthetic", **node.event) for i, node in enumerate(nodes)]


def _observation(state: dict) -> dict:
    return {key: state.get(key) for key in OBSERVATIONS}


def oracle(nodes: list[Node]) -> dict:
    """Declarative projections of recorded facts, NOT the replay state machine.

    This intentionally does not call replay/compare or validate transitions. The
    DAG supplies validity; start/end set membership supplies expected state.
    """
    events = [node.event for node in nodes]
    step_ends = {e["step"]: e["status"] for e in events if e["type"] == "step_finished"}
    call_ends = {e["call"]: e for e in events if e["type"] == "tool_finished"}
    steps = {e["step"]: {"actor": e["actor"], "status": step_ends.get(e["step"], "running")}
             for e in events if e["type"] == "step_started"}
    tools = {}
    for e in events:
        if e["type"] == "tool_started":
            tools[e["call"]] = {key: e[key] for key in ("actor", "step", "tool")}
            end = call_ends.get(e["call"])
            tools[e["call"]].update({"status": end["status"], "digest": end["digest"]}
                                    if end else {"status": "running"})
    checkpoints = {}
    for e in events:
        if e["type"] == "checkpoint":
            checkpoints.setdefault(e["step"], []).append(e["digest"])
    return {
        "status": next((e["status"] for e in events if e["type"] == "run_finished"), "running"),
        "steps": steps, "tools": tools, "checkpoints": checkpoints,
        "usage": {key: sum(e[key] for e in events if e["type"] == "usage")
                  for key in ("input_tokens", "output_tokens")},
        "unfinished_steps": sorted(key for key in steps if key not in step_ends),
        "unfinished_tools": sorted(key for key in tools if key not in call_ends),
    }


def _valid_selection(nodes: list[Node]) -> bool:
    seen = set()
    terminal = False
    for node in nodes:
        if node.key in seen or not set(node.after) <= seen or terminal:
            return False
        seen.add(node.key)
        terminal = node.event["type"] == "run_finished"
    return bool(nodes) and nodes[0].key == "start"


class NonReproducingFailure(ValueError):
    """An observed failure is not repeatable enough to justify reduction."""


def minimize(items: list, fails: Callable[[list], bool]) -> list:
    """Deletion reducer: preserve the exact predicate, then reach 1-minimality.

    1-minimal means no single deletion still fails, not a global minimum. Trace
    predicates resequence candidates so sequence gaps cannot mask the real fault.
    """
    current = list(items)
    if not fails(current):
        raise NonReproducingFailure("counterexample does not reproduce")
    width = max(1, len(current) // 2)
    while width:
        index = 0
        while index < len(current):
            trial = current[:index] + current[index + width:]
            if fails(trial):
                current = trial
                index = 0
            else:
                index += width
        width //= 2
    if not fails(current):
        raise NonReproducingFailure("reduced counterexample no longer reproduces")
    return current


class CampaignFailure(AssertionError):
    def __init__(self, label: str, original: list[Node], fails: Callable[[list[Node]], bool],
                 *, context: dict | None = None,
                 baseline: Callable[[list[Node]], list[Node]] | None = None):
        super().__init__(label)
        self.label = label
        self.original_count = len(original)
        self.context = context or {}
        try:
            self.nodes = minimize(original, fails)
            self.minimality = "1-minimal under deletion and resequencing for this failure predicate"
        except NonReproducingFailure:
            # Stateful/nondeterministic failures need the original campaign
            # context. Never claim that an unrepeatable trace was minimized.
            self.nodes = list(original)
            self.minimality = "not reduced: failure did not reproduce consistently"
        self.trace = records(self.nodes)
        self.baseline_trace = records(baseline(self.nodes)) if baseline else None


def _check(label: str, nodes: list[Node], fails: Callable[[list[Node]], bool],
           *, context: dict | None = None,
           baseline: Callable[[list[Node]], list[Node]] | None = None) -> None:
    if fails(nodes):
        raise CampaignFailure(label, nodes, fails, context=context, baseline=baseline)


def _invalid_witness(nodes: list[Node], kind: str) -> bool:
    """Small independent witnesses keep invalid-case shrinking honest."""
    events = [n.event for n in nodes]
    for index, event in enumerate(events):
        before = events[:index]
        if kind == "duplicate_result" and event["type"] == "tool_finished":
            if any(e["type"] == "tool_finished" and e["call"] == event["call"] for e in before):
                return True
        if kind == "result_without_start" and event["type"] == "tool_finished":
            if not any(e["type"] == "tool_started" and e["call"] == event["call"] for e in before):
                return True
        if kind == "premature_step_finish" and event["type"] == "step_finished":
            starts = {e["call"] for e in before if e["type"] == "tool_started" and e["step"] == event["step"]}
            ends = {e["call"] for e in before if e["type"] == "tool_finished"}
            if starts - ends:
                return True
        if kind == "completed_with_pending" and event["type"] == "run_finished" and event["status"] == "completed":
            starts = {e["step"] for e in before if e["type"] == "step_started"}
            ends = {e["step"] for e in before if e["type"] == "step_finished"}
            if starts - ends:
                return True
        if kind == "after_terminal" and any(e["type"] == "run_finished" for e in before):
            return True
    return False


def invalid_cases(order: list[Node]) -> dict[str, list[Node]]:
    first_result = next(i for i, n in enumerate(order) if n.event["type"] == "tool_finished")
    result = order[first_result]
    first_call = next(i for i, n in enumerate(order) if n.event["type"] == "tool_started")
    call = order[first_call].event
    return {
        "duplicate_result": order[:first_result + 1] + [replace(result, key="duplicate")],
        "result_without_start": [n for n in order[:first_result + 1]
                                 if not (n.event["type"] == "tool_started" and n.event["call"] == result.event["call"])],
        "premature_step_finish": order[:first_call + 1] + [Node("early-finish", dict(
            type="step_finished", actor=call["actor"], step=call["step"], status="ok"))],
        "completed_with_pending": order[:first_call + 1] + [Node("early-completed", dict(
            type="run_finished", status="completed"))],
        "after_terminal": order + [Node("late-terminal", dict(type="run_finished", status="cancelled"))],
    }


def run(*, seeds: tuple[int, ...] = SEEDS, schedules: int = 4,
        replay_fn: Callable = replay, compare_fn: Callable = compare) -> dict:
    """Run the bounded campaign; injected functions are only for harness self-tests."""
    if not seeds or any(type(seed) is not int for seed in seeds) or type(schedules) is not int or schedules < 1:
        raise ValueError("nonempty integer seeds and a positive schedule count are required")
    report = {"experiment": "causal-lifecycle-v1", "seeds": list(seeds), "graphs": 0,
              "schedules": 0, "prefixes": 0, "cancellations": 0, "append_recoveries": 0,
              "incomplete_traces_rejected": 0,
              "invalid_cases_rejected": 0, "drift_cases_detected": 0, "network_calls": 0,
              "cases": []}

    def wrong_observation(nodes):
        if not _valid_selection(nodes):
            return False
        try:
            return _observation(replay_fn(records(nodes), allow_partial=True)) != oracle(nodes)
        except TraceError:
            return True

    for graph_index, seed in enumerate(seeds):
        actors = 3 + graph_index % 3
        nodes = graph(seed, actors)
        reference = schedule(nodes, 0)
        lookup = {node.key: node for node in reference}
        _check("declarative_oracle", reference, wrong_observation)
        report["graphs"] += 1
        case = {"seed": seed, "actors": actors, "events": len(nodes), "schedule_sha256": []}
        for schedule_index in range(schedules):
            order = schedule(nodes, _number(seed, schedule_index))
            _check("declarative_oracle", order, wrong_observation)
            def strict_rejection(selected):
                if not _valid_selection(selected) or oracle(selected)["status"] == "running":
                    return False
                try:
                    replay_fn(records(selected))
                    return False
                except TraceError:
                    return True

            context = {"seed": seed, "schedule_index": schedule_index, "replay_mode": "strict"}
            try:
                state = replay_fn(records(order))
            except TraceError as exc:
                raise CampaignFailure("strict_replay_rejection", order, strict_rejection,
                                      context=context) from exc

            def equivalence_baseline(selected):
                keys = {n.key for n in selected}
                return [n for n in reference if n.key in keys]

            def inequivalent(selected):
                if not _valid_selection(selected):
                    return False
                return not compare_fn(replay_fn(records(equivalence_baseline(selected)), allow_partial=True),
                                      replay_fn(records(selected), allow_partial=True))["equivalent"]

            _check("interleaving_equivalence", order, inequivalent,
                   context=dict(context, replay_mode="partial", check="observational_equivalence"),
                   baseline=equivalence_baseline)
            report["schedules"] += 1
            case["schedule_sha256"].append(state["trace_sha256"])
            for stop in range(1, len(order)):
                prefix = order[:stop]
                _check("interrupted_prefix_oracle", prefix, wrong_observation)

                def accepted_incomplete(selected):
                    if not _valid_selection(selected) or oracle(selected)["status"] != "running":
                        return False
                    try:
                        replay_fn(records(selected))
                        return True
                    except TraceError:
                        return False

                _check("accepted_incomplete", prefix, accepted_incomplete)
                report["incomplete_traces_rejected"] += 1
                cancelled = prefix + [Node("cancel", dict(type="run_finished", status="cancelled"), ("start",))]
                _check("cancelled_prefix_oracle", cancelled, wrong_observation)
                append_context = dict(context, prefix_events=stop,
                                      check="baseline versus prefix-plus-suffix full replay")
                try:
                    appended = replay_fn(records(prefix + order[stop:]))
                except TraceError as exc:
                    raise CampaignFailure("append_replay_rejection", order, strict_rejection,
                                          context=append_context) from exc
                if appended != state:
                    def repeat_mismatch(selected):
                        if not _valid_selection(selected) or oracle(selected)["status"] == "running":
                            return False
                        try:
                            return replay_fn(records(selected)) != replay_fn(records(selected))
                        except TraceError:
                            # Rejection is not evidence for a repeat-output mismatch.
                            return False

                    raise CampaignFailure("append_replay_mismatch", order, repeat_mismatch,
                                          context=append_context, baseline=lambda selected: selected)
                report["prefixes"] += 1
                report["cancellations"] += 1
                report["append_recoveries"] += 1
            for kind, bad in invalid_cases(order).items():
                def accepted_invalid(selected, kind=kind):
                    if not _invalid_witness(selected, kind):
                        return False
                    try:
                        replay_fn(records(selected), allow_partial=True)
                        return True
                    except TraceError:
                        return False
                _check(f"accepted_{kind}", bad, accepted_invalid)
                report["invalid_cases_rejected"] += 1
            for kind, field, value in (("tool_finished", "digest", "0" * 64),
                                       ("tool_finished", "status", "cancelled"),
                                       ("checkpoint", "digest", "0" * 64),
                                       ("usage", "input_tokens", 999)):
                target = next(n for n in order if n.event["type"] == kind and n.event[field] != value)
                changed = replace(target, event=dict(target.event, **{field: value}))
                candidate = [changed if n.key == target.key else n for n in order]

                def drift_baseline(selected):
                    return [lookup[n.key] for n in selected]

                def missed_drift(selected, key=target.key):
                    if not _valid_selection(selected) or not any(n.key == key for n in selected):
                        return False
                    return compare_fn(replay_fn(records(drift_baseline(selected)), allow_partial=True),
                                      replay_fn(records(selected), allow_partial=True))["equivalent"]

                _check(f"missed_{kind}_{field}", candidate, missed_drift,
                       context=dict(context, replay_mode="partial", check="observational_drift"),
                       baseline=drift_baseline)
                report["drift_cases_detected"] += 1
        report["cases"].append(case)
    report["passed"] = True
    return report


def save_failure(failure: CampaignFailure, directory: Path) -> None:
    """Write only synthetic evidence to a new directory; never overwrite a run."""
    directory.mkdir(parents=True, exist_ok=False)
    trace = "".join(json.dumps(event, sort_keys=True) + "\n" for event in failure.trace)
    (directory / "counterexample.jsonl").write_text(trace, encoding="utf-8")
    if failure.baseline_trace is not None:
        baseline = "".join(json.dumps(event, sort_keys=True) + "\n" for event in failure.baseline_trace)
        (directory / "baseline.jsonl").write_text(baseline, encoding="utf-8")
        (directory / "candidate.jsonl").write_text(trace, encoding="utf-8")
    metadata = {"failure": failure.label, "original_events": failure.original_count,
                "reduced_events": len(failure.trace), "synthetic": True,
                "minimality": failure.minimality, "context": failure.context}
    if failure.baseline_trace is not None:
        metadata.update(baseline_file="baseline.jsonl", candidate_file="candidate.jsonl")
    (directory / "failure.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--failure-dir", type=Path, help="new directory for a minimized synthetic counterexample, only on failure")
    args = parser.parse_args(argv)
    try:
        result = run()
    except CampaignFailure as failure:
        if args.failure_dir:
            save_failure(failure, args.failure_dir)
        print(json.dumps({"passed": False, "failure": failure.label,
                          "original_events": failure.original_count, "reduced_events": len(failure.trace),
                          "synthetic_counterexample": failure.trace,
                          "minimality": failure.minimality, "context": failure.context,
                          "synthetic_baseline": failure.baseline_trace}, sort_keys=True))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
