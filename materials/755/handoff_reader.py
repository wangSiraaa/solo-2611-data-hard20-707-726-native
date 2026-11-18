#!/usr/bin/env python3
"""Read the frozen members of an asset-handoff/1 file only."""
import argparse
import json

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("handoff")
    args = ap.parse_args()
    with open(args.handoff, encoding="utf-8") as fh:
        p = json.load(fh)
    if p["format"] != "asset-handoff/1":
        raise ValueError("unsupported product")
    print(p["handoff_id"], p["from"], p["to"], p["membership_revision"])
    for item in p["members"]:
        print(item["asset_id"], item["type"])
    print("COUNT", len(p["members"]))

if __name__ == "__main__":
    main()
