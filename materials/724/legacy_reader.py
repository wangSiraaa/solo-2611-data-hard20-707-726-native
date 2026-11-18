#!/usr/bin/env python3
"""Read an already published legacy score file, without re-scoring answers."""
import argparse
import json

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("report")
    ap.add_argument("--catalog")
    args = ap.parse_args()
    if args.catalog:
        with open(args.catalog, encoding="utf-8") as fh:
            c = json.load(fh)
        for scale in c["scales"]:
            print("raw", scale["scale_id"], scale["version"],
                  [(e["question_id"], e["weight"], e["reverse"])
                   for e in scale["entries"]])
        print("legacy cache", c["direction_cache_by_question"])
    with open(args.report, encoding="utf-8") as fh:
        p = json.load(fh)
    if p["format"] != "published-scores/1":
        raise ValueError("historical reader only understands format 1")
    print(p["report_id"], p["scoring_contract"])
    for s in p["scores"]:
        print(s["respondent_id"], s["scale_id"], s["scale_version"],
              s["eligible"], s["weighted_sum"], s["mean"])

if __name__ == "__main__":
    main()
