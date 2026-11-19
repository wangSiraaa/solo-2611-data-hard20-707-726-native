from __future__ import annotations

import json
import sys
import unittest
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scale_calibration import Catalog, Calibrator, Store
from scale_calibration.calibration import (
    CalibrationInputError,
    InfeasibleError,
    parse_request,
)
from scale_calibration.catalog import (
    CatalogError,
    ConflictError,
    IllegalCodeError,
    NotFoundError,
)
from scale_calibration.rationals import (
    contains,
    fmt,
    intersection,
    normalize,
    parse_interval,
    parse_rational,
)

INPUT = json.loads(
    (Path(__file__).resolve().parents[1] / "materials/759/input.json").read_text()
)


def fresh():
    store = Store(":memory:")
    catalog = Catalog(store)
    for c in INPUT["scale_contracts"]:
        catalog.add_contract(**c)
    for r in INPUT["reads"]:
        catalog.add_read(**r)
    calibrator = Calibrator(store, catalog)
    return store, catalog, calibrator


def request_by_id(rid):
    return next(r for r in INPUT["requests"] if r["request_id"] == rid)


class RationalTests(unittest.TestCase):
    def test_parse_exact(self):
        self.assertEqual(parse_rational("3/4"), Fraction(3, 4))
        self.assertEqual(parse_rational("-3"), Fraction(-3))
        self.assertEqual(parse_rational("0.75"), Fraction(3, 4))
        self.assertEqual(parse_rational(5), Fraction(5))

    def test_reject_float_and_bool(self):
        with self.assertRaises(Exception):
            parse_rational(0.75)
        with self.assertRaises(Exception):
            parse_rational(True)

    def test_interval_validation(self):
        self.assertEqual(parse_interval(["0", "1/2"]), (Fraction(0), Fraction(1, 2)))
        with self.assertRaises(Exception):
            parse_interval(["1", "0"])

    def test_normalize_merges_touching_and_overlap(self):
        self.assertEqual(
            normalize([(Fraction(0), Fraction(1)), (Fraction(1), Fraction(2)),
                       (Fraction(3), Fraction(4))]),
            [(Fraction(0), Fraction(2)), (Fraction(3), Fraction(4))],
        )

    def test_intersection_and_contains(self):
        a = [(Fraction(0), Fraction(2))]
        b = [(Fraction(1), Fraction(3))]
        self.assertEqual(intersection(a, b), [(Fraction(1), Fraction(2))])
        self.assertTrue(contains([(Fraction(0), Fraction(1))], Fraction(1)))
        self.assertFalse(contains([(Fraction(0), Fraction(1))], Fraction(2)))

    def test_fmt(self):
        self.assertEqual(fmt(Fraction(3, 4)), "3/4")
        self.assertEqual(fmt(Fraction(4)), "4/1")


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.store, self.catalog, _ = fresh()

    def test_interpretations(self):
        self.assertEqual(self.catalog.interpret_read("A", 1)["position"], "3/1")
        self.assertEqual(self.catalog.interpret_read("B", 1)["position"], "7/1")
        self.assertEqual(self.catalog.interpret_read("B", 2)["position"], "8/1")
        self.assertEqual(self.catalog.interpret_read("AX", 1)["position"], "2/1")
        self.assertEqual(self.catalog.interpret_read("BX", 1)["position"], "8/1")

    def test_interpret_code_independent(self):
        self.assertEqual(self.catalog.interpret_code("K", 1, 6)["position"], "3/1")
        self.assertEqual(self.catalog.interpret_code("K", 2, 14)["position"], "8/1")

    def test_contract_idempotent_and_conflict(self):
        first = self.catalog.add_contract("K", 1, 0, 1, 2, 0, 24)
        self.assertFalse(first["created"])
        with self.assertRaises(ConflictError):
            self.catalog.add_contract("K", 1, 0, 1, 3, 0, 24)

    def test_contract_validation(self):
        with self.assertRaises(CatalogError):
            self.catalog.add_contract("Z", 1, 0, 0, 2, 0, 4)  # p must be positive
        with self.assertRaises(CatalogError):
            self.catalog.add_contract("Z", 1, 0, 1, 2, 5, 4)  # hi < lo

    def test_read_idempotent_and_conflict(self):
        again = self.catalog.add_read("A", 1, "P1", "K", 1, 6)
        self.assertFalse(again["created"])
        self.assertEqual(again["position"], "3/1")
        with self.assertRaises(ConflictError):
            self.catalog.add_read("A", 1, "P1", "K", 1, 7)

    def test_illegal_code_rejected(self):
        with self.assertRaises(IllegalCodeError):
            self.catalog.add_read("Z", 1, "P1", "K", 1, 25)
        with self.assertRaises(IllegalCodeError):
            self.catalog.interpret_code("K", 1, -1)

    def test_missing_contract_on_read(self):
        with self.assertRaises(NotFoundError):
            self.catalog.add_read("Z", 1, "P1", "K", 99, 1)

    def test_history_keeps_old_revision(self):
        history = self.catalog.list_read_revisions("B")
        self.assertEqual([h["revision"] for h in history], [1, 2])
        self.assertEqual(history[0]["position"], "7/1")
        self.assertEqual(history[1]["position"], "8/1")
        self.assertEqual(history[0]["scale_revision"], 1)
        self.assertEqual(history[1]["scale_revision"], 2)

    def test_old_value_survives_new_revision(self):
        self.catalog.add_read("B", 3, "P2", "K", 2, 20)
        self.assertEqual(self.catalog.interpret_read("B", 1)["position"], "7/1")
        self.assertEqual(self.catalog.interpret_read("B", 2)["position"], "8/1")
        self.assertEqual(self.catalog.interpret_read("B", 3)["position"], "11/1")


class CalibrationTests(unittest.TestCase):
    def setUp(self):
        self.store, self.catalog, self.calibrator = fresh()

    def test_G1_disjoint_no_common_shift(self):
        with self.assertRaises(InfeasibleError) as ctx:
            self.calibrator.submit(request_by_id("G1"))
        self.assertTrue(any("disjoint" in r for r in ctx.exception.reasons))

    def test_G2_common_shift_closed_interval(self):
        result = self.calibrator.submit(request_by_id("G2"))
        self.assertEqual(result["feasible_shifts"], [["3/4", "5/4"]])
        self.assertEqual(result["witness"]["shift"], "1/1")
        self.assertEqual(result["backup_used"], 0)
        corrected = {s["point"]: s["corrected"] for s in result["samples"]}
        self.assertEqual(corrected, {"P1": "4/1", "P2": "9/1"})

    def test_G3_rejected_by_shift_limit(self):
        with self.assertRaises(InfeasibleError) as ctx:
            self.calibrator.submit(request_by_id("G3"))
        self.assertTrue(
            any("allowed shift interval" in r for r in ctx.exception.reasons)
        )

    def test_G4_union_and_backup(self):
        result = self.calibrator.submit(request_by_id("G4"))
        self.assertEqual(
            result["feasible_shifts"], [["3/4", "5/4"], ["7/4", "9/4"]]
        )
        self.assertEqual(result["backup_used"], 1)
        roles = {s["point"]: s["role"] for s in result["samples"]}
        self.assertEqual(roles, {"P1": "PRIMARY", "P2": "BACKUP"})

    def test_G4_3_2_illegal(self):
        result = self.calibrator.submit(request_by_id("G4"))
        intervals = [(Fraction(a), Fraction(b)) for a, b in result["feasible_shifts"]]
        self.assertFalse(contains(intervals, Fraction(3, 2)))
        self.assertTrue(contains(intervals, Fraction(1)))
        self.assertTrue(contains(intervals, Fraction(2)))

    def test_replay_identical(self):
        first = self.calibrator.submit(request_by_id("G2"))
        again = self.calibrator.submit(request_by_id("G2"))
        self.assertFalse(first["replayed"])
        self.assertTrue(again["replayed"])
        self.assertEqual(first["feasible_shifts"], again["feasible_shifts"])
        self.assertEqual(first["samples"], again["samples"])

    def test_replay_survives_later_revision(self):
        before = self.calibrator.submit(request_by_id("G2"))
        # Publish a new revision of B and a new contract revision afterwards.
        self.catalog.add_contract("K", 3, 100, 1, 2, 0, 24)
        self.catalog.add_read("B", 3, "P2", "K", 3, 14)
        after = self.calibrator.submit(request_by_id("G2"))
        self.assertTrue(after["replayed"])
        self.assertEqual(before["product"] if "product" in before else before["samples"],
                         after["samples"])
        # The stored product still cites the original frozen sources.
        self.assertEqual(after["samples"][1]["source"]["scale_revision"], 2)

    def test_conflicting_resubmission_rejected(self):
        self.calibrator.submit(request_by_id("G2"))
        bad = json.loads(json.dumps(request_by_id("G2")))
        bad["backup_limit"] = 3
        with self.assertRaises(CalibrationInputError):
            self.calibrator.submit(bad)

    def test_no_product_left_after_infeasible(self):
        with self.assertRaises(InfeasibleError):
            self.calibrator.submit(request_by_id("G1"))
        self.assertIsNone(
            self.store.one(
                "SELECT 1 FROM calibration_requests WHERE request_id = 'G1'"
            )
        )
        with self.assertRaises(NotFoundError):
            self.calibrator.get_product("G1")
        self.assertEqual(
            self.store.one(
                "SELECT count(*) c FROM calibration_samples WHERE request_id='G1'"
            )["c"],
            0,
        )

    def test_input_error_unknown_point(self):
        bad = {
            "request_id": "X1",
            "reads": [["A", 1, "PRIMARY"]],
            "backup_limit": 0,
            "shift": ["-3", "3"],
            "references": {"P2": [9, "1/4"]},
        }
        with self.assertRaises(CalibrationInputError):
            self.calibrator.submit(bad)

    def test_input_error_missing_coverage(self):
        bad = {
            "request_id": "X2",
            "reads": [["A", 1, "PRIMARY"]],
            "backup_limit": 0,
            "shift": ["-3", "3"],
            "references": {"P1": [4, "1/4"], "P2": [9, "1/4"]},
        }
        with self.assertRaises(CalibrationInputError):
            self.calibrator.submit(bad)

    def test_input_error_missing_read_revision(self):
        bad = {
            "request_id": "X3",
            "reads": [["A", 1, "PRIMARY"], ["B", 99, "PRIMARY"]],
            "backup_limit": 0,
            "shift": ["-3", "3"],
            "references": {"P1": [4, "1/4"], "P2": [9, "1/4"]},
        }
        with self.assertRaises(CalibrationInputError):
            self.calibrator.submit(bad)

    def test_input_error_bad_role(self):
        with self.assertRaises(CalibrationInputError):
            parse_request(
                {
                    "request_id": "X4",
                    "reads": [["A", 1, "SPARE"]],
                    "backup_limit": 0,
                    "shift": ["-3", "3"],
                    "references": {"P1": [4, "1/4"]},
                }
            )

    def test_input_error_negative_epsilon(self):
        with self.assertRaises(CalibrationInputError):
            parse_request(
                {
                    "request_id": "X5",
                    "reads": [["A", 1, "PRIMARY"]],
                    "backup_limit": 0,
                    "shift": ["-3", "3"],
                    "references": {"P1": [4, "-1/4"]},
                }
            )


class FeasibilityEdgeTests(unittest.TestCase):
    """Constructed cases exercising the exact sweep, not the bundled input."""

    def _setup(self, contracts, reads):
        store = Store(":memory:")
        catalog = Catalog(store)
        for c in contracts:
            catalog.add_contract(*c)
        for r in reads:
            catalog.add_read(*r)
        return store, catalog, Calibrator(store, catalog)

    def test_isolated_single_point_not_lost(self):
        # P1 window = [0,1], P2 window = [1,2]; only the point s=1 works.
        # window = [ref-eps-pos, ref+eps-pos]
        store, cat, cal = self._setup(
            [("S", 1, 0, 1, 1, 0, 100)],
            [("a", 1, "P1", "S", 1, 0), ("b", 1, "P2", "S", 1, 1)],
        )
        req = {
            "request_id": "ISO",
            "reads": [["a", 1, "PRIMARY"], ["b", 1, "PRIMARY"]],
            "backup_limit": 0,
            "shift": ["-5", "5"],
            "references": {"P1": ["1/2", "1/2"], "P2": ["5/2", "1/2"]},
        }
        result = cal.submit(req)
        self.assertEqual(result["feasible_shifts"], [["1/1", "1/1"]])
        self.assertEqual(result["witness"]["shift"], "1/1")

    def test_tagged_union_two_separate_parts(self):
        # Each point can be read at position 0 or 10; both points must agree.
        # Window for pos 0 is [-1,1]; for pos 10 it is [-11,-9].
        store, cat, cal = self._setup(
            [("S", 1, 0, 1, 1, 0, 100)],
            [
                ("a0", 1, "P1", "S", 1, 0),   # pos 0
                ("a1", 1, "P1", "S", 1, 10),  # pos 10
                ("b0", 1, "P2", "S", 1, 0),   # pos 0
                ("b1", 1, "P2", "S", 1, 10),  # pos 10
            ],
        )
        req = {
            "request_id": "TWO",
            "reads": [
                ["a0", 1, "PRIMARY"], ["a1", 1, "PRIMARY"],
                ["b0", 1, "BACKUP"], ["b1", 1, "BACKUP"],
            ],
            "backup_limit": 1,
            "shift": ["-20", "20"],
            "references": {"P1": [0, "1"], "P2": [0, "1"]},
        }
        result = cal.submit(req)
        self.assertEqual(
            result["feasible_shifts"], [["-11/1", "-9/1"], ["-1/1", "1/1"]]
        )
        intervals = [(Fraction(a), Fraction(b)) for a, b in result["feasible_shifts"]]
        self.assertFalse(contains(intervals, Fraction(5)))

    def test_backup_budget_is_the_only_obstruction(self):
        # One point has only a BACKUP candidate; budget 0 must fail with budget reason.
        store, cat, cal = self._setup(
            [("S", 1, 0, 1, 1, 0, 100)],
            [("a", 1, "P1", "S", 1, 0), ("b", 1, "P2", "S", 1, 0)],
        )
        req = {
            "request_id": "BUD",
            "reads": [["a", 1, "PRIMARY"], ["b", 1, "BACKUP"]],
            "backup_limit": 0,
            "shift": ["-5", "5"],
            "references": {"P1": [0, "1"], "P2": [0, "1"]},
        }
        with self.assertRaises(InfeasibleError) as ctx:
            cal.submit(req)
        self.assertTrue(any("BACKUP" in r and "budget" in r for r in ctx.exception.reasons))

    def test_closed_endpoints_are_included(self):
        # Single point, epsilon 0 -> exactly one shift value, a closed point.
        store, cat, cal = self._setup(
            [("S", 1, 0, 1, 1, 0, 100)],
            [("a", 1, "P1", "S", 1, 3)],
        )
        req = {
            "request_id": "PT",
            "reads": [["a", 1, "PRIMARY"]],
            "backup_limit": 0,
            "shift": ["-5", "5"],
            "references": {"P1": [5, "0"]},
        }
        result = cal.submit(req)
        self.assertEqual(result["feasible_shifts"], [["2/1", "2/1"]])

    def test_exact_nonterminating_shift_window(self):
        # p/q = 1/3 -> windows have denominator 3, must stay exact.
        store, cat, cal = self._setup(
            [("S", 1, 0, 1, 3, 0, 100)],
            [("a", 1, "P1", "S", 1, 0)],
        )
        req = {
            "request_id": "THIRD",
            "reads": [["a", 1, "PRIMARY"]],
            "backup_limit": 0,
            "shift": ["-5", "5"],
            "references": {"P1": [Fraction(1, 3), "1/3"]},
        }
        result = cal.submit(req)
        self.assertEqual(result["feasible_shifts"], [["0/1", "2/3"]])
        # midpoint 1/3 must remain exactly representable / renderable
        self.assertEqual(result["witness"]["shift"], "1/3")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class ScaleAndPersistenceTests(unittest.TestCase):
    def _build_scale(self, n_points=20, n_cand=6):
        store = Store(":memory:")
        catalog = Catalog(store)
        catalog.add_contract("S", 1, 0, 1, 1, 0, 100000)
        reads = []
        refs = {}
        # Each point has a true base position; candidates straddle it.
        for i in range(n_points):
            point = f"P{i}"
            base_pos = 10 * i + 10
            refs[point] = [base_pos, 1]
            for c in range(n_cand):
                read_id = f"R{i}_{c}"
                # positions spread by up to +/-5 around base; code = position
                code = base_pos + (c - (n_cand // 2)) * 2
                catalog.add_read(read_id, 1, point, "S", 1, code)
        return store, catalog, refs

    def test_scale_100_requests(self):
        import time
        store, catalog, refs = self._build_scale()
        cal = Calibrator(store, catalog)
        start = time.time()
        ok = 0
        for r in range(100):
            reads = []
            for i in range(20):
                # use 6 candidates: middle-ish primaries and edge backups
                for c in range(6):
                    role = "PRIMARY" if c in (2, 3) else "BACKUP"
                    reads.append([f"R{i}_{c}", 1, role])
            req = {
                "request_id": f"SCALE{r}",
                "reads": reads,
                "backup_limit": 20,
                "shift": ["-100", "100"],
                "references": {p: [v[0], str(v[1])] for p, v in refs.items()},
            }
            result = cal.submit(req)
            self.assertEqual(result["status"], "FEASIBLE")
            ok += 1
        elapsed = time.time() - start
        self.assertEqual(ok, 100)
        # Sanity ceiling: this is a pure-Python exact-rational sweep.
        self.assertLess(elapsed, 30.0, f"scale run too slow: {elapsed:.2f}s")

    def test_persistence_file_db_and_replay_across_connections(self):
        import tempfile, os
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            store1 = Store(path)
            cat1 = Catalog(store1)
            for c in INPUT["scale_contracts"]:
                cat1.add_contract(**c)
            for r in INPUT["reads"]:
                cat1.add_read(**r)
            cal1 = Calibrator(store1, cat1)
            first = cal1.submit(request_by_id("G2"))
            store1.close()

            store2 = Store(path)
            cat2 = Catalog(store2)
            cal2 = Calibrator(store2, cat2)
            # Replay must work with no reads present in this fresh handle's view
            # of the catalog? Reads persist in the same DB, so they are there.
            again = cal2.submit(request_by_id("G2"))
            self.assertTrue(again["replayed"])
            self.assertEqual(first["feasible_shifts"], again["feasible_shifts"])
            # Publish later revisions and confirm replay is unchanged.
            cat2.add_contract("K", 9, 50, 1, 2, 0, 24)
            cat2.add_read("B", 9, "P2", "K", 9, 14)
            third = cal2.submit(request_by_id("G2"))
            self.assertTrue(third["replayed"])
            self.assertEqual(third["samples"], first["samples"])
            store2.close()
        finally:
            os.unlink(path)


class EnvelopeTrapTests(unittest.TestCase):
    """The classic error: intersect each point's widest envelope, not its windows.

    Point A's candidate windows are [-0.4,0.4] (pos 0) and [-10.4,-9.6]
    (pos 10), so its *widest envelope* is [-10.4, 0.4].  Point B's only window
    is [-5.4,-4.6].  The envelopes intersect near -5, so an envelope-only
    analysis would publish a shift there -- but neither of A's actual windows
    reaches it.  The engine must reject.
    """

    def test_no_fabricated_offset_from_widest_envelope(self):
        store = Store(":memory:")
        cat = Catalog(store)
        cat.add_contract("S", 1, 0, 1, 1, 0, 1000)
        for rid, point, code in [
            ("a0", "A", 0), ("a1", "A", 10),
            ("b0", "B", 5),
        ]:
            cat.add_read(rid, 1, point, "S", 1, code)
        cal = Calibrator(store, cat)
        req = {
            "request_id": "ENV",
            "reads": [["a0", 1, "PRIMARY"], ["a1", 1, "BACKUP"], ["b0", 1, "PRIMARY"]],
            "backup_limit": 1,
            "shift": ["-20", "20"],
            "references": {"A": [0, "0.4"], "B": [0, "0.4"]},
        }
        with self.assertRaises(InfeasibleError) as ctx:
            cal.submit(req)
        self.assertTrue(any("disjoint" in r for r in ctx.exception.reasons))
        # And no product/row was left behind.
        self.assertIsNone(
            store.one("SELECT 1 FROM calibration_requests WHERE request_id='ENV'")
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
