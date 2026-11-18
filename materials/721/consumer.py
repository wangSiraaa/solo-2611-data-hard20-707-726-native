#!/usr/bin/env python3
"""Read a published classification-summary/1 file; no database access."""
import argparse
import json

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("product")
    args = ap.parse_args()
    with open(args.product, encoding="utf-8") as fh:
        p = json.load(fh)
    if p["format"] != "classification-summary/1":
        raise ValueError("unsupported product")
    cut = p["cut"]
    print("cut", cut["catalog_revision"], cut["fact_revision"])
    for g in p["groups"]:
        print(g["target_id"], g["count"], g["amount"],
              ",".join(x["ticket_id"] for x in g["contributions"]))
    print("UNION", p["union"]["count"], p["union"]["amount"])

if __name__ == "__main__":
    main()
