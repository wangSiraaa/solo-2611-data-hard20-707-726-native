#!/usr/bin/env python3
"""Consume period-bill/1 JSONL, preserving signed adjustments and original IDs."""
import argparse
import json

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("documents", nargs="+")
    args = ap.parse_args()
    seen = set()
    total = 0
    for path in args.documents:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                p = json.loads(line)
                if p["format"] != "period-bill/1":
                    raise ValueError("unsupported format")
                if p["document_id"] in seen:
                    raise ValueError("duplicate document ID")
                seen.add(p["document_id"])
                total += p["amount_cents"]
                print(p["document_id"], p["document_type"], p["period"],
                      p["amount_cents"], p.get("previous_statement_id"))
    print("NET", total)

if __name__ == "__main__":
    main()
