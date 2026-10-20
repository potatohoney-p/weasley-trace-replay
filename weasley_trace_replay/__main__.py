"""Command line: replay one trace, or compare two valid traces."""

import argparse
import json
import sys

from .replay import TraceError, compare, read_events, replay


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
    args = parser.parse_args(argv)
    try:
        if args.command == "replay":
            result = replay(read_events(args.trace), allow_partial=args.allow_partial)
        else:
            result = compare(
                replay(read_events(args.baseline), allow_partial=args.allow_partial),
                replay(read_events(args.candidate), allow_partial=args.allow_partial),
            )
        print(json.dumps(result, sort_keys=True, indent=2))
        return 1 if result.get("equivalent") is False else 0
    except (TraceError, OSError):
        # OS messages can contain paths; payloads can contain arbitrary strings.
        # Keep the CLI error contract content-free. Library users retain diagnostics.
        print(json.dumps({"error": "invalid_or_unreadable_trace"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
