#!/usr/bin/env python3
"""Run the frozen 719 operation file and print a hand-checkable trace.

Usage::

    python3 demo_719.py [--db PATH]

The script always starts from a fresh database. It prints the operation
outcome and, at the end, per-bottle conservation that can be checked by hand.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from reagent.cli import execute_operations, _reserve_competition  # noqa: E402
from reagent.inventory import InventoryService  # noqa: E402
from reagent.schema import connect, initialize  # noqa: E402

ROOT = Path(__file__).resolve().parent
MATERIALS = ROOT / "materials" / "719"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=None)
    args = parser.parse_args()

    if args.db:
        db_path = args.db
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(db_path + suffix)
            except FileNotFoundError:
                pass
    else:
        handle, db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)

    inventory_doc = json.loads((MATERIALS / "inventory.json").read_text("utf-8"))
    operations_doc = json.loads((MATERIALS / "operations.json").read_text("utf-8"))

    conn = connect(db_path)
    initialize(conn)
    inventory = InventoryService(conn)
    inventory.initialize(inventory_doc)

    print("== operations ==")
    results = execute_operations(conn, operations_doc)
    for entry in results:
        op = entry["op"]
        result = entry["result"]
        if op == "reserve_competition":
            print(
                f"{op:18} winner={result['winners']} loser={result['losers']}"
            )
            for run_id, outcome in result["outcomes"].items():
                if not outcome["ok"]:
                    print(f"{'':18} {run_id}: {outcome['error_type']}: "
                          f"{outcome['error']}")
            continue
        if op in ("start", "repeat_start"):
            print(
                f"{op:18} {entry['run_id']:12} -> {result['state']:8} "
                f"day={result.get('started_day') or result.get('released_day')} "
                f"replayed={result.get('replayed', False)}"
            )
            if result.get("reason"):
                print(f"{'':18} reason: {result['reason']}")
            continue
        if op == "query":
            print(
                f"{op:18} {result['bottle_id']:5} "
                f"day={result.get('day', '?')} "
                f"unallocated={result['unallocated']} "
                f"leadable_today={result['leadable_today']} "
                f"opened_day={result['opened_day']}"
            )
            continue
        if op == "create":
            print(
                f"{op:18} {entry['run_id']:12} -> {result['state']:8} "
                f"revision={result['method_revision']} "
                f"requirements={result['requirements']}"
            )
            continue
        print(f"{op:18} {entry.get('run_id', '')} {result}")

    print("\n== final conservation ==")
    print(f"{'bottle':6} {'total':>5} {'unalloc':>7} {'reserved':>8} "
          f"{'consumed':>8}  total == unalloc+reserved+consumed")
    for row in inventory.conservation():
        lhs = row["total_portions"]
        rhs = row["unallocated"] + row["reserved"] + row["consumed"]
        print(
            f"{row['bottle_id']:6} {lhs:>5} {row['unallocated']:>7} "
            f"{row['reserved']:>8} {row['consumed']:>8}  "
            f"{lhs} == {row['unallocated']}+{row['reserved']}+"
            f"{row['consumed']} -> {'OK' if lhs == rhs else 'FAIL'}"
        )

    print("\n== day-13 / day-14 leadability of U ==")
    for day in (13, 14):
        u = inventory.query("U", day)
        print(
            f"day {day}: unallocated={u['unallocated']} "
            f"leadable_today={u['leadable_today']} "
            f"effective_expiry_day={u['effective_expiry_day']} "
            f"opened_day={u['opened_day']}"
        )
    p = inventory.query("P", 11)
    print(
        f"day 11 P: unallocated={p['unallocated']} reserved={p['reserved']} "
        f"consumed={p['consumed']} leadable_today={p['leadable_today']}"
    )
    conn.close()
    if not args.db:
        os.unlink(db_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
