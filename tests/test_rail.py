"""Test suite for the ordered-marshalling and unload services.

Runs on the standard library only (``python3 -m unittest discover -s tests``),
because no third-party packages are available in this environment.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rail import RailwayStore  # noqa: E402
from rail.material import load_material  # noqa: E402
from rail.model import (  # noqa: E402
    ConflictError,
    InactiveError,
    NotFoundError,
    UnloadError,
    ValidationError,
    couple_sequences,
    reverse_composition,
)

MATERIAL = ROOT / "materials" / "734"


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.store = RailwayStore(":memory:")

    def tearDown(self):
        self.store.close()

    def add_three(self):
        for car_id in ("V1", "V2", "V3"):
            self.store.add_car(car_id, 10)
        self.store.create_train("T", [{"car_id": "V1"}, {"car_id": "V2"}])
        self.store.create_train("S", [{"car_id": "V3"}])
        return self.store

    def add_cargo(self):
        for cargo_id, car_id in (("C1", "V1"), ("C2", "V2"), ("C3", "V3")):
            self.store.add_cargo(cargo_id, car_id, 2)

    def seq(self, train_id):
        return [
            (c["car_id"], c["orientation"])
            for c in self.store.composition_view(train_id)["cars"]
        ]

    def x(self, train_id, car_id, local=2):
        return self.store.coordinate(train_id, car_id, local)["x"]

    def couple_tail_tail(self, event_id="E1"):
        return self.store.apply_event(
            {
                "op": "couple",
                "event_id": event_id,
                "target": "T",
                "target_end": "tail",
                "other": "S",
                "other_end": "tail",
            }
        )


# ===========================================================================
# Pure geometry
# ===========================================================================


class PureModelTests(unittest.TestCase):
    def test_tail_tail_reverses_other_only(self):
        seq, rule = couple_sequences(
            [("V1", 1), ("V2", 1)], [("V3", 1)], "tail", "tail"
        )
        assert seq == [("V1", 1), ("V2", 1), ("V3", -1)]
        assert rule == "target_then_reverse_other"

    def test_four_ends(self):
        t = [("A", 1), ("B", 1)]
        o = [("X", 1), ("Y", 1)]
        assert couple_sequences(t, o, "tail", "head")[0] == t + o
        assert couple_sequences(t, o, "tail", "tail")[0] == t + list(
            reverse_composition(o)
        )
        assert couple_sequences(t, o, "head", "tail")[0] == o + t
        assert couple_sequences(t, o, "head", "head")[0] == list(
            reverse_composition(o)
        ) + t

    def test_reverse_order_and_orientation(self):
        assert reverse_composition([("V1", 1), ("V2", 1), ("V3", -1)]) == [
            ("V3", 1),
            ("V2", -1),
            ("V1", -1),
        ]


# ===========================================================================
# Composition service
# ===========================================================================


class CompositionTests(StoreCase):
    def test_material_tail_tail_geometry(self):
        self.add_three()
        self.add_cargo()
        self.couple_tail_tail()
        assert self.seq("T") == [("V1", 1), ("V2", 1), ("V3", -1)]
        assert self.x("T", "V1") == {"n": 2, "d": 1}
        assert self.x("T", "V2") == {"n": 12, "d": 1}
        assert self.x("T", "V3") == {"n": 28, "d": 1}

    def test_material_reverse_geometry(self):
        self.add_three()
        self.couple_tail_tail()
        self.store.apply_event({"op": "reverse", "event_id": "E2", "train_id": "T"})
        assert self.seq("T") == [("V3", 1), ("V2", -1), ("V1", -1)]
        assert self.x("T", "V3") == {"n": 2, "d": 1}
        assert self.x("T", "V2") == {"n": 18, "d": 1}
        assert self.x("T", "V1") == {"n": 28, "d": 1}

    def test_only_reordering_would_be_wrong(self):
        # Guard against the stated trap: if reverse kept orientations, V2 and V3
        # would land at 12 and 22 instead of 18 and 28.
        self.add_three()
        self.couple_tail_tail()
        self.store.apply_event({"op": "reverse", "event_id": "E2", "train_id": "T"})
        naive = {"V2": 12, "V3": 22}  # order reversed but orientation untouched
        assert self.x("T", "V2") != {"n": naive["V2"], "d": 1}
        assert self.x("T", "V3") != {"n": naive["V3"], "d": 1}

    def test_four_couple_ends_live(self):
        expected = {
            ("tail", "head"): [("V1", 1), ("V2", 1), ("V3", 1)],
            ("tail", "tail"): [("V1", 1), ("V2", 1), ("V3", -1)],
            ("head", "tail"): [("V3", 1), ("V1", 1), ("V2", 1)],
            ("head", "head"): [("V3", -1), ("V1", 1), ("V2", 1)],
        }
        for (te, oe), want in expected.items():
            with self.subTest(te=te, oe=oe):
                s = RailwayStore(":memory:")
                try:
                    for car in ("V1", "V2", "V3"):
                        s.add_car(car, 10)
                    s.create_train("T", [{"car_id": "V1"}, {"car_id": "V2"}])
                    s.create_train("S", [{"car_id": "V3"}])
                    s.apply_event(
                        {
                            "op": "couple",
                            "event_id": "E",
                            "target": "T",
                            "target_end": te,
                            "other": "S",
                            "other_end": oe,
                        }
                    )
                    got = [
                        (c["car_id"], c["orientation"])
                        for c in s.composition_view("T")["cars"]
                    ]
                    assert got == want
                    with self.assertRaises(InactiveError):
                        s.composition_view("S")
                finally:
                    s.close()

    def test_couple_keeps_target_orientation(self):
        self.add_three()
        self.store.apply_event(
            {
                "op": "couple",
                "event_id": "E",
                "target": "T",
                "target_end": "head",
                "other": "S",
                "other_end": "tail",
            }
        )
        target_part = [(c, o) for c, o in self.seq("T") if c in ("V1", "V2")]
        assert target_part == [("V1", 1), ("V2", 1)]

    def test_self_couple_rejected(self):
        self.add_three()
        with self.assertRaises(ValidationError):
            self.store.apply_event(
                {
                    "op": "couple",
                    "event_id": "SELF",
                    "target": "T",
                    "target_end": "tail",
                    "other": "T",
                    "other_end": "tail",
                }
            )

    def test_event_id_replay_and_conflict(self):
        self.add_three()
        req = {
            "op": "couple",
            "event_id": "E1",
            "target": "T",
            "target_end": "tail",
            "other": "S",
            "other_end": "tail",
        }
        first = self.store.apply_event(req)
        assert self.store.apply_event(dict(req)) == first
        with self.assertRaises(ConflictError):
            self.store.apply_event({**req, "other_end": "head"})
        # failed conflicting replay changed nothing
        assert self.seq("T") == [("V1", 1), ("V2", 1), ("V3", -1)]

    def test_duplicate_event_id_different_op_rejected(self):
        self.add_three()
        self.couple_tail_tail("DUP")
        with self.assertRaises(ConflictError):
            self.store.apply_event(
                {"op": "reverse", "event_id": "DUP", "train_id": "T"}
            )
        assert self.seq("T") == [("V1", 1), ("V2", 1), ("V3", -1)]

    def test_split_head(self):
        self.add_three()
        self.add_cargo()
        self.couple_tail_tail()
        result = self.store.apply_event(
            {
                "op": "split",
                "event_id": "SP",
                "train_id": "T",
                "end": "head",
                "n": 2,
                "new_train_id": "T2",
            }
        )
        assert [(c["car_id"], c["orientation"]) for c in result["new_cars"]] == [
            ("V1", 1),
            ("V2", 1),
        ]
        assert self.seq("T2") == [("V1", 1), ("V2", 1)]
        assert self.seq("T") == [("V3", -1)]
        assert self.x("T2", "V1") == {"n": 2, "d": 1}
        assert self.x("T", "V3") == {"n": 8, "d": 1}

    def test_split_tail(self):
        self.add_three()
        self.couple_tail_tail()
        result = self.store.apply_event(
            {
                "op": "split",
                "event_id": "SP",
                "train_id": "T",
                "end": "tail",
                "n": 1,
                "new_train_id": "T3",
            }
        )
        assert [(c["car_id"], c["orientation"]) for c in result["new_cars"]] == [
            ("V3", -1)
        ]
        assert self.seq("T") == [("V1", 1), ("V2", 1)]
        assert self.seq("T3") == [("V3", -1)]

    def test_split_invalid_n(self):
        self.add_three()
        for n in (0, -1, 2, 3):
            with self.subTest(n=n):
                with self.assertRaises(ValidationError):
                    self.store.apply_event(
                        {
                            "op": "split",
                            "event_id": f"SP{n}",
                            "train_id": "T",
                            "end": "head",
                            "n": n,
                            "new_train_id": f"N{n}",
                        }
                    )
        assert self.seq("T") == [("V1", 1), ("V2", 1)]

    def test_split_id_reuse_rejected(self):
        self.add_three()
        with self.assertRaises(ValidationError):
            self.store.apply_event(
                {
                    "op": "split",
                    "event_id": "SP",
                    "train_id": "T",
                    "end": "head",
                    "n": 1,
                    "new_train_id": "S",
                }
            )

    def test_no_car_in_two_active_trains(self):
        self.add_three()
        self.store.apply_event(
            {
                "op": "split",
                "event_id": "SP",
                "train_id": "T",
                "end": "head",
                "n": 1,
                "new_train_id": "T2",
            }
        )
        dup = self.store.conn.execute(
            "SELECT car_id, COUNT(*) c FROM train_cars WHERE active = 1 "
            "GROUP BY car_id HAVING c > 1"
        ).fetchall()
        assert [tuple(r) for r in dup] == []
        # creating a train that shares V1 must fail
        with self.assertRaises(ValidationError):
            self.store.create_train("X", [{"car_id": "V1"}])

    def test_shared_car_rejected_at_creation(self):
        self.add_three()
        with self.assertRaises(ValidationError):
            self.store.create_train("X", [{"car_id": "V1"}, {"car_id": "V2"}])
        with self.assertRaises(ValidationError):
            self.store.create_train("Y", [{"car_id": "V1"}, {"car_id": "V1"}])

    def test_rational_lengths_exact(self):
        self.store.add_car("A", "5/2")
        self.store.add_car("B", {"n": 7, "d": 3})
        self.store.create_train("X", [{"car_id": "A"}, {"car_id": "B"}])
        assert self.store.coordinate("X", "B", {"n": 1, "d": 2})["x"] == {
            "n": 3,
            "d": 1,
        }
        # local position maps exactly at the far edge too
        assert self.store.coordinate("X", "B", {"n": 7, "d": 3})["x"] == {
            "n": 29,
            "d": 6,
        }

    def test_invalid_lengths(self):
        for bad in (0, -1, "0", "-2"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    self.store.add_car(f"bad{bad}", bad)

    def test_out_of_range_local(self):
        self.add_three()
        with self.assertRaises(ValidationError):
            self.store.coordinate("T", "V1", 11)
        with self.assertRaises(ValidationError):
            self.store.coordinate("T", "V1", -1)

    def test_car_not_in_train(self):
        self.add_three()
        with self.assertRaises(NotFoundError):
            self.store.coordinate("T", "V3", 2)

    def test_version_changes_and_reverse_twice_restores_layout_not_revision(self):
        self.add_three()
        self.couple_tail_tail()
        v1 = self.store.composition_view("T")["composition_version"]
        r1 = self.store.composition_view("T")["revision"]
        self.store.apply_event({"op": "reverse", "event_id": "R1", "train_id": "T"})
        v2 = self.store.composition_view("T")["composition_version"]
        assert v1 != v2
        self.store.apply_event({"op": "reverse", "event_id": "R2", "train_id": "T"})
        view = self.store.composition_view("T")
        assert view["composition_version"] == v1  # same geometry
        assert view["revision"] == r1 + 2  # but a distinct revision


# ===========================================================================
# Scanning
# ===========================================================================


class ScanTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.add_three()
        self.add_cargo()
        self.couple_tail_tail()

    def test_r1_matches_material(self):
        report = self.store.platform.scan_platform("T", "R1", [0, 5])
        assert report["schema"] == "platform-scan/v1"
        assert report["report_id"] == "R1"
        assert report["train_id"] == "T"
        assert report["composition_version"]
        by_id = {p["cargo_id"]: p for p in report["cargo_positions"]}
        assert by_id["C1"]["x"] == {"n": 2, "d": 1}
        assert by_id["C1"]["in_window"] is True
        assert by_id["C2"]["x"] == {"n": 12, "d": 1}
        assert by_id["C2"]["in_window"] is False
        assert by_id["C3"]["x"] == {"n": 28, "d": 1}
        assert by_id["C3"]["in_window"] is False
        # permanent car binding, not a car ordinal
        assert by_id["C3"]["car_id"] == "V3"

    def test_r2_after_reverse_hits_c3(self):
        self.store.platform.scan_platform("T", "R1", [0, 5])
        self.store.apply_event({"op": "reverse", "event_id": "E2", "train_id": "T"})
        report = self.store.platform.scan_platform("T", "R2", [0, 5])
        hits = [p["cargo_id"] for p in report["cargo_positions"] if p["in_window"]]
        assert hits == ["C3"]
        by_id = {p["cargo_id"]: p for p in report["cargo_positions"]}
        assert by_id["C3"]["x"] == {"n": 2, "d": 1}
        assert by_id["C2"]["x"] == {"n": 18, "d": 1}
        assert by_id["C1"]["x"] == {"n": 28, "d": 1}

    def test_old_report_is_frozen(self):
        r1 = self.store.platform.scan_platform("T", "R1", [0, 5])
        self.store.apply_event({"op": "reverse", "event_id": "E2", "train_id": "T"})
        again = self.store.platform.get_report("R1")
        assert again == r1
        assert {p["cargo_id"]: p["x"] for p in again["cargo_positions"]}["C1"] == {
            "n": 2,
            "d": 1,
        }

    def test_scan_version_mismatch_rejected(self):
        with self.assertRaises(ValidationError):
            self.store.platform.scan_platform(
                "T", "RX", [0, 5], composition_version="not-the-version"
            )

    def test_scan_report_id_conflict(self):
        self.store.platform.scan_platform("T", "R1", [0, 5])
        with self.assertRaises(ConflictError):
            self.store.platform.scan_platform("T", "R1", [0, 6])

    def test_scan_replay_identical(self):
        a = self.store.platform.scan_platform("T", "R1", [0, 5])
        b = self.store.platform.scan_platform("T", "R1", [0, 5])
        assert a == b

    def test_reader_accepts_report(self):
        report = self.store.platform.scan_platform("T", "R1", [0, 5])
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "R1.json"
            path.write_text(json.dumps(report))
            proc = subprocess.run(
                [sys.executable, str(MATERIAL / "platform_reader.py"), str(path)],
                capture_output=True,
                text=True,
            )
        assert proc.returncode == 0, proc.stderr
        lines = proc.stdout.strip().splitlines()
        assert lines[0] == f"R1 T {report['composition_version']}"
        assert "C1 V1 2 IN" in lines
        assert "C2 V2 12 OUT" in lines
        assert "C3 V3 28 OUT" in lines


# ===========================================================================
# Unloading
# ===========================================================================


class UnloadTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.add_three()
        self.add_cargo()
        self.couple_tail_tail()

    def version(self, train_id="T"):
        return self.store.composition_view(train_id)["composition_version"]

    def test_full_material_chain(self):
        # R1: C1 in [0,5)
        r1 = self.store.platform.scan_platform("T", "R1", [0, 5])
        v1 = r1["composition_version"]
        hits = [p["cargo_id"] for p in r1["cargo_positions"] if p["in_window"]]
        assert hits == ["C1"]

        # prepare U1 (legal), reverse, old basis must be refused
        u1 = self.store.prepare_unload("U1", "T", v1, [0, 5], ["C1"])
        assert u1["state"] == "PREPARED"
        assert self.store.get_cargo("C1")["status"] == "LOADED"

        self.store.apply_event({"op": "reverse", "event_id": "E2", "train_id": "T"})
        with self.assertRaises(UnloadError):
            self.store.confirm_unload("U1")
        assert self.store.get_cargo("C1")["status"] == "LOADED"

        # R2: C3 now in [0,5)
        r2 = self.store.platform.scan_platform("T", "R2", [0, 5])
        v2 = r2["composition_version"]
        assert [p["cargo_id"] for p in r2["cargo_positions"] if p["in_window"]] == [
            "C3"
        ]
        assert r2["composition_version"] != r1["composition_version"]

        u2 = self.store.prepare_unload("U2", "T", v2, [0, 5], ["C3"])
        confirmed = self.store.confirm_unload("U2")
        assert confirmed["state"] == "CONFIRMED"
        receipt = confirmed["receipt"]
        assert receipt["receipt_id"] == "RC-U2"
        assert [i["cargo_id"] for i in receipt["items"]] == ["C3"]
        assert self.store.get_cargo("C3")["status"] == "UNLOADED"

        # U3 tries to unload C3 again: geometry still hits, status does not
        with self.assertRaises(UnloadError):
            self.store.prepare_unload("U3", "T", v2, [0, 5], ["C3"])

        # R1/R2 and the new receipt coexist and do not share a snapshot table
        assert self.store.platform.get_report("R1") == r1
        assert self.store.platform.get_report("R2") == r2
        assert self.store.unload.get_receipt("U2")["receipt_id"] == "RC-U2"
        assert r1["composition_version"] != r2["composition_version"]

    def test_race_two_jobs_same_cargo_one_wins(self):
        v = self.version()
        self.store.prepare_unload("A", "T", v, [0, 5], ["C1"])
        self.store.prepare_unload("B", "T", v, [0, 5], ["C1"])
        a = self.store.confirm_unload("A")
        assert a["state"] == "CONFIRMED"
        with self.assertRaises(UnloadError):
            self.store.confirm_unload("B")
        assert self.store.get_cargo("C1")["status"] == "UNLOADED"
        assert self.store.get_cargo("C1")["unload_job_id"] == "A"
        with self.assertRaises(UnloadError):
            self.store.unload.get_receipt("B")

    def test_confirm_is_all_or_nothing(self):
        # C1 is inside [0,5); C2 is not.  Whole job must be refused.
        v = self.version()
        with self.assertRaises(UnloadError):
            self.store.prepare_unload("U", "T", v, [0, 5], ["C1", "C2"])
        assert self.store.get_cargo("C1")["status"] == "LOADED"
        assert self.store.get_cargo("C2")["status"] == "LOADED"

    def test_prepare_does_not_change_status(self):
        v = self.version()
        self.store.prepare_unload("U", "T", v, [0, 5], ["C1"])
        assert self.store.get_cargo("C1")["status"] == "LOADED"
        assert self.store.unload.get_job("U")["state"] == "PREPARED"

    def test_confirm_wrong_version_rejected(self):
        v = self.version()
        self.store.prepare_unload("U", "T", v, [0, 5], ["C1"])
        # Reverse and back: geometry version identical, revision is not.
        self.store.apply_event({"op": "reverse", "event_id": "R1", "train_id": "T"})
        self.store.apply_event({"op": "reverse", "event_id": "R2", "train_id": "T"})
        assert self.version() == v
        with self.assertRaises(UnloadError):
            self.store.confirm_unload("U")
        assert self.store.get_cargo("C1")["status"] == "LOADED"

    def test_split_invalidates_prepared_job(self):
        v = self.version()
        self.store.prepare_unload("U", "T", v, [0, 5], ["C1"])
        self.store.apply_event(
            {
                "op": "split",
                "event_id": "SP",
                "train_id": "T",
                "end": "head",
                "n": 1,
                "new_train_id": "T2",
            }
        )
        with self.assertRaises(UnloadError):
            self.store.confirm_unload("U")
        assert self.store.get_cargo("C1")["status"] == "LOADED"

    def test_couple_invalidates_prepared_job(self):
        self.store.add_car("V9", 10)
        self.store.create_train("S2", [{"car_id": "V9"}])
        self.store.add_cargo("C9", "V9", 2)
        # C1 at x=2 is on the platform in T
        v = self.store.composition_view("T")["composition_version"]
        self.store.prepare_unload("U", "T", v, [0, 5], ["C1"])
        self.store.apply_event(
            {
                "op": "couple",
                "event_id": "CP",
                "target": "T",
                "target_end": "head",
                "other": "S2",
                "other_end": "tail",
            }
        )
        with self.assertRaises(UnloadError):
            self.store.confirm_unload("U")
        assert self.store.get_cargo("C1")["status"] == "LOADED"

    def test_cancel_prepared_and_refuse_confirmed(self):
        v = self.version()
        self.store.prepare_unload("U", "T", v, [0, 5], ["C1"])
        cancelled = self.store.cancel_unload("U", "operator aborted")
        assert cancelled["state"] == "CANCELLED"
        with self.assertRaises(UnloadError):
            self.store.confirm_unload("U")
        assert self.store.get_cargo("C1")["status"] == "LOADED"

        self.store.prepare_unload("U2", "T", v, [0, 5], ["C1"])
        self.store.confirm_unload("U2")
        with self.assertRaises(UnloadError):
            self.store.cancel_unload("U2")
        assert self.store.get_cargo("C1")["status"] == "UNLOADED"

    def test_prepare_replay_and_conflict(self):
        v = self.version()
        first = self.store.prepare_unload("U", "T", v, [0, 5], ["C1"])
        assert self.store.prepare_unload("U", "T", v, [0, 5], ["C1"]) == first
        with self.assertRaises(ConflictError):
            self.store.prepare_unload("U", "T", v, [0, 5], ["C2"])
        # terminal replay of a confirmed job returns the receipt
        self.store.confirm_unload("U")
        terminal = self.store.prepare_unload("U", "T", v, [0, 5], ["C1"])
        assert terminal["state"] == "CONFIRMED"
        assert terminal["receipt"]["receipt_id"] == "RC-U"
        # cancel replay/conflict
        self.store.prepare_unload("V", "T", v, [0, 15], ["C2"])
        self.store.cancel_unload("V", "why")
        assert self.store.cancel_unload("V", "why")["state"] == "CANCELLED"
        with self.assertRaises(ConflictError):
            self.store.cancel_unload("V", "other")

    def test_duplicate_cargo_ids_rejected(self):
        v = self.version()
        with self.assertRaises(ValidationError):
            self.store.prepare_unload("U", "T", v, [0, 5], ["C1", "C1"])

    def test_half_open_window(self):
        v = self.version()
        # C1 is at x=2: [0,2) excludes it, [2,3) includes it.
        with self.assertRaises(UnloadError):
            self.store.prepare_unload("U", "T", v, [0, 2], ["C1"])
        report = self.store.platform.scan_platform("T", "RH", [0, 2])
        by_id = {p["cargo_id"]: p for p in report["cargo_positions"]}
        assert by_id["C1"]["in_window"] is False  # x=2, [0,2) excludes 2
        report2 = self.store.platform.scan_platform("T", "RH2", [2, 3])
        by_id2 = {p["cargo_id"]: p for p in report2["cargo_positions"]}
        assert by_id2["C1"]["in_window"] is True
        ok = self.store.prepare_unload("U2", "T", v, [2, 3], ["C1"])
        assert ok["state"] == "PREPARED"

    def test_scan_is_not_unload_authority(self):
        # Repeated scans of the same hit never change cargo status.
        for i in range(3):
            self.store.platform.scan_platform("T", f"R{i}", [0, 5])
        assert self.store.get_cargo("C1")["status"] == "LOADED"
        # And a report alone cannot confirm anything.
        assert self.store.unload.list_jobs() == []

    def test_no_reload_after_composition_change(self):
        v = self.version()
        self.store.prepare_unload("U", "T", v, [0, 5], ["C1"])
        self.store.confirm_unload("U")
        assert self.store.get_cargo("C1")["status"] == "UNLOADED"
        self.store.apply_event(
            {
                "op": "split",
                "event_id": "SP",
                "train_id": "T",
                "end": "head",
                "n": 1,
                "new_train_id": "T2",
            }
        )
        assert self.store.get_cargo("C1")["status"] == "UNLOADED"
        # A new job cannot unload it again, even in its new train.
        v2 = self.store.composition_view("T2")["composition_version"]
        with self.assertRaises(UnloadError):
            self.store.prepare_unload("U2", "T2", v2, [0, 5], ["C1"])

    def test_partial_failure_leaves_all_cargo_loaded(self):
        # U1 covers C1(x=2) and C2(x=12); another job unloads C2 first.
        v = self.version()
        self.store.prepare_unload("U1", "T", v, [0, 15], ["C1", "C2"])
        self.store.prepare_unload("U2", "T", v, [0, 15], ["C2"])
        self.store.confirm_unload("U2")
        with self.assertRaises(UnloadError):
            self.store.confirm_unload("U1")
        # C1 must not have been flipped even though its own basis still held
        assert self.store.get_cargo("C1")["status"] == "LOADED"
        assert self.store.unload.get_job("U1")["state"] == "PREPARED"
        with self.assertRaises(UnloadError):
            self.store.unload.get_receipt("U1")

    def test_two_connections_serialise_confirm(self):
        # A second connection to the same file must see the committed state and
        # must not be able to unload the same cargo again.
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "race.db")
            a = RailwayStore(path)
            b = None
            try:
                for car in ("V1", "V2"):
                    a.add_car(car, 10)
                a.create_train("T", [{"car_id": "V1"}, {"car_id": "V2"}])
                a.add_cargo("C1", "V1", 2)
                v = a.composition_view("T")["composition_version"]
                a.prepare_unload("A", "T", v, [0, 5], ["C1"])
                b = RailwayStore(path)
                b.prepare_unload("B", "T", v, [0, 5], ["C1"])
                a.confirm_unload("A")
                with self.assertRaises(UnloadError):
                    b.confirm_unload("B")
                assert b.get_cargo("C1")["status"] == "UNLOADED"
                assert b.get_cargo("C1")["unload_job_id"] == "A"
            finally:
                a.close()
                if b is not None:
                    b.close()

    def test_confirmed_is_terminal_replay(self):
        v = self.version()
        self.store.prepare_unload("A", "T", v, [0, 5], ["C1"])
        self.store.confirm_unload("A")
        receipt = self.store.unload.get_receipt("A")
        assert self.store.confirm_unload("A")["receipt"] == receipt
        assert self.store.get_cargo("C1")["status"] == "UNLOADED"


# ===========================================================================
# Material loading, CLI and reopen
# ===========================================================================


class MaterialAndReopenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = str(Path(self.tmp) / "rail.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_load_material_and_reopen(self):
        s = RailwayStore(self.db)
        try:
            summary = load_material(s.conn, MATERIAL)
            assert summary["cars"] == ["V1", "V2", "V3"]
            assert [c["cargo_id"] for c in s.unload.list_cargo()] == ["C1", "C2", "C3"]
            assert s.composition_view("T")["cars"][0]["car_id"] == "V1"
            v = s.composition_view("T")["composition_version"]
        finally:
            s.close()

        # reopen: identity and versions survive; a repeating event is idempotent
        s = RailwayStore(self.db)
        try:
            assert s.composition_view("T")["composition_version"] == v
            first = s.apply_event(
                {
                    "op": "couple",
                    "event_id": "E1",
                    "target": "T",
                    "target_end": "tail",
                    "other": "S",
                    "other_end": "tail",
                }
            )
            assert s.apply_event(
                {
                    "op": "couple",
                    "event_id": "E1",
                    "target": "T",
                    "target_end": "tail",
                    "other": "S",
                    "other_end": "tail",
                }
            ) == first
            assert s.composition_view("T")["cars"][2]["orientation"] == -1
        finally:
            s.close()

    def test_reopen_keeps_reports_jobs_and_status(self):
        s = RailwayStore(self.db)
        try:
            load_material(s.conn, MATERIAL)
            s.apply_event(
                {
                    "op": "couple",
                    "event_id": "E1",
                    "target": "T",
                    "target_end": "tail",
                    "other": "S",
                    "other_end": "tail",
                }
            )
            r1 = s.platform.scan_platform("T", "R1", [0, 5])
            v = r1["composition_version"]
            s.prepare_unload("U1", "T", v, [0, 5], ["C1"])
            s.confirm_unload("U1")
            job_before = s.unload.get_job("U1")
            report_before = s.platform.get_report("R1")
            state_before = s.get_cargo("C1")["status"]
        finally:
            s.close()

        s = RailwayStore(self.db)
        try:
            assert s.platform.get_report("R1") == report_before
            assert s.unload.get_job("U1") == job_before
            assert s.get_cargo("C1")["status"] == state_before == "UNLOADED"
            # old job can be replayed, never re-run
            assert s.confirm_unload("U1")["receipt"] == job_before["receipt"]
            # a new job still cannot unload C1
            with self.assertRaises(UnloadError):
                s.prepare_unload("U2", "T", v, [0, 5], ["C1"])
            # event replay after reopen returns the original result
            assert s.apply_event(
                {
                    "op": "couple",
                    "event_id": "E1",
                    "target": "T",
                    "target_end": "tail",
                    "other": "S",
                    "other_end": "tail",
                }
            )["layout_rule"] == "target_then_reverse_other"
        finally:
            s.close()

    def test_cli_end_to_end(self):
        env = {"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"}
        def run(*args):
            return subprocess.run(
                [sys.executable, "-m", "rail.cli", "--db", self.db, *args],
                capture_output=True,
                text=True,
                env=env,
                cwd=str(ROOT),
            )

        r = run("init", "--material", str(MATERIAL))
        assert r.returncode == 0, r.stderr
        # material log: E1 tail-tail couple, then R1/R2 scans
        r = run("replay", "--material", str(MATERIAL))
        assert r.returncode == 0, r.stderr
        r = run("read-report", "R1")
        assert r.returncode == 0, r.stderr
        r1 = json.loads(r.stdout)
        assert {p["cargo_id"]: p["in_window"] for p in r1["cargo_positions"]} == {
            "C1": True,
            "C2": False,
            "C3": False,
        }
        r = run("read-report", "R2")
        r2 = json.loads(r.stdout)
        assert {p["cargo_id"]: p["in_window"] for p in r2["cargo_positions"]} == {
            "C1": False,
            "C2": False,
            "C3": True,
        }
        # downstream reader consumes the stored R1 unchanged
        out = Path(self.tmp) / "R1.json"
        r = run("scan", "T", "R1", "--start", "0", "--end", "5", "--out", str(out))
        assert r.returncode == 0, r.stderr
        reader = subprocess.run(
            [sys.executable, str(MATERIAL / "platform_reader.py"), str(out)],
            capture_output=True,
            text=True,
        )
        assert reader.returncode == 0, reader.stderr
        assert "C1 V1 2 IN" in reader.stdout

        # after E1 + E2 the composition is [V3+, V2-, V1-]
        r = run("coordinate", "T", "V3", "2")
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["x"] == {"n": 2, "d": 1}
        r = run("coordinate", "T", "V1", "2")
        assert json.loads(r.stdout)["x"] == {"n": 28, "d": 1}


if __name__ == "__main__":
    unittest.main(verbosity=2)
