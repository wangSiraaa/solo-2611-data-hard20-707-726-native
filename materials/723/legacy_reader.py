#!/usr/bin/env python3
"""Historical file reader only. No date parser, grouping service or database."""
import argparse
import json

def read_report(path):
    with open(path, encoding="utf-8") as fh:
        p = json.load(fh)
    if p["format"] != "partial-date-groups/1":
        raise ValueError("this historical reader only understands format 1")
    print(p["report_id"], p["date_contract"])
    for g in p["groups"]:
        print(g["group_id"], g["count"], ",".join(g["person_ids"]))
    print("outside", p["outside"]["count"])
    print("unknown", p["unknown"]["count"])

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("report")
    ap.add_argument("--people")
    args = ap.parse_args()
    if args.people:
        with open(args.people, encoding="utf-8") as fh:
            people = json.load(fh)
        for p in people["records"]:
            print(p["person_id"], p["raw_date"], p["cached_day"])
    read_report(args.report)

if __name__ == "__main__":
    main()
