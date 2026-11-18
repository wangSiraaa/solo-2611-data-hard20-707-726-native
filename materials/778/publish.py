"""Publish legacy single-receiver packets with a zero origin and unit step."""

import argparse
import json
from pathlib import Path

from receiver import decode
from ruler_source import snapshot


def publish_legacy(data, profiles, name, samples):
    if not samples:
        raise ValueError("no requested samples")
    packet = {
        "format": "rail-scale-v1",
        "catalogue_revision": data["revision"],
        "origin": 0,
        "step": 1,
        "rows": [
            {"receiver": name, "sample": sample, "tick": data["positions"][sample]} for sample in samples
        ],
    }
    decode(packet, name, profiles[name])
    return packet


def main():
    parser = argparse.ArgumentParser()
    actions = parser.add_subparsers(dest="action", required=True)
    catalog = actions.add_parser("catalog")
    catalog.add_argument("source")
    legacy = actions.add_parser("legacy")
    legacy.add_argument("source")
    legacy.add_argument("profiles")
    legacy.add_argument("output")
    legacy.add_argument("receiver")
    legacy.add_argument("samples", nargs="+")
    args = parser.parse_args()
    try:
        data = snapshot(args.source)
        if args.action == "catalog":
            print(json.dumps(data, sort_keys=True))
        else:
            profiles = json.loads(Path(args.profiles).read_text(encoding="utf-8"))
            packet = publish_legacy(data, profiles, args.receiver, args.samples)
            with Path(args.output).open("x", encoding="utf-8") as output:
                json.dump(packet, output, sort_keys=True)
    except (KeyError, OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
