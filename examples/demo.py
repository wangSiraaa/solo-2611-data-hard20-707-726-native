#!/usr/bin/env python3
"""End-to-end demonstration of the gradaudit library.

Runs against the real materials in ``materials/725`` and a throw-away SQLite
database. Shows, in order:

  1. catalog inspection (candidates + categories, never auto-chosen)
  2. two *different* valid plans both accepted by the independent verifier
  3. the record-reuse plan rejected with a located reason (R1)
  4. groups competing for one record; single-group reachable vs overall not
  5. multi-candidate records and unused records
  6. solver output re-verified by the same verify entry point
  7. revision 2 changes feasibility; old revision remains queryable
  8. prepare / confirm / stale / reopen lifecycle with full history

Usage:  PYTHONPATH=. python3 examples/demo.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from gradaudit import (
    RevisionConflictError,
    Store,
    parse_catalog,
    parse_records,
    parse_requirements,
    solve,
    verify_plan_document,
)

MAT = os.path.join(ROOT, "materials", "725")


def load(name):
    with open(os.path.join(MAT, name), encoding="utf-8") as fh:
        return json.load(fh)


def banner(text):
    print("\n" + "=" * 74)
    print(text)
    print("=" * 74)


def main() -> int:
    cat1 = parse_catalog(load("catalog.json"))
    cat2 = parse_catalog(load("catalog_revision2.json"))
    records = parse_records(load("records.json"))
    requirements = parse_requirements(load("requirements.json"))

    banner("1. Catalog: candidates are explicit, categories are listed")
    for ext in ("X", "Y", "Z"):
        print(f"  {ext} -> {cat1.candidates_with_categories(ext)}")
    print(f"  'LC_CORE' (not an external code) -> {cat1.candidates('LC_CORE')}")

    banner("2. Two different valid plans (plan_a / plan_b) are BOTH accepted")
    for name in ("plan_a.json", "plan_b.json"):
        res = verify_plan_document(load(name), cat1, records, requirements)
        print(f"  {name}: accepted={res.accepted} totals={res.group_totals}")

    banner("3. plan_invalid_reuse reuses R1 -> rejected, reason points at R1")
    res = verify_plan_document(load("plan_invalid_reuse.json"), cat1, records, requirements)
    print(f"  accepted={res.accepted}")
    for r in res.reasons:
        print(f"    - [{r['code']}] {r.get('location')}: {r['message']}")

    banner("4. Two groups competing for one record (shared external course)")
    shared_catalog = parse_catalog({
        "format": "course-catalog/1", "revision": 1,
        "courses": [{"course_id": "CAB", "categories": ["A", "B"]}],
        "equivalences": {"SHARED": ["CAB"]},
    })
    shared_records = parse_records({"student_id": "S", "revision": 1, "records": [
        {"record_id": "R1", "external_course": "SHARED", "points": 3},
    ]})
    shared_req = parse_requirements({"request_id": "Q", "revision": 1, "groups": [
        {"group_id": "GA", "allowed_categories": ["A"], "minimum_points": 3},
        {"group_id": "GB", "allowed_categories": ["B"], "minimum_points": 3},
    ]})
    out = solve(shared_catalog, shared_records, shared_req)
    print(f"  feasible={out.feasible}")
    for g in out.groups:
        print(f"    {g.group_id}: alone max={g.max_ignoring_other_groups} "
              f"(minimum {g.minimum_points}, reachable alone={g.satisfied})")
    print("  -> each group is reachable alone, but R1 cannot serve both.")

    banner("5. Multi-candidate + unused records (solver picks one exact allocation)")
    mc = parse_catalog({
        "format": "course-catalog/1", "revision": 1,
        "courses": [
            {"course_id": "A1", "categories": ["A"]},
            {"course_id": "A2", "categories": ["A"]},
            {"course_id": "B1", "categories": ["B"]},
            {"course_id": "AB", "categories": ["A", "B"]},
        ],
        "equivalences": {"EA": ["A1", "A2", "AB"], "EB": ["B1", "AB"], "EX": ["A1"]},
    })
    mc_rec = parse_records({"student_id": "S", "revision": 1, "records": [
        {"record_id": "R1", "external_course": "EA", "points": 2},
        {"record_id": "R2", "external_course": "EB", "points": 2},
        {"record_id": "R3", "external_course": "EA", "points": 2},
        {"record_id": "R4", "external_course": "EX", "points": 2},
    ]})
    mc_req = parse_requirements({"request_id": "Q", "revision": 1, "groups": [
        {"group_id": "GA", "allowed_categories": ["A"], "minimum_points": 4},
        {"group_id": "GB", "allowed_categories": ["B"], "minimum_points": 2},
    ]})
    mc_out = solve(mc, mc_rec, mc_req)
    print(f"  feasible={mc_out.feasible} states={mc_out.states_explored}")
    for a in mc_out.assignments:
        print(f"    {a.record_id} -> {a.course_id} ({a.group_id})")
    used = {a.record_id for a in mc_out.assignments}
    print(f"  unused records: {sorted(set(mc_rec.records) - used) or 'none'}")
    print(f"  re-verified by verify entry point: "
          f"{verify_plan_document(mc_out.as_plan_document(), mc, mc_rec, mc_req).accepted}")

    banner("6. Solver output accepted by the same verify entry point")
    out1 = solve(cat1, records, requirements)
    v = verify_plan_document(out1.as_plan_document(), cat1, records, requirements)
    print(f"  rev1 solver feasible={out1.feasible}, verify accepted={v.accepted}")

    banner("7. Mapping revision 2 changes feasibility")
    out2 = solve(cat2, records, requirements)
    print(f"  rev2 solver feasible={out2.feasible}")
    for g in out2.groups:
        print(f"    {g.group_id}: alone max={g.max_ignoring_other_groups}")
    print("  -> OPT can only reach 2 points now; the old revision stays queryable.")

    banner("8. Library lifecycle: import -> prepare -> confirm -> stale -> reopen")
    db_path = os.path.join(tempfile.gettempdir(), "gradaudit_demo.sqlite3")
    if os.path.exists(db_path):
        os.remove(db_path)
    with Store(db_path) as store:
        store.import_catalog(load("catalog.json"))
        store.import_records(load("records.json"))
        store.import_requirements(load("requirements.json"))
        print("  candidates rev1 X:", store.list_candidates(1, "X"))

        prep = store.prepare_audit(
            load("plan_a.json"), request_id="REQ1",
            catalog_revision=1, records_revision=1, requirements_revision=1,
        )
        print("  prepared accepted:", prep["accepted"])
        print("  point destinations:",
              [(p["record_id"], p["course_id"], p["group_id"], p["points"])
               for p in store.preparation_points(prep["preparation_id"])])

        store.confirm_audit(request_id="REQ1")
        print("  status:", store.confirmation_status("REQ1")["confirmed"])

        try:
            store.import_catalog(load("catalog_revision2.json"))
            print("  imported catalog revision 2")
        except RevisionConflictError as exc:
            print("  unexpected conflict:", exc)

        status = store.confirmation_status("REQ1")
        print("  after rev2 -> confirmed:", status["confirmed"], "stale:", status["stale"])

        try:
            store.confirm_audit(request_id="REQ1", preparation_id=prep["preparation_id"])
        except Exception as exc:
            print(f"  re-confirm old preparation blocked: {getattr(exc, 'code', type(exc).__name__)}")

        store.reopen_audit(request_id="REQ1", reason="mapping revised")
        hist = store.history("REQ1")
        print("  history kinds:", [h["kind"] for h in hist])
        print("  confirmations retained:", len(store.list_confirmations("REQ1")))

    print("\nOK: all demonstrations completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
