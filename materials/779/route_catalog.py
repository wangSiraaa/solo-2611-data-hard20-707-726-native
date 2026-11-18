"""Reading and lookup for the deployed v1 export-route catalogue."""

import json
from pathlib import Path


def load_catalog(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload["schema"] != "route-capacity/v1":
        raise ValueError("unsupported route catalogue schema")

    limits = {}
    for route in payload["routes"]:
        name, rate = route["route"], route["rate"]
        if not isinstance(name, str) or not name or name in limits:
            raise ValueError("invalid or repeated route")
        if type(rate) is not int or rate <= 0:
            raise ValueError("route rate must be a positive integer")
        limits[name] = rate
    return limits


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Look up one export-route limit")
    parser.add_argument("catalog")
    parser.add_argument("route")
    args = parser.parse_args()
    print(json.dumps({"route": args.route, "rate": load_catalog(args.catalog)[args.route]}))
