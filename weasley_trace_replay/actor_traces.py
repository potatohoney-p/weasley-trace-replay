"""Opt-in actor-local trace fidelity, not causal or semantic equivalence."""

from collections.abc import Iterable
import hashlib

from .replay import ReplayState, _canonical, compare


def _project(events: Iterable[dict], allow_partial: bool) -> tuple[dict, dict]:
    state = ReplayState()
    digests = {}
    counts = {}
    for event in events:
        # Never fingerprint an invalid event or bypass normal lifecycle checks.
        state.append(event)
        if "actor" in event:
            actor = event["actor"]
            if actor not in digests:
                digests[actor] = hashlib.sha256()
                counts[actor] = 0
            # seq/run encode global scheduling and run identity. All other v1
            # fields, including usage segmentation and checkpoint placement,
            # remain part of this actor's ordered recorded projection.
            local = {key: value for key, value in event.items() if key not in {"seq", "run"}}
            digests[actor].update(_canonical(local) + b"\n")
            counts[actor] += 1
    report = state.report(allow_partial=allow_partial)
    return report, {actor: {"events": counts[actor], "sha256": digests[actor].hexdigest()}
                    for actor in sorted(digests)}


def compare_actor_traces(left: Iterable[dict], right: Iterable[dict], *,
                         allow_partial: bool = False) -> dict:
    """Require both v1 observation equality and actor-local trace equality.

    Each input is consumed once and fully validated before returning. Per-actor
    SHA-256 fingerprints remove only seq/run; stable actor/step/call IDs remain
    required. Cross-actor interleavings are ignored, not proven independent.
    Within one actor, even concurrent-call arrival order and usage segmentation
    remain exact. No tolerance policy is applied. Retains ordinary replay state
    plus one hash/count per actor, not a bounded-memory replay implementation.
    """
    before, old = _project(left, allow_partial)
    after, new = _project(right, allow_partial)
    observations = compare(before, after)
    changes = [{"actor": actor, "before": old.get(actor), "after": new.get(actor)}
               for actor in sorted(old.keys() | new.keys()) if old.get(actor) != new.get(actor)]
    return {
        "comparison_version": 1,
        "mode": "actor-local-v1",
        "equivalent": observations["equivalent"] and not changes,
        "observations": observations,
        "actor_traces": {"equivalent": not changes, "changes": changes},
        "baseline_trace_sha256": before["trace_sha256"],
        "candidate_trace_sha256": after["trace_sha256"],
    }
