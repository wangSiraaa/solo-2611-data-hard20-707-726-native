"""Venue operator's existing command-line client."""

import argparse
import json
from pathlib import Path

import qualification
from scoreboard import rankings


def main():
    parser = argparse.ArgumentParser()
    actions = parser.add_subparsers(dest="action", required=True)
    scores = actions.add_parser("rankings")
    scores.add_argument("feed")
    first = actions.add_parser("first")
    first.add_argument("state")
    first.add_argument("release")
    first.add_argument("feed")
    first.add_argument("slots")
    start = actions.add_parser("started")
    start.add_argument("state")
    start.add_argument("seat")
    read = actions.add_parser("read")
    read.add_argument("state")
    read.add_argument("release")
    args = parser.parse_args()
    try:
        if args.action == "rankings":
            value = rankings(args.feed)
        elif args.action == "first":
            slots = json.loads(Path(args.slots).read_text(encoding="utf-8"))
            value = qualification.publish_first(args.state, args.release, slots, rankings(args.feed))
        elif args.action == "started":
            value = qualification.mark_started(args.state, args.seat)
        else:
            value = qualification.load(args.state)["receipts"][args.release]
            if value["format"] != "bracket-v1":
                raise ValueError("unknown receipt version")
            value = {slot: row["team"] for slot, row in value["slots"].items()}
        print(json.dumps(value, sort_keys=True, ensure_ascii=False))
    except (KeyError, ValueError, OSError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
