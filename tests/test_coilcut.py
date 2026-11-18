"""End-to-end tests for the coil cutting geometry service and ledger.

Run with:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from fractions import Fraction
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from coilcut import (  # noqa: E402
    ConflictError,
    DuplicatePieceIdError,
    InvalidKerfError,
    Ledger,
    LengthMismatchError,
    OutOfBoundsError,
    PieceConsumedError,
    PieceGeometry,
    StaleOrientationError,
    StaleRevisionError,
    ZeroLengthPieceError,
    cut_geometry,
    flip_geometry,
)
from coilcut.operations import (  # noqa: E402
    initialize_from_stock,
    load_stock,
    run_operations,
    run_operations_file,
)

STOCK = ROOT_DIR / "materials" / "731" / "stock.json"
OPS = ROOT_DIR / "materials" / "731" / "operations.jsonl"
READER = ROOT_DIR / "materials" / "731" / "delivery_reader.py"


class TempLedger(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self._tmp.name) / "ledger.db")
        self.ledger = Ledger(self.db)
        initialize_from_stock(self.ledger, load_stock(STOCK))

    def tearDown(self) -> None:
        try:
            self.ledger.close()
        finally:
            self._tmp.cleanup()


class GeometryTests(unittest.TestCase):
    def test_c1_then_flip_then_c2_matches_materials(self) -> None:
        p0 = PieceGeometry("P0", "ROOT", 0, 100, 1)
        c1 = cut_geometry(p0, 30, 2, "L1", "R1", root_length=100)
        self.assertEqual((c1.left.root_lo, c1.left.root_hi), (0, 30))
        self.assertEqual((c1.right.root_lo, c1.right.root_hi), (32, 100))
        self.assertEqual((c1.kerf_interval.lo, c1.kerf_interval.hi), (30, 32))

        r1 = flip_geometry(c1.right)
        self.assertEqual(r1.orientation, -1)
        c2 = cut_geometry(r1, 20, 2, "L2", "R2", root_length=100)
        # u=20 from the -1 start (root hi=100) lands at root 80, NOT 52.
        self.assertEqual((c2.left.root_lo, c2.left.root_hi), (80, 100))
        self.assertEqual(c2.left.length, 20)
        self.assertEqual(c2.left.orientation, -1)
        self.assertEqual((c2.right.root_lo, c2.right.root_hi), (32, 78))
        self.assertEqual(c2.right.length, 46)
        self.assertEqual(c2.right.orientation, -1)
        self.assertEqual((c2.kerf_interval.lo, c2.kerf_interval.hi), (78, 80))

    def test_orientation_plus_one_local_mapping(self) -> None:
        p = PieceGeometry("P", "R", 10, 50, 1)
        c = cut_geometry(p, 10, 2, "A", "B")
        self.assertEqual((c.left.root_lo, c.left.root_hi), (10, 20))
        self.assertEqual((c.right.root_lo, c.right.root_hi), (22, 50))

    def test_geometry_is_stateless_and_does_not_drop_root_source(self) -> None:
        p = PieceGeometry("P", "R", 0, 100, -1)
        flipped = flip_geometry(p)
        self.assertEqual(
            (flipped.root_lo, flipped.root_hi, flipped.orientation), (0, 100, 1)
        )
        self.assertEqual((p.orientation, flipped.orientation), (-1, 1))

    def test_exact_rational_lengths(self) -> None:
        p = PieceGeometry("P", "R", 0, 1, 1)
        c = cut_geometry(p, Fraction(1, 3), Fraction(1, 7), "A", "B")
        self.assertEqual(c.left.length, Fraction(1, 3))
        self.assertEqual(c.right.length, 1 - Fraction(1, 3) - Fraction(1, 7))
        self.assertEqual(
            c.left.length + c.kerf + c.right.length, Fraction(1)
        )

    def test_kerf_out_of_bounds(self) -> None:
        p = PieceGeometry("P", "R", 0, 10, 1)
        with self.assertRaises(OutOfBoundsError):
            cut_geometry(p, 9, 2, "A", "B")
        with self.assertRaises(OutOfBoundsError):
            cut_geometry(p, -1, 2, "A", "B")

    def test_zero_length_children(self) -> None:
        p = PieceGeometry("P", "R", 0, 10, 1)
        with self.assertRaises(ZeroLengthPieceError):
            cut_geometry(p, 0, 2, "A", "B")
        with self.assertRaises(ZeroLengthPieceError):
            cut_geometry(p, 8, 2, "A", "B")

    def test_non_positive_kerf(self) -> None:
        p = PieceGeometry("P", "R", 0, 10, 1)
        with self.assertRaises(InvalidKerfError):
            cut_geometry(p, 5, 0, "A", "B")
        with self.assertRaises(InvalidKerfError):
            cut_geometry(p, 5, -2, "A", "B")


class CutLedgerTests(TempLedger):
    def test_operations_produce_true_d1(self) -> None:
        results = run_operations_file(self.ledger, OPS)
        c1 = results[0]["result"]
        flip = results[1]["result"]
        c2 = results[2]["result"]
        d1 = results[3]["result"]
        replay = results[4]["result"]

        self.assertEqual(c2["left"]["piece_id"], "L2")
        self.assertEqual((c2["left"]["root_lo"], c2["left"]["root_hi"]), (80, 100))
        self.assertEqual(c2["left"]["orientation"], -1)
        self.assertEqual((c2["right"]["root_lo"], c2["right"]["root_hi"]), (32, 78))
        self.assertEqual(flip["revision"], 2)

        manifest = d1["manifest"]
        self.assertEqual(manifest["schema"], "piece-delivery/v1")
        self.assertEqual(manifest["piece_id"], "L2")
        self.assertEqual((manifest["root_lo"], manifest["root_hi"]), (80, 100))
        self.assertEqual(manifest["orientation"], -1)
        self.assertEqual(manifest["length"], 20)
        self.assertNotEqual((manifest["root_lo"], manifest["root_hi"]), (32, 52))

        self.assertEqual(replay, c2)

        report = self.ledger.root_report("ROOT")
        self.assertEqual(report["totals"], {
            "active": 76, "delivered": 20, "kerf": 4, "sum": 100,
        })

    def test_delivery_is_consumable_by_provided_reader(self) -> None:
        run_operations_file(self.ledger, OPS)
        manifest = self.ledger.deliveries()[0]["manifest"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "D1.json"
            path.write_text(json.dumps(manifest))
            out = subprocess.run(
                [sys.executable, str(READER), str(path)],
                capture_output=True, text=True, check=True,
            )
        self.assertEqual(out.stdout.strip(), "D1 L2 ROOT 80 100 -1 20")

    def test_cut_requires_expected_revision(self) -> None:
        with self.assertRaises(Exception):
            self.ledger.cut(
                cut_id="X", piece_id="P0", expected_revision=None,
                u=30, left_child="A", right_child="B",
            )

    def test_stale_revision_rejected_without_side_effects(self) -> None:
        with self.assertRaises(StaleRevisionError):
            self.ledger.cut(
                cut_id="C_BAD", piece_id="P0", expected_revision=99,
                u=30, left_child="A", right_child="B",
            )
        self.assertEqual(self.ledger.get_piece("P0")["status"], "active")
        self.assertIsNone(
            self.ledger._conn.execute("SELECT * FROM cuts WHERE cut_id='C_BAD'").fetchone()
        )
        with self.assertRaises(Exception):
            self.ledger.get_piece("A")

    def test_geometry_failure_does_not_allocate_anything(self) -> None:
        with self.assertRaises(OutOfBoundsError):
            self.ledger.cut(
                cut_id="C_BAD", piece_id="P0", expected_revision=1,
                u=99, left_child="A", right_child="B",
            )
        self.assertEqual(self.ledger.get_piece("P0")["status"], "active")
        with self.assertRaises(Exception):
            self.ledger.get_piece("A")
        with self.assertRaises(Exception):
            self.ledger.replay_cut("C_BAD")

    def test_zero_length_child_rejected(self) -> None:
        with self.assertRaises(ZeroLengthPieceError):
            self.ledger.cut(
                cut_id="C0", piece_id="P0", expected_revision=1,
                u=0, left_child="A", right_child="B",
            )

    def test_duplicate_cut_same_content_replays(self) -> None:
        first = self.ledger.cut(
            cut_id="C1", piece_id="P0", expected_revision=1,
            u=30, left_child="L1", right_child="R1",
        )
        again = self.ledger.cut(
            cut_id="C1", piece_id="P0", expected_revision=1,
            u=30, left_child="L1", right_child="R1",
        )
        self.assertEqual(first, again)

    def test_duplicate_cut_different_content_conflicts(self) -> None:
        self.ledger.cut(
            cut_id="C1", piece_id="P0", expected_revision=1,
            u=30, left_child="L1", right_child="R1",
        )
        before = self.ledger.root_report("ROOT")
        with self.assertRaises(ConflictError):
            self.ledger.cut(
                cut_id="C1", piece_id="P0", expected_revision=1,
                u=20, left_child="X", right_child="Y",
            )
        self.assertEqual(self.ledger.root_report("ROOT"), before)

    def test_failed_cut_does_not_occupy_success_identity(self) -> None:
        with self.assertRaises(OutOfBoundsError):
            self.ledger.cut(
                cut_id="C1", piece_id="P0", expected_revision=1,
                u=99, left_child="L1", right_child="R1",
            )
        # The same id is still free for a valid cut.
        result = self.ledger.cut(
            cut_id="C1", piece_id="P0", expected_revision=1,
            u=30, left_child="L1", right_child="R1",
        )
        self.assertEqual(result["cut_id"], "C1")

    def test_two_connections_cannot_consume_same_parent(self) -> None:
        self.ledger.cut(
            cut_id="C1", piece_id="P0", expected_revision=1,
            u=30, left_child="L1", right_child="R1",
        )
        with self.assertRaises(PieceConsumedError):
            self.ledger.cut(
                cut_id="C2", piece_id="P0", expected_revision=1,
                u=40, left_child="L2x", right_child="R2x",
            )
        # Also via a stale revision on a retired piece.
        with self.assertRaises(PieceConsumedError):
            self.ledger.cut(
                cut_id="C3", piece_id="P0", expected_revision=2,
                u=40, left_child="L3x", right_child="R3x",
            )

    def test_concurrent_cut_single_winner(self) -> None:
        other = Ledger(self.db)
        results: list[tuple[str, str]] = []
        barrier = threading.Barrier(2)

        def attempt(ledger: Ledger, tag: str) -> None:
            barrier.wait()
            try:
                ledger.cut(
                    cut_id=f"C_{tag}", piece_id="P0", expected_revision=1,
                    u=30, left_child=f"L_{tag}", right_child=f"R_{tag}",
                )
                results.append((tag, "ok"))
            except Exception as exc:  # noqa: BLE001 - record the losing outcome
                results.append((tag, type(exc).__name__))

        threads = [
            threading.Thread(target=attempt, args=(self.ledger, "A")),
            threading.Thread(target=attempt, args=(other, "B")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        other.close()

        winners = [r for r in results if r[1] == "ok"]
        self.assertEqual(len(winners), 1)
        report = self.ledger.root_report("ROOT")
        self.assertEqual(report["totals"]["sum"], 100)

    def test_duplicate_piece_id_rejected(self) -> None:
        self.ledger.cut(
            cut_id="C1", piece_id="P0", expected_revision=1,
            u=30, left_child="L1", right_child="R1",
        )
        with self.assertRaises(DuplicatePieceIdError):
            self.ledger.cut(
                cut_id="C2", piece_id="L1", expected_revision=1,
                u=10, left_child="X", right_child="R1",
            )
        self.assertEqual(self.ledger.get_piece("L1")["status"], "active")


class FlipTests(TempLedger):
    def test_flip_requires_active(self) -> None:
        self.ledger.deliver(delivery_id="D", piece_id="P0", required_length=100)
        with self.assertRaises(PieceConsumedError):
            self.ledger.flip("P0", expected_revision=1)

    def test_flip_rejects_stale_revision(self) -> None:
        self.ledger.flip("P0", expected_revision=1)
        with self.assertRaises(StaleRevisionError):
            self.ledger.flip("P0", expected_revision=1)

    def test_stale_preview_cannot_commit_after_flip(self) -> None:
        preview = self.ledger.preview_cut(
            piece_id="P0", u=30, left_child="L1", right_child="R1"
        )
        self.ledger.flip("P0", expected_revision=1)
        with self.assertRaises(StaleRevisionError):
            self.ledger.commit_preview("C1", preview)
        # Nothing consumed, nothing allocated.
        self.assertEqual(self.ledger.get_piece("P0")["status"], "active")
        with self.assertRaises(Exception):
            self.ledger.get_piece("L1")

    def test_stale_orientation_guard(self) -> None:
        with self.assertRaises(StaleOrientationError):
            self.ledger.cut(
                cut_id="C1", piece_id="P0", expected_revision=1,
                expected_orientation=-1,
                u=30, left_child="L1", right_child="R1",
            )

    def test_flip_preserves_root_source(self) -> None:
        self.ledger.cut(
            cut_id="C1", piece_id="P0", expected_revision=1,
            u=30, left_child="L1", right_child="R1",
        )
        flipped = self.ledger.flip("R1", expected_revision=1)
        self.assertEqual((flipped["root_lo"], flipped["root_hi"]), (32, 100))
        self.assertEqual(flipped["orientation"], -1)
        self.assertEqual(flipped["revision"], 2)


class DeliveryTests(TempLedger):
    def _cut_and_flip(self) -> None:
        run_operations(self.ledger, [
            {"op": "cut", "cut_id": "C1", "piece_id": "P0", "expected_revision": 1,
             "u": 30, "left_child": "L1", "right_child": "R1"},
            {"op": "flip", "piece_id": "R1", "expected_revision": 1},
            {"op": "cut", "cut_id": "C2", "piece_id": "R1", "expected_revision": 2,
             "u": 20, "left_child": "L2", "right_child": "R2"},
        ])

    def test_delivery_freezes_piece(self) -> None:
        self._cut_and_flip()
        self.ledger.deliver(delivery_id="D1", piece_id="L2", required_length=20)
        with self.assertRaises(PieceConsumedError):
            self.ledger.flip("L2", expected_revision=1)
        with self.assertRaises(PieceConsumedError):
            self.ledger.cut(
                cut_id="C3", piece_id="L2", expected_revision=1,
                u=5, left_child="X", right_child="Y",
            )
        with self.assertRaises(PieceConsumedError):
            self.ledger.deliver(delivery_id="D2", piece_id="L2", required_length=20)

    def test_length_must_match(self) -> None:
        with self.assertRaises(LengthMismatchError):
            self.ledger.deliver(delivery_id="D1", piece_id="P0", required_length=99)
        self.assertEqual(self.ledger.get_piece("P0")["status"], "active")

    def test_duplicate_delivery_same_content_replays(self) -> None:
        first = self.ledger.deliver(delivery_id="D1", piece_id="P0", required_length=100)
        again = self.ledger.deliver(delivery_id="D1", piece_id="P0", required_length=100)
        self.assertEqual(first, again)

    def test_duplicate_delivery_different_content_conflicts(self) -> None:
        self.ledger.deliver(delivery_id="D1", piece_id="P0", required_length=100)
        with self.assertRaises(ConflictError):
            self.ledger.deliver(delivery_id="D1", piece_id="P0", required_length=50)

    def test_manifest_keeps_source_and_orientation(self) -> None:
        self._cut_and_flip()
        result = self.ledger.deliver(delivery_id="D1", piece_id="L2", required_length=20)
        manifest = result["manifest"]
        self.assertEqual(
            {k: manifest[k] for k in ("root_lo", "root_hi", "orientation")},
            {"root_lo": 80, "root_hi": 100, "orientation": -1},
        )
        self.assertEqual(
            set(manifest),
            {"schema", "delivery_id", "piece_id", "root_id", "root_lo",
             "root_hi", "orientation", "length"},
        )


class AccountingTests(TempLedger):
    def test_full_partition_after_operations(self) -> None:
        run_operations_file(self.ledger, OPS)
        report = self.ledger.root_report("ROOT")
        self.assertEqual(report["totals"]["sum"], 100)
        self.assertEqual(report["totals"]["kerf"], 4)
        self.assertEqual(report["totals"]["delivered"], 20)
        self.assertEqual(report["totals"]["active"], 76)

    def test_retired_parent_not_double_counted(self) -> None:
        self.ledger.cut(
            cut_id="C1", piece_id="P0", expected_revision=1,
            u=30, left_child="L1", right_child="R1",
        )
        report = self.ledger.root_report("ROOT")
        listed = {p["piece_id"] for p in report["active"]} | {
            p["piece_id"] for p in report["delivered"]
        }
        self.assertNotIn("P0", listed)
        self.assertEqual(report["totals"]["sum"], 100)

    def test_lineage_explains_children_and_kerf(self) -> None:
        run_operations_file(self.ledger, OPS)
        lineage = self.ledger.lineage("L2")
        self.assertEqual(lineage["piece"]["piece_id"], "L2")
        first = lineage["cut_ancestry"][0]
        self.assertEqual(first["cut_id"], "C2")
        self.assertEqual(first["side"], "left")
        self.assertEqual(first["sibling_piece_id"], "R2")
        self.assertEqual(first["parent_piece_id"], "R1")
        self.assertEqual(first["kerf_interval"]["root_lo"], 78)
        second = lineage["cut_ancestry"][1]
        self.assertEqual(second["cut_id"], "C1")
        self.assertEqual(second["parent_piece_id"], "P0")
        self.assertEqual(first["parent_flips"][0]["new_orientation"], -1)
        self.assertEqual(first["parent_orientation_at_cut"], -1)
        self.assertEqual(lineage["delivered_as"], "D1")

    def test_reports_reject_overlap(self) -> None:
        self.ledger.cut(
            cut_id="C1", piece_id="P0", expected_revision=1,
            u=30, left_child="L1", right_child="R1",
        )
        # Corrupt the ledger directly to prove the coverage check catches it.
        self.ledger._conn.execute(
            "INSERT INTO pieces(piece_id, root_id, root_lo, root_hi, orientation, revision, status)"
            " VALUES ('GHOST', 'ROOT', '10/1', '20/1', 1, 1, 'active')"
        )
        with self.assertRaises(Exception):
            self.ledger.root_report("ROOT")

    def test_reopen_preserves_history(self) -> None:
        run_operations_file(self.ledger, OPS)
        report_before = self.ledger.root_report("ROOT")
        deliveries_before = self.ledger.deliveries()
        self.ledger.close()
        self.ledger = Ledger(self.db)
        self.assertEqual(self.ledger.root_report("ROOT"), report_before)
        self.assertEqual(self.ledger.deliveries(), deliveries_before)
        # Replay still works after reopening.
        self.assertEqual(
            self.ledger.replay_cut("C2")["left"]["root_lo"], 80
        )


class RationalLedgerTests(unittest.TestCase):
    def test_fractional_lengths_close_the_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(str(Path(tmp) / "r.db"))
            try:
                ledger.initialize(
                    roots=[{"root_id": "R", "length": "10/1"}],
                    active_pieces=[{
                        "piece_id": "P", "root_id": "R", "root_lo": "0/1",
                        "root_hi": "10/1", "orientation": 1, "revision": 1,
                    }],
                    kerf="1/10",
                )
                ledger.cut(
                    cut_id="C1", piece_id="P", expected_revision=1,
                    u="3/1", left_child="A", right_child="B",
                )
                report = ledger.root_report("R")
                self.assertEqual(report["totals"]["sum"], 10)
                self.assertEqual(report["kerfs"][0]["root_lo"], 3)
                self.assertEqual(report["kerfs"][0]["root_hi"], "31/10")
            finally:
                ledger.close()


class ScaleTests(unittest.TestCase):
    def test_twenty_roots_five_hundred_cuts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(str(Path(tmp) / "scale.db"))
            try:
                roots = [{"root_id": f"R{i}", "length": 1000} for i in range(20)]
                pieces = [
                    {
                        "piece_id": f"P{i}", "root_id": f"R{i}", "root_lo": 0,
                        "root_hi": 1000, "orientation": 1, "revision": 1,
                    }
                    for i in range(20)
                ]
                ledger.initialize(roots=roots, active_pieces=pieces, kerf=2)

                # Split the largest active leaf of each root, round-robin, for
                # exactly 500 cuts.  Every u keeps both children positive.
                active = {r["root_id"]: [(f"P{i}", 0, 1000)] for i, r in enumerate(roots)}
                cuts = 0
                piece_ids = 0
                for cut_index in range(500):
                    root_id = f"R{cut_index % 20}"
                    leaves = active[root_id]
                    leaves.sort(key=lambda leaf: leaf[2] - leaf[1])
                    piece_id, lo, hi = leaves.pop()
                    length = hi - lo
                    u = (length - 2) // 2
                    left_id = f"X{piece_ids}"
                    right_id = f"X{piece_ids + 1}"
                    piece_ids += 2
                    ledger.cut(
                        cut_id=f"C{cut_index}",
                        piece_id=piece_id,
                        expected_revision=1,
                        u=u,
                        left_child=left_id,
                        right_child=right_id,
                    )
                    leaves.append((left_id, lo, lo + u))
                    leaves.append((right_id, lo + u + 2, hi))
                    cuts += 1

                self.assertEqual(cuts, 500)
                total = 0
                for root in roots:
                    report = ledger.root_report(root["root_id"])
                    self.assertEqual(report["totals"]["sum"], 1000)
                    total += report["totals"]["sum"]
                self.assertEqual(total, 20000)
            finally:
                ledger.close()


if __name__ == "__main__":
    unittest.main()
