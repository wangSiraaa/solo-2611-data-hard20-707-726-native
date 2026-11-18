#!/usr/bin/env python3
"""Showcase the required behaviours on the supplied diamond catalog.

Run after (or instead of) ``demo.py``; it uses its own throwaway database so
it is independent of any other run.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from cs.store import CatalogError, Store  # noqa: E402

CATALOG = json.loads((ROOT / "materials" / "721" / "catalog.json").read_text())
FACTS = [
    {"ticket_id": "T1", "revision": 1, "amount": 100, "classes": ["L"]},
    {"ticket_id": "T2", "revision": 1, "amount": 0, "classes": ["A"]},
    {"ticket_id": "T3", "revision": 1, "amount": 50, "classes": ["B"]},
    # Multi-label: directly in both A and B, so R counts it once.
    {"ticket_id": "T4", "revision": 1, "amount": 40, "classes": ["A", "B"]},
]


def group(result, target):
    return next(g for g in result["groups"] if g["target_id"] == target)


def show(title, result, targets=None):
    print(f"\n## {title}")
    print(f"cut: {result['cut']}")
    for target in targets or [g["target_id"] for g in result["groups"]]:
        g = group(result, target)
        tickets = ", ".join(
            f"{c['ticket_id']}={c['amount']}({'/'.join(c['path'])})"
            for c in g["contributions"]
        )
        print(f"  {target}: count={g['count']} amount={g['amount']} [{tickets}]")
    print(f"  UNION: count={result['union']['count']} amount={result['union']['amount']}")


def main() -> int:
    db_path = Path(tempfile.mktemp(suffix=".db"))
    try:
        with Store.open(str(db_path)) as store:
            store.init_catalog(CATALOG)
            store.apply_fact_revisions(FACTS, request_id="seed")

            print("# Initial diamond (multi-parent L, multi-label T4)")
            show("overlap A/B", store.query(["A", "B"], name="overlap"))
            print("\n  Multi-label same parent check: T4 is in both A and B;")
            print("  R must still count T4 once.")
            show("parent and child R/L", store.query(["R", "L"], name="parent-and-child"))

            print("\n  Same-name nodes: A and B both named '办件' but distinct ids:")
            state = store.load_catalog_state()
            print(f"    A.name={state.nodes['A']!r} B.name={state.nodes['B']!r}")
            show("distinct A and B", store.query(["A", "B"]))

            print("\n  Coverage explanation for target A, classes [A, L, B]:")
            explanation = store.explain_target("A", ["A", "L", "B"])
            for entry in explanation["entries"]:
                print(f"    {entry}")

            print("\n# B1: remove L->A and revise T1 to 120 atomically")
            b1 = store.apply_bundle(
                {
                    "remove_edges": [["L", "A"]],
                    "add_edges": [],
                    "fact_revisions": [
                        {"ticket_id": "T1", "revision": 2, "amount": 120, "classes": ["L"]}
                    ],
                },
                request_id="B1",
            )
            print(f"  committed cut: {b1.cut.as_dict()}")
            show("after B1", store.query(["A", "B"]), ["A", "B"])
            print(f"  union after B1: {store.query(['A', 'B'])['union']}")

            print("\n# B_BAD: add R->L (cycle) together with T2 amount 999")
            try:
                store.apply_bundle(
                    {
                        "add_edges": [["R", "L"]],
                        "remove_edges": [],
                        "fact_revisions": [
                            {"ticket_id": "T2", "revision": 2, "amount": 999, "classes": ["A"]}
                        ],
                    },
                    request_id="B_BAD",
                )
                print("  unexpectedly accepted")
            except CatalogError as exc:
                print(f"  rejected: {exc}")
            after_bad = store.query(["A", "B", "R"])
            all_amounts = {c["amount"] for g in after_bad["groups"] for c in g["contributions"]}
            print(f"  cut unchanged: {store.current_cut().as_dict()}")
            print(f"  999 present anywhere? {999 in all_amounts}")
            show("after rejected B_BAD", after_bad)

            print("\n# Withdraw T1 and re-open it with a higher revision")
            withdrawn = store.withdraw("T1", 2, request_id="W1")
            print(f"  withdrawn at cut {withdrawn.cut.as_dict()}")
            result = store.query(["B", "L"])
            print(f"  B after withdraw: {group(result, 'B')['count']} / {group(result, 'B')['amount']}")
            print(f"  L after withdraw: {group(result, 'L')['count']} / {group(result, 'L')['amount']}")
            reopened = store.apply_fact_revision("T1", 3, 70, ["L"], request_id="REOPEN")
            print(f"  reopened at revision 3: new cut {reopened.cut.as_dict()}")
            show("after reopen", store.query(["A", "B", "R", "L"]))

            print("\n# Recovering after a rejected cycle: legal edge edit accepted")
            fixed = store.apply_bundle(
                {
                    "remove_edges": [["L", "B"]],
                    "add_edges": [],
                    "fact_revisions": [],
                },
                request_id="FIX",
            )
            print(f"  accepted, catalog {fixed.catalog_revision}")
            show("final (L only under A)", store.query(["A", "B", "R"]))
        return 0
    finally:
        if db_path.exists():
            db_path.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
