"""Opt-in bounded verification of the pinned upstream tau2 artifact.

Never writes the archive, runs its tools, or contacts a model provider.
The default compatibility evaluator needs no network and no archive.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import urllib.request

from weasley_trace_replay.tau2 import adapt_episode, minimize_episode, read_episode

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "examples/tau2/provenance.json"
FIXTURE = ROOT / "examples/tau2/task46-trial0.json"
URL = "https://raw.githubusercontent.com/sierra-research/tau2-bench/05736c27219b769d0be507574dd3135010916644/data/tau2/results/final/o4-mini-2025-04-16_airline_default_gpt-4.1-2025-04-14_4trials.json"
SOURCE_URL = "https://github.com/sierra-research/tau2-bench/blob/05736c27219b769d0be507574dd3135010916644/data/tau2/results/final/o4-mini-2025-04-16_airline_default_gpt-4.1-2025-04-14_4trials.json"
REVISION = "05736c27219b769d0be507574dd3135010916644"
LIMIT = 8 * 1024 * 1024
ARTIFACT_BYTES = 7599444
ARTIFACT_SHA = "6a3a354c9bce42b52905af096e9cea6f555548acbb17b567fd403c356df7b4ec"


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("utf-8")


def normalized_numbers(value):
    """Alternate researcher hash protocol; not the preserving-types protocol."""
    if type(value) is float and value.is_integer():
        return int(value)
    if type(value) is dict:
        return {key: normalized_numbers(item) for key, item in value.items()}
    if type(value) is list:
        return [normalized_numbers(item) for item in value]
    return value


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def fetch_pinned() -> bytes:
    with urllib.request.urlopen(URL, timeout=60) as response:
        length = response.headers.get("Content-Length")
        if length is not None and not 0 <= int(length) <= LIMIT:
            raise ValueError("archive exceeds byte limit")
        raw = response.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError("archive exceeds byte limit")
    return raw


def verify(raw: bytes, manifest: dict, fixture: dict) -> dict:
    if (manifest["source_url"] != SOURCE_URL or manifest["artifact_revision"] != REVISION
            or manifest["runner_source_reproduced"] is not False):
        raise ValueError("unsupported provenance declaration")
    if (len(raw) != ARTIFACT_BYTES or len(raw) > LIMIT
            or hashlib.sha256(raw).hexdigest() != ARTIFACT_SHA
            or manifest["artifact_bytes"] != ARTIFACT_BYTES
            or manifest["artifact_sha256"] != ARTIFACT_SHA):
        raise ValueError("archive identity mismatch")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate archive key")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("nonfinite archive number")

    archive = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    rows = [row for row in archive["simulations"] if row["id"] == manifest["selected_identity"]["id"]]
    if len(rows) != 1:
        raise ValueError("selected row is missing or duplicated")
    row = rows[0]
    if canonical({key: row[key] for key in ("id", "task_id", "trial", "seed")}) != canonical(manifest["selected_identity"]):
        raise ValueError("selected row identity mismatch")
    if digest(row) != manifest["original_row_canonical_sha256"]:
        raise ValueError("original row fingerprint mismatch")
    if digest(normalized_numbers(row)) != manifest["integral_float_normalized_row_sha256"]:
        raise ValueError("normalized row fingerprint mismatch")
    minimized = minimize_episode(row)
    if canonical(minimized) != canonical(fixture) or digest(minimized) != manifest["fixture_canonical_sha256"]:
        raise ValueError("fixture projection mismatch")
    omitted = {"/" + key for key in row if key not in minimized}
    for index, (original, kept) in enumerate(zip(row["messages"], minimized["messages"])):
        omitted.update(f"/messages/{index}/{key}" for key in original if key not in kept)
    declared = manifest["fixture_omitted_fields"]
    if sorted(declared, key=lambda entry: entry["path"]) != [
            {"path": path, "scope": "entire subtree"} for path in sorted(omitted)]:
        raise ValueError("omission ledger mismatch")
    if adapt_episode(row)["events"] != adapt_episode(minimized)["events"]:
        raise ValueError("projection changes mapped observations")
    if archive["info"]["git_commit"] != manifest["declared_runner_revision"]:
        raise ValueError("declared runner revision mismatch")
    return {"verified": True, "artifact_bytes": len(raw), "artifact_sha256": ARTIFACT_SHA,
            "original_row_canonical_sha256": digest(row),
            "integral_float_normalized_row_sha256": digest(normalized_numbers(row)),
            "fixture_canonical_sha256": digest(minimized), "omitted_subtrees": len(omitted),
            "full_row_projection_parity": True, "runner_source_reproduced": False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--fetch-pinned", action="store_true", help="fetch only the pinned public URL, at most 8 MiB")
    mode.add_argument("--archive", type=Path, help="verify an existing archive without network access")
    args = parser.parse_args(argv)
    try:
        if args.fetch_pinned:
            raw = fetch_pinned()
        else:
            with args.archive.open("rb") as stream:
                raw = stream.read(LIMIT + 1)
        result = verify(raw, json.loads(MANIFEST.read_text(encoding="utf-8")), read_episode(FIXTURE))
    except (ValueError, OSError, KeyError, TypeError, RecursionError):
        print(json.dumps({"verified": False, "error": "source_verification_failed"}, sort_keys=True))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
