"""Exercise one built wheel outside the source tree, with synthetic inputs only."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import venv


def check(wheel: Path) -> dict:
    if not __debug__:
        raise RuntimeError("installed validation requires assertions enabled")
    wheel = wheel.resolve(strict=True)
    if wheel.is_dir():
        wheels = list(wheel.glob("*.whl"))
        if len(wheels) != 1:
            raise ValueError("expected exactly one wheel in directory")
        wheel = wheels[0]
    if wheel.suffix != ".whl":
        raise ValueError("expected one wheel file")
    with tempfile.TemporaryDirectory(prefix="trace-installed-") as temporary:
        root = Path(temporary)
        environment, working = root / "environment", root / "working"
        working.mkdir()
        venv.EnvBuilder(with_pip=True).create(environment)
        binaries = environment / ("Scripts" if os.name == "nt" else "bin")
        python = binaries / ("python.exe" if os.name == "nt" else "python")
        command = binaries / ("weasley-trace-replay.exe" if os.name == "nt" else "weasley-trace-replay")
        clean_env = {k: v for k, v in os.environ.items()
                     if k not in {"PYTHONPATH", "PYTHONHOME"}}

        def execute(args: list[str], expected: int = 0) -> subprocess.CompletedProcess:
            result = subprocess.run(args, cwd=working, env=clean_env, text=True,
                                    capture_output=True, timeout=120)
            if result.returncode != expected:
                raise AssertionError(f"unexpected exit {result.returncode}, expected {expected}: "
                                     f"{result.stdout}\n{result.stderr}")
            return result

        execute([str(python), "-I", "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)])
        location = execute([str(python), "-I", "-c",
                            "import weasley_trace_replay; print(weasley_trace_replay.__file__)"]).stdout.strip()
        if not Path(location).resolve().is_relative_to(environment.resolve()):
            raise AssertionError("module did not load from isolated installation")

        events = [
            {"type": "run_started"},
            {"type": "step_started", "actor": "worker", "step": "step1"},
            {"type": "usage", "actor": "worker", "step": "step1", "input_tokens": 2, "output_tokens": 3},
            {"type": "step_finished", "actor": "worker", "step": "step1", "status": "ok"},
            {"type": "run_finished", "status": "completed"},
        ]
        events = [{"v": 1, "seq": seq, "run": "synthetic", **event}
                  for seq, event in enumerate(events)]

        def write(name: str, values: list[dict]) -> str:
            (working / name).write_text("".join(json.dumps(event) + "\n" for event in values), encoding="utf-8")
            return name

        trace = write("complete.jsonl", events)
        prefix = write("prefix.jsonl", events[:3])
        suffix = write("suffix.jsonl", events[3:])
        changed = [dict(event) for event in events]
        changed[2]["input_tokens"] = 3
        candidate = write("candidate.jsonl", changed)
        mismatched_prefix = write("mismatched-prefix.jsonl", changed[:3])
        (working / "policy.json").write_text(json.dumps({"policy_version": 1,
            "usage_tolerances": {"input_tokens": 1}}), encoding="utf-8")
        cases = []

        def cli(name: str, args: list[str], expected: int = 0) -> dict:
            result = execute([str(command), *args], expected)
            if result.stderr:
                raise AssertionError(f"unexpected stderr for {name}: {result.stderr}")
            cases.append(name)
            return json.loads(result.stdout)

        report = cli("console replay", ["replay", trace])
        assert report["events"] == 5 and report["status"] == "completed"
        assert report["usage"] == {"input_tokens": 2, "output_tokens": 3}
        module = execute([str(python), "-I", "-m", "weasley_trace_replay", "replay", trace])
        assert not module.stderr and json.loads(module.stdout) == report
        cases.append("isolated module replay")
        assert cli("equivalent diff", ["diff", trace, trace])["equivalent"] is True
        assert cli("drift exit one", ["diff", trace, candidate], 1)["equivalent"] is False
        policy = cli("explicit tolerance", ["diff", trace, candidate, "--policy", "policy.json"])
        assert policy["equivalent"] is True and policy["exact_equivalent"] is False
        assert len(policy["tolerated_changes"]) == 1
        assert cli("partial replay", ["replay", prefix, "--allow-partial"])["status"] == "running"
        saved = cli("snapshot", ["snapshot", prefix])
        (working / "saved.json").write_text(json.dumps(saved), encoding="utf-8")
        assert cli("verified resume", ["resume", "saved.json", prefix, suffix]) == report
        secret = "SYNTHETIC_REJECTION_SENTINEL"
        malformed = write("malformed.jsonl", [{"private": secret}])
        for name, args in [("malformed rejection", ["replay", malformed]),
                           ("incomplete rejection", ["replay", prefix]),
                           ("snapshot mismatch rejection", ["resume", "saved.json", mismatched_prefix, suffix])]:
            rejected = execute([str(command), *args], 2)
            assert not rejected.stdout and secret not in rejected.stderr
            assert json.loads(rejected.stderr) == {"error": "invalid_or_unreadable_trace"}
            cases.append(name)
        return {"scope": "synthetic installed-wheel CLI validation", "wheel": wheel.name,
                "isolated_import": True, "cases": cases, "passed": len(cases)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    print(json.dumps(check(args.wheel), sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
