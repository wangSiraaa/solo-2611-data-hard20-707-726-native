"""Run the bundled 715 scenario end-to-end and print a human-readable trace.

Usage::

    python -m obs_service.demo                 # in-memory
    python -m obs_service.demo --db /tmp/x.db   # persists, then reopens to read old report

The demo performs exactly the operations in ``materials/715/operations.json``
against ``materials/715/observations.json`` and asserts the exercise's stated
results.  It is illustrative only; the test-suite exercises many more cases.
"""

from __future__ import annotations

import argparse
import json
import os

from .api import ObservationService
from .semantics import SemanticsError

MATERIALS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "materials", "715")


def _load(name: str) -> dict:
    with open(os.path.join(MATERIALS, name), encoding="utf-8") as f:
        return json.load(f)


def run(db_path: str = ":memory:") -> dict:
    print("== import observations.json ==")
    svc = ObservationService(db_path)
    print(json.dumps(svc.import_dataset(_load("observations.json")),
                     indent=2, ensure_ascii=False))

    print("\n== r1 (threshold rev 1 = 5) ==")
    r1 = svc.create_report("r1", "b1", "p", 1)
    _show_counts_and_avg(r1)
    assert r1["counts"] == {
        "total_samples": 4, "valid_samples": 3, "definitely_exceeds": 1,
        "possibly_exceeds": 1, "uncertain": 0, "missing": 1,
    }, r1["counts"]
    avg1 = r1["average"]
    assert avg1["range_display"] == "(10/3,+inf)"
    assert avg1["lower_attainable"] is False and avg1["upper_unbounded"] is True
    print("  average derivation:", json.dumps(svc.explain("r1")["derivation"], indent=2))
    print("  s3 contribution:", json.dumps(svc.explain("r1", "s3")["sample"], indent=2))

    print("\n== operations: correct s3 -> 7, threshold rev 2 = 6, report r2 ==")
    ops = _load("operations.json")
    svc.store.import_measurements("b1", "p", ops["operations"][1]["records"])
    svc.register_threshold("p", 2, ops["operations"][2]["value"])
    r2 = svc.create_report("r2", "b1", "p", 2)
    _show_counts_and_avg(r2)
    assert r2["counts"] == {
        "total_samples": 4, "valid_samples": 3, "definitely_exceeds": 1,
        "possibly_exceeds": 1, "uncertain": 0, "missing": 1,
    }, r2["counts"]
    avg2 = r2["average"]
    assert avg2["range_display"] == "[4,17/3)"
    assert avg2["lower_attainable"] is True and avg2["upper_attainable"] is False
    assert avg2["unique"] is False
    print("  average derivation:", json.dumps(svc.explain("r2")["derivation"], indent=2))
    print("  s3 contribution:", json.dumps(svc.explain("r2", "s3")["sample"], indent=2))

    print("\n== old r1 still bound to its own snapshot ==")
    r1_again = svc.read_report("r1")
    assert r1_again["average"]["range_display"] == "(10/3,+inf)"
    assert r1_again["threshold"]["value"] == "5"
    assert r1_again["superseded"] is True
    print("  r1 threshold:", r1_again["threshold"], " avg:", r1_again["average"]["range_display"])

    print("\n== illegal import LT(0) + valid correction; whole batch must be rejected ==")
    try:
        svc.import_measurements("b1", "p", ops["operations"][5]["records"])
        raise AssertionError("expected rejection")
    except SemanticsError as exc:
        print("  rejected as expected:", exc)
    s2_hist = svc.revision_history("b1", "p", "s2")
    assert [h["revision"] for h in s2_hist] == [1], s2_hist
    print("  s2 history unchanged:", s2_hist)

    print("\n== semantic queries ==")
    print("  normalize LT(5):", svc.normalize({"kind": "LT", "upper": "5"}))
    print("  intersect [0,5) & GE(4):",
          svc.intersect({"kind": "LT", "upper": "5"}, {"kind": "GE", "lower": "4"}))
    print("  check GT(5) vs T=5:", svc.threshold_check({"kind": "GT", "lower": "5"}, "5"))
    print("  check EXACT(5) vs T=5:", svc.threshold_check({"kind": "EXACT", "value": "5"}, "5"))

    if db_path != ":memory:":
        print("\n== close & reopen: old report unchanged ==")
        svc.close()
        svc = ObservationService(db_path)
        r1_reopened = svc.read_report("r1")
        assert r1_reopened["average"]["range_display"] == "[10/3,+inf)"
        assert r1_reopened["threshold"]["value"] == "5"
        print("  r1 after reopen:", r1_reopened["average"]["range_display"],
              "threshold", r1_reopened["threshold"]["value"])
    svc.close()
    print("\nAll demo assertions passed.")
    return {"r1": r1, "r2": r2}


def _show_counts_and_avg(report: dict) -> None:
    print("  counts:", json.dumps(report["counts"], ensure_ascii=False))
    a = report["average"]
    print(f"  average: {a['range_display']}  n={a['n']}  unique={a['unique']}  "
          f"lower_attainable={a['lower_attainable']}  upper_attainable={a['upper_attainable']}")
    for c in report["contributions"]:
        if c["is_missing"]:
            print(f"    {c['sample_id']} rev{c['adopted_revision']}: MISSING (excluded)")
        else:
            print(f"    {c['sample_id']} rev{c['adopted_revision']}: {c['range_display']} "
                  f"-> {c['status']}, denom={c['denominator']}, "
                  f"lo={c['lower_contribution']}, hi={c['upper_contribution']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=":memory:")
    args = ap.parse_args(argv)
    run(args.db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
