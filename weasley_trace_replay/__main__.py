"""Offline replay, comparison, snapshot creation, and verified-prefix resume."""

import argparse
import json
import sys

from .replay import TraceError, compare, read_events, read_snapshot, replay, resume, snapshot
from .policy import compare_with_policy, read_policy


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    replay_parser = commands.add_parser("replay")
    replay_parser.add_argument("trace")
    replay_parser.add_argument("--allow-partial", action="store_true")
    diff_parser = commands.add_parser("diff")
    diff_parser.add_argument("baseline")
    diff_parser.add_argument("candidate")
    diff_parser.add_argument("--allow-partial", action="store_true")
    diff_parser.add_argument("--policy", help="strict JSON absolute token tolerance policy")
    snapshot_parser = commands.add_parser("snapshot", help="snapshot a valid nonempty prefix to stdout")
    snapshot_parser.add_argument("prefix")
    resume_parser = commands.add_parser("resume", help="verify the full prefix, then append a suffix")
    resume_parser.add_argument("snapshot")
    resume_parser.add_argument("prefix")
    resume_parser.add_argument("suffix")
    resume_parser.add_argument("--allow-partial", action="store_true")
    for command_parser in (replay_parser, diff_parser, resume_parser):
        command_parser.add_argument("--report-version", type=int, choices=(1, 2), default=1,
                                    help="v2 distinguishes unrecorded from recorded zero usage")
    args = parser.parse_args(argv)
    try:
        if args.command == "replay":
            result = replay(read_events(args.trace), allow_partial=args.allow_partial, report_version=args.report_version)
        elif args.command == "snapshot":
            result = snapshot(read_events(args.prefix))
        elif args.command == "resume":
            result = resume(read_snapshot(args.snapshot), read_events(args.prefix),
                            read_events(args.suffix), allow_partial=args.allow_partial, report_version=args.report_version)
        else:
            left = replay(read_events(args.baseline), allow_partial=args.allow_partial, report_version=args.report_version)
            right = replay(read_events(args.candidate), allow_partial=args.allow_partial, report_version=args.report_version)
            result = (compare(left, right) if args.policy is None else
                      compare_with_policy(left, right, read_policy(args.policy)))
        print(json.dumps(result, sort_keys=True, indent=2))
        return 1 if result.get("equivalent") is False else 0
    except (TraceError, OSError):
        # OS messages can contain paths; payloads can contain arbitrary strings.
        # Keep the CLI error contract content-free. Library users retain diagnostics.
        print(json.dumps({"error": "invalid_or_unreadable_trace"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
