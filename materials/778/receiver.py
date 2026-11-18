"""The installed terminal reader for packets of format rail-scale-v1."""

import argparse
import json
from pathlib import Path


def decode(packet, name, profile):
    if packet["format"] != "rail-scale-v1":
        raise ValueError("unknown format")
    origin, step = packet["origin"], packet["step"]
    if type(origin) is not int or not profile["origin_min"] <= origin <= profile["origin_max"]:
        raise ValueError("origin outside receiver range")
    if type(step) is not int or step not in profile["steps"] or step <= 0:
        raise ValueError("unsupported step")
    found = {}
    for row in packet["rows"]:
        if row["receiver"] != name:
            continue
        sample, tick = row["sample"], row["tick"]
        if sample in found or type(tick) is not int or not 0 <= tick <= profile["tick_max"]:
            raise ValueError("duplicate position or tick outside receiver range")
        found[sample] = origin + tick * step
    if not found:
        raise ValueError("no data for receiver")
    return found


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("packet")
    parser.add_argument("profiles")
    parser.add_argument("name")
    args = parser.parse_args()
    try:
        packet = json.loads(Path(args.packet).read_text(encoding="utf-8"))
        profile = json.loads(Path(args.profiles).read_text(encoding="utf-8"))[args.name]
        print(json.dumps(decode(packet, args.name, profile), sort_keys=True))
    except (KeyError, OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
