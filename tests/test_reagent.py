"""Tests for the reagent inventory + run service (stdlib unittest only)."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from reagent import connect, initialize
from reagent.errors import (
    Conflict,
    InsufficientStock,
    NotFoundError,
    ValidationError,
)
from reagent.inventory import InventoryService
from reagent.runs import RunService

PATTERN_DOCUMENT = {
    "initial_day": 10,
    "kinds": [{"kind": "R", "open_lifetime_days": 2}],
    "bottles": [
        {
            "bottle_id": "U",
            "kind": "R",
            "total_portions": 4,
            "intake_day": 10,
            "printed_expiry_day": 20,
            "opened_day": None,
        },
        {
            "bottle_id": "P",
            "kind": "R",
            "total_portions": 2,
            "intake_day": 8,
            "printed_expiry_day": 20,
            "opened_day": 9,
        },
        {
            "bottle_id": "LAST",
            "kind": "R",
            "total_portions": 1,
            "intake_day": 10,
            "printed_expiry_day": 20,
            "opened_day": None,
        },
    ],
    "methods": [
        {"method_id": "M", "revision": 1, "requirements": {"R": 1}},
        {
            "method_id": "M2",
            "revision": 1,
            "requirements": {"R": 2},
        },
    ],
}


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "store.db")
        self.conn = connect(self.db_path)
        initialize(self.conn)
        self.inventory = InventoryService(self.conn)
        self.runs = RunService(self.conn)

    def tearDown(self) -> None:
        try:
            self.conn.close()
        finally:
            self._tmp.cleanup()

    def init_document(self, document: dict | None = None) -> None:
        self.inventory.initialize(document or PATTERN_DOCUMENT)

    def reserve(self, run_id: str, day: int, *pairs) -> dict:
        total = sum(portions for _, portions in pairs)
        method_id = "M2" if total == 2 else "M"
        return self.runs.create_run(
            run_id,
            method_id,
            1,
            day,
            [{"bottle_id": b, "portions": p} for b, p in pairs],
        )


class ValidityTests(StoreTestCase):
    def test_unopened_uses_intake_and_printed_expiry(self) -> None:
        self.init_document()
        alloc = self.inventory.query("U", 10)
        self.assertTrue(alloc["valid_today"])
        self.assertEqual(alloc["opened_day"], None)
        # day of printed expiry is not usable
        self.assertFalse(self.inventory.query("U", 20)["valid_today"])
        self.assertFalse(self.inventory.query("U", 9)["valid_today"])

    def test_opened_uses_min_printed_and_open_plus_lifetime(self) -> None:
        self.init_document()
        # P opened day 9, L=2 -> days 9,10 usable; 11 is not
        alloc10 = self.inventory.query("P", 10)
        self.assertTrue(alloc10["valid_today"])
        self.assertEqual(alloc10["effective_expiry_day"], 11)
        alloc11 = self.inventory.query("P", 11)
        self.assertFalse(alloc11["valid_today"])
        # expired stock keeps unallocated, only leadability drops
        self.assertEqual(alloc11["unallocated"], 2)
        self.assertEqual(alloc11["leadable_today"], 0)

    def test_open_lifetime_never_extends_past_printed_expiry(self) -> None:
        self.init_document(
            {
                "initial_day": 10,
                "kinds": [{"kind": "R", "open_lifetime_days": 50}],
                "bottles": [
                    {
                        "bottle_id": "X",
                        "kind": "R",
                        "total_portions": 1,
                        "intake_day": 1,
                        "printed_expiry_day": 12,
                        "opened_day": 10,
                    }
                ],
                "methods": [
                    {"method_id": "M", "revision": 1, "requirements": {"R": 1}}
                ],
            }
        )
        self.assertEqual(self.inventory.query("X", 10)["effective_expiry_day"], 12)
        self.assertFalse(self.inventory.query("X", 12)["valid_today"])


class InitializationTests(StoreTestCase):
    def test_printed_expiry_must_follow_intake(self) -> None:
        bad = json.loads(json.dumps(PATTERN_DOCUMENT))
        bad["bottles"][0]["printed_expiry_day"] = 10
        with self.assertRaises(ValidationError):
            self.inventory.initialize(bad)

    def test_initial_opened_day_must_be_within_intake_and_initial_day(self) -> None:
        bad = json.loads(json.dumps(PATTERN_DOCUMENT))
        bad["bottles"][1]["opened_day"] = 5  # before intake 8
        with self.assertRaises(ValidationError):
            self.inventory.initialize(bad)
        bad = json.loads(json.dumps(PATTERN_DOCUMENT))
        bad["bottles"][1]["opened_day"] = 11  # after initial day 10
        with self.assertRaises(ValidationError):
            self.inventory.initialize(bad)

    def test_non_positive_and_non_integer_rejected(self) -> None:
        for bad_value in (0, -1, 1.5, True, "1"):
            bad = json.loads(json.dumps(PATTERN_DOCUMENT))
            bad["bottles"][0]["total_portions"] = bad_value
            with self.assertRaises(ValidationError):
                self.inventory.initialize(bad)
        bad = json.loads(json.dumps(PATTERN_DOCUMENT))
        bad["kinds"][0]["open_lifetime_days"] = 0
        with self.assertRaises(ValidationError):
            self.inventory.initialize(bad)

    def test_unknown_kind_rejected(self) -> None:
        bad = json.loads(json.dumps(PATTERN_DOCUMENT))
        bad["bottles"][0]["kind"] = "Z"
        with self.assertRaises(ValidationError):
            self.inventory.initialize(bad)

    def test_double_initialization_conflicts(self) -> None:
        self.init_document()
        with self.assertRaises(Conflict):
            self.init_document()


class ManualOpenTests(StoreTestCase):
    def test_manual_open_sets_actual_day(self) -> None:
        self.init_document()
        alloc = self.inventory.open_bottle("U", 11)
        self.assertEqual(alloc["opened_day"], 11)
        self.assertEqual(alloc["effective_expiry_day"], 13)

    def test_cannot_open_before_intake(self) -> None:
        self.init_document(
            {
                "initial_day": 10,
                "kinds": [{"kind": "R", "open_lifetime_days": 2}],
                "bottles": [
                    {
                        "bottle_id": "FUTURE",
                        "kind": "R",
                        "total_portions": 1,
                        "intake_day": 12,
                        "printed_expiry_day": 20,
                        "opened_day": None,
                    }
                ],
                "methods": [
                    {"method_id": "M", "revision": 1, "requirements": {"R": 1}}
                ],
            }
        )
        # day 11 is >= initial day but still before intake
        with self.assertRaises(ValidationError):
            self.inventory.open_bottle("FUTURE", 11)

    def test_cannot_rewrite_or_reopen(self) -> None:
        self.init_document()
        self.inventory.open_bottle("U", 11)
        with self.assertRaises(Conflict):
            self.inventory.open_bottle("U", 12)
        self.assertEqual(self.inventory.query("U", 12)["opened_day"], 11)

    def test_cannot_rewrite_initial_opened_bottle(self) -> None:
        self.init_document()
        with self.assertRaises(Conflict):
            self.inventory.open_bottle("P", 10)
        self.assertEqual(self.inventory.query("P", 10)["opened_day"], 9)


class ReserveConsumeTests(StoreTestCase):
    def test_reservation_does_not_open_or_spend_shelf_life(self) -> None:
        self.init_document()
        self.reserve("r1", 10, ("U", 1))
        alloc = self.inventory.query("U", 10)
        self.assertIsNone(alloc["opened_day"])
        self.assertEqual(alloc["reserved"], 1)
        self.assertEqual(alloc["consumed"], 0)
        self.assertEqual(alloc["unallocated"], 3)
        # still usable all the way to printed expiry while merely reserved
        self.assertTrue(self.inventory.query("U", 19)["valid_today"])

    def test_consume_sets_first_open_on_actual_use_day_only(self) -> None:
        self.init_document()
        self.reserve("r1", 10, ("U", 1))
        self.assertEqual(self.inventory.query("U", 10)["opened_day"], None)
        self.runs.start_run("r1", 12)
        alloc = self.inventory.query("U", 12)
        self.assertEqual(alloc["opened_day"], 12)
        self.assertEqual(alloc["consumed"], 1)
        self.assertEqual(alloc["reserved"], 0)
        self.assertEqual(alloc["unallocated"], 3)
        self.assertEqual(alloc["effective_expiry_day"], 14)

    def test_new_consumption_does_not_reset_existing_open_day(self) -> None:
        self.init_document()
        self.reserve("r1", 10, ("P", 1))
        self.runs.start_run("r1", 10)
        self.assertEqual(self.inventory.query("P", 10)["opened_day"], 9)
        self.runs.create_run(
            "r2", "M", 1, 10, [{"bottle_id": "P", "portions": 1}]
        )
        self.runs.start_run("r2", 10)
        alloc = self.inventory.query("P", 10)
        self.assertEqual(alloc["opened_day"], 9)
        self.assertEqual(alloc["consumed"], 2)

    def test_failed_start_leaves_no_opening_fact_and_releases_all(self) -> None:
        self.init_document()
        self.reserve("bad", 10, ("P", 1))
        receipt = self.runs.start_run("bad", 11)
        self.assertEqual(receipt["state"], "released")
        self.assertIn("unusable", receipt["reason"])
        self.assertEqual(self.inventory.query("P", 11)["opened_day"], 9)
        self.assertEqual(self.inventory.query("P", 11)["reserved"], 0)
        self.assertEqual(self.inventory.query("P", 11)["consumed"], 0)
        self.assertEqual(self.inventory.query("P", 11)["unallocated"], 2)

    def test_failed_start_releases_every_reservation_of_run(self) -> None:
        self.init_document()
        # U stays valid, P expires on day 11; the whole run must release
        self.runs.create_run(
            "multi",
            "M2",
            1,
            10,
            [
                {"bottle_id": "U", "portions": 1},
                {"bottle_id": "P", "portions": 1},
            ],
        )
        receipt = self.runs.start_run("multi", 11)
        self.assertEqual(receipt["state"], "released")
        self.assertEqual(len(receipt["released"]), 2)
        assert self.inventory.query("U", 11)["unallocated"] == 4
        assert self.inventory.query("U", 11)["reserved"] == 0
        assert self.inventory.query("P", 11)["unallocated"] == 2
        # the valid bottle in the failed run got no opening fact either
        assert self.inventory.query("U", 11)["opened_day"] is None

    def test_repeat_start_replays_terminal_state_without_reconsuming(self) -> None:
        self.init_document()
        self.reserve("r1", 10, ("U", 1))
        first = self.runs.start_run("r1", 12)
        second = self.runs.start_run("r1", 13)
        self.assertTrue(second["replayed"])
        self.assertEqual(second["started_day"], first["started_day"])
        self.assertEqual(self.inventory.query("U", 13)["consumed"], 1)
        self.assertEqual(self.inventory.query("U", 13)["unallocated"], 3)

    def test_repeat_failed_start_replays_original_reason(self) -> None:
        self.init_document()
        self.reserve("bad", 10, ("P", 1))
        first = self.runs.start_run("bad", 11)
        # later stock changes must not recompute the stored reason
        self.runs.create_run("other", "M", 1, 11, [{"bottle_id": "U", "portions": 1}])
        second = self.runs.start_run("bad", 12)
        self.assertTrue(second["replayed"])
        self.assertEqual(second["reason"], first["reason"])
        self.assertEqual(second["released_day"], first["released_day"])

    def test_terminal_run_cannot_be_released(self) -> None:
        self.init_document()
        self.reserve("r1", 10, ("U", 1))
        self.runs.start_run("r1", 12)
        with self.assertRaises(Conflict):
            self.runs.release_run("r1", 12, "too late")
        self.assertEqual(self.inventory.query("U", 12)["consumed"], 1)

    def test_release_reserved_run_returns_portions(self) -> None:
        self.init_document()
        self.reserve("r1", 10, ("U", 2))
        self.runs.release_run("r1", 10, "cancelled")
        alloc = self.inventory.query("U", 10)
        self.assertEqual(alloc["reserved"], 0)
        self.assertEqual(alloc["unallocated"], 4)
        # replaying release returns the same receipt
        again = self.runs.release_run("r1", 10, "cancelled")
        self.assertTrue(again["replayed"])

    def test_failed_create_leaves_no_half_order(self) -> None:
        # Hold LAST entirely so the second selected bottle fails after the
        # first one has already been validated as available.
        self.init_document()
        self.inventory.hold("blocker", [("LAST", 1)], 10)
        with self.assertRaises(InsufficientStock):
            self.runs.create_run(
                "half",
                "M2",
                1,
                10,
                [
                    {"bottle_id": "U", "portions": 1},
                    {"bottle_id": "LAST", "portions": 1},
                ],
            )
        self.assertEqual(self.inventory.query("U", 10)["reserved"], 0)
        self.assertFalse(
            self.conn.execute(
                "SELECT 1 FROM runs WHERE run_id='half'"
            ).fetchone()
        )
        self.assertEqual(self.inventory.query("LAST", 10)["unallocated"], 0)
        self.assertEqual(self.inventory.query("LAST", 10)["reserved"], 1)

    def test_expired_bottle_cannot_be_reserved(self) -> None:
        self.init_document()
        with self.assertRaises(InsufficientStock):
            self.reserve("late", 11, ("P", 1))

    def test_duplicate_run_id_rejected(self) -> None:
        self.init_document()
        self.reserve("r1", 10, ("U", 1))
        with self.assertRaises(Conflict):
            self.reserve("r1", 10, ("U", 1))


class SelectionTests(StoreTestCase):
    def test_selection_must_exactly_match_required_kind_totals(self) -> None:
        self.init_document()
        # requires 2 R portions
        with self.assertRaises(ValidationError):
            self.runs.create_run(
                "short", "M2", 1, 10, [{"bottle_id": "U", "portions": 1}]
            )
        with self.assertRaises(ValidationError):
            self.runs.create_run(
                "over", "M2", 1, 10, [{"bottle_id": "U", "portions": 3}]
            )

    def test_selection_may_split_across_bottles_of_same_kind(self) -> None:
        self.init_document()
        run = self.runs.create_run(
            "split",
            "M2",
            1,
            10,
            [
                {"bottle_id": "U", "portions": 1},
                {"bottle_id": "LAST", "portions": 1},
            ],
        )
        self.assertEqual(run["allocations"]["R"]["active"], 2)
        self.assertEqual(self.inventory.query("U", 10)["reserved"], 1)
        self.assertEqual(self.inventory.query("LAST", 10)["reserved"], 1)

    def test_unknown_method_and_bottle_rejected(self) -> None:
        self.init_document()
        with self.assertRaises(NotFoundError):
            self.runs.create_run("x", "NOPE", 1, 10, [{"bottle_id": "U", "portions": 1}])
        with self.assertRaises(NotFoundError):
            self.reserve("y", 10, ("NOPE", 1))

    def test_zero_or_non_integer_portions_rejected(self) -> None:
        self.init_document()
        for bad in (0, -1, 1.0, True):
            with self.assertRaises(ValidationError):
                self.runs.create_run(
                    f"x{bad}", "M", 1, 10, [{"bottle_id": "U", "portions": bad}]
                )


class RevisionTests(StoreTestCase):
    def test_revision_only_affects_new_runs(self) -> None:
        self.init_document()
        self.reserve("old", 10, ("U", 1))
        self.runs.revise_method("M", 2, {"R": 2}, 12)
        old = self.runs.get_run("old")
        self.assertEqual(old["method_revision"], 1)
        self.assertEqual(old["requirements"], {"R": 1})
        new = self.runs.create_run(
            "new",
            "M",
            2,
            12,
            [
                {"bottle_id": "U", "portions": 1},
                {"bottle_id": "LAST", "portions": 1},
            ],
        )
        self.assertEqual(new["requirements"], {"R": 2})

    def test_revision_must_increase(self) -> None:
        self.init_document()
        with self.assertRaises(Conflict):
            self.runs.revise_method("M", 1, {"R": 2}, 10)
        with self.assertRaises(ValidationError):
            self.runs.revise_method("M", 3, {"Z": 1}, 10)
        with self.assertRaises(ValidationError):
            self.runs.revise_method("M", 3, {"R": 0}, 10)

    def test_new_revision_still_bounded_by_bottle_validity(self) -> None:
        self.init_document()
        self.runs.revise_method("M", 3, {"R": 2}, 12)
        # P is expired on day 11 and cannot satisfy a new run, even after the
        # revision that only adds new demand
        with self.assertRaises(InsufficientStock):
            self.runs.create_run(
                "late", "M", 3, 12, [{"bottle_id": "P", "portions": 2}]
            )


class ConservationTests(StoreTestCase):
    def test_expired_unallocated_portions_stay_in_equation(self) -> None:
        self.init_document()
        rows = {r["bottle_id"]: r for r in self.inventory.conservation()}
        self.assertEqual(rows["U"], {
            "bottle_id": "U",
            "total_portions": 4,
            "unallocated": 4,
            "reserved": 0,
            "consumed": 0,
        })
        # run the U scenario: consume 3 by day 12; by day 14 one portion left
        self.reserve("r1", 10, ("U", 1))
        self.runs.start_run("r1", 12)
        self.runs.create_run(
            "r2", "M2", 1, 12,
            [{"bottle_id": "U", "portions": 2}],
        )
        self.runs.start_run("r2", 12)
        day13 = self.inventory.query("U", 13)
        self.assertEqual(day13["unallocated"], 1)
        self.assertEqual(day13["leadable_today"], 1)
        day14 = self.inventory.query("U", 14)
        self.assertEqual(day14["unallocated"], 1)
        self.assertEqual(day14["leadable_today"], 0)
        rows = {r["bottle_id"]: r for r in self.inventory.conservation()}
        self.assertEqual(rows["U"]["total_portions"], 4)
        self.assertEqual(
            rows["U"]["unallocated"] + rows["U"]["reserved"] + rows["U"]["consumed"],
            4,
        )

    def test_run_quantities_are_derived_from_inventory_rows(self) -> None:
        self.init_document()
        run = self.runs.create_run(
            "r1", "M2", 1, 10, [{"bottle_id": "U", "portions": 2}]
        )
        self.assertEqual(run["allocations"]["R"]["active"], 2)
        # mutate only the inventory row; run view must follow, proving there
        # is no second copy of the quantity
        self.conn.execute(
            "UPDATE reservations SET portions = 1 "
            "WHERE holder_id = 'r1' AND status = 'active'"
        )
        run = self.runs.get_run("r1")
        self.assertEqual(run["allocations"]["R"]["active"], 1)
        self.assertEqual(self.inventory.query("U", 10)["reserved"], 1)


class StandaloneHoldTests(StoreTestCase):
    def test_hold_consume_release_without_runs(self) -> None:
        self.init_document()
        view = self.inventory.hold("h1", [("U", 2)], 10)
        self.assertIsNone(self.inventory.query("U", 10)["opened_day"])
        self.assertEqual(view["per_kind"]["R"]["active"], 2)
        consumed = self.inventory.consume_held("h1", 12)
        self.assertEqual(consumed[0]["opened_now"], True)
        alloc = self.inventory.query("U", 12)
        self.assertEqual(alloc["consumed"], 2)
        self.assertEqual(alloc["opened_day"], 12)

    def test_hold_release_returns_stock(self) -> None:
        self.init_document()
        self.inventory.hold("h1", [("LAST", 1)], 10)
        with self.assertRaises(InsufficientStock):
            self.inventory.hold("h2", [("LAST", 1)], 10)
        self.assertEqual(self.inventory.release_held("h1", 10, "abort"), 1)
        self.inventory.hold("h2", [("LAST", 1)], 10)
        self.assertEqual(self.inventory.query("LAST", 10)["reserved"], 1)

    def test_cannot_settle_run_reservations_through_holder_api(self) -> None:
        self.init_document()
        self.reserve("r1", 10, ("U", 1))
        with self.assertRaises(Conflict):
            self.inventory.consume_held("r1", 12)
        with self.assertRaises(Conflict):
            self.inventory.release_held("r1", 12, "sneaky")
        self.assertEqual(self.inventory.query("U", 12)["reserved"], 1)

    def test_run_id_cannot_reuse_existing_holder(self) -> None:
        self.init_document()
        self.inventory.hold("shared", [("U", 1)], 10)
        with self.assertRaises(Conflict):
            self.reserve("shared", 10, ("U", 1))
        # and the reverse direction
        self.reserve("run-x", 10, ("U", 1))
        with self.assertRaises(Conflict):
            self.inventory.hold("run-x", [("U", 1)], 10)


class StructuralInvariantTests(StoreTestCase):
    def test_over_reservation_aborts_at_dbstore_level(self) -> None:
        self.init_document()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO reservations(reservation_id, holder_id, bottle_id, "
                "portions, status, created_day) "
                "VALUES('raw', 'h', 'LAST', 2, 'active', 10)"
            )

    def test_settled_reservation_cannot_change_state(self) -> None:
        self.init_document()
        self.reserve("r1", 10, ("U", 1))
        self.runs.start_run("r1", 12)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE reservations SET status = 'active' "
                "WHERE holder_id = 'r1'"
            )

    def test_opened_day_cannot_be_cleared_or_moved(self) -> None:
        self.init_document()
        self.inventory.open_bottle("U", 11)
        for value in (None, 12, 10):
            with self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(
                    "UPDATE bottles SET opened_day = ? WHERE bottle_id = 'U'",
                    (value,),
                )

    def test_settled_reservation_quantities_are_frozen(self) -> None:
        self.init_document()
        self.reserve("r1", 10, ("U", 1))
        self.runs.start_run("r1", 12)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE reservations SET portions = 3 WHERE holder_id = 'r1'"
            )

    def test_terminal_run_and_its_demand_are_frozen(self) -> None:
        self.init_document()
        self.reserve("r1", 10, ("U", 1))
        self.runs.start_run("r1", 12)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE runs SET method_revision = 99 WHERE run_id = 'r1'"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE run_requirements SET portions = 2 WHERE run_id = 'r1'"
            )


class PersistenceAndConcurrencyTests(StoreTestCase):
    def test_close_and_reopen_preserves_facts(self) -> None:
        self.init_document()
        self.reserve("r1", 10, ("U", 1))
        self.conn.close()
        self.conn = connect(self.db_path)
        inventory = InventoryService(self.conn)
        runs = RunService(self.conn)
        alloc = inventory.query("U", 10)
        self.assertEqual(alloc["reserved"], 1)
        receipt = runs.start_run("r1", 12)
        self.assertEqual(receipt["state"], "started")
        self.assertEqual(inventory.query("U", 12)["opened_day"], 12)
        self.assertEqual(inventory.conservation()[2]["consumed"], 1)

    def test_two_connections_race_for_last_portion(self) -> None:
        self.init_document()
        barrier = threading.Barrier(2)
        outcomes: dict[str, object] = {}
        lock = threading.Lock()

        def worker(run_id: str) -> None:
            conn = connect(self.db_path)
            try:
                barrier.wait(timeout=5)
                try:
                    RunService(conn).create_run(
                        run_id,
                        "M",
                        1,
                        10,
                        [{"bottle_id": "LAST", "portions": 1}],
                    )
                    result: object = "ok"
                except Exception as exc:  # noqa: BLE001 - inspected below
                    result = exc
            finally:
                conn.close()
            with lock:
                outcomes[run_id] = result

        threads = [
            threading.Thread(target=worker, args=(rid,))
            for rid in ("a", "b")
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        winners = [rid for rid, out in outcomes.items() if out == "ok"]
        losers = [rid for rid, out in outcomes.items() if isinstance(out, Exception)]
        self.assertEqual(len(winners), 1, outcomes)
        self.assertEqual(len(losers), 1, outcomes)
        self.assertIsInstance(outcomes[losers[0]], InsufficientStock)
        self.assertEqual(self.inventory.query("LAST", 10)["reserved"], 1)


class ScaleTests(StoreTestCase):
    def test_100_bottles_and_200_runs(self) -> None:
        document = {
            "initial_day": 1,
            "kinds": [{"kind": "R", "open_lifetime_days": 1000}],
            "bottles": [
                {
                    "bottle_id": f"B{i:03d}",
                    "kind": "R",
                    "total_portions": 2,
                    "intake_day": 1,
                    "printed_expiry_day": 10000,
                    "opened_day": None,
                }
                for i in range(100)
            ],
            "methods": [
                {"method_id": "M", "revision": 1, "requirements": {"R": 1}}
            ],
        }
        self.init_document(document)
        for i in range(200):
            bottle = f"B{i % 100:03d}"
            self.runs.create_run(
                f"run{i:03d}",
                "M",
                1,
                1,
                [{"bottle_id": bottle, "portions": 1}],
            )
        # each bottle got exactly two reservations -> full but conserved
        self.assertFalse(
            [r for r in self.inventory.conservation() if r["unallocated"] != 0]
        )
        with self.assertRaises(InsufficientStock):
            self.runs.create_run(
                "overflow",
                "M",
                1,
                1,
                [{"bottle_id": "B000", "portions": 1}],
            )
        total = sum(r["total_portions"] for r in self.inventory.conservation())
        self.assertEqual(total, 200)


class OperationFileTests(StoreTestCase):
    def test_719_operation_file_end_to_end(self) -> None:
        from reagent.cli import execute_operations

        root = Path(__file__).resolve().parent.parent
        document = json.loads(
            (root / "materials/719/inventory.json").read_text(encoding="utf-8")
        )
        operations = json.loads(
            (root / "materials/719/operations.json").read_text(encoding="utf-8")
        )
        self.inventory.initialize(document)
        results = execute_operations(self.conn, operations)
        by_op = {(r["op"], r.get("run_id")): r["result"] for r in results}

        competition = by_op[("reserve_competition", None)]
        self.assertEqual(len(competition["winners"]), 1)
        self.assertEqual(len(competition["losers"]), 1)
        self.assertIn(
            competition["losers"][0], ("last-1", "last-2")
        )

        self.assertEqual(by_op[("start", "expire-P")]["state"], "released")
        self.assertEqual(by_op[("start", "reserve-U")]["started_day"], 12)
        self.assertTrue(by_op[("repeat_start", "reserve-U")]["replayed"])
        self.assertEqual(
            by_op[("create", "new-need")]["requirements"], {"R": 2}
        )
        self.assertEqual(by_op[("start", "new-need")]["state"], "started")

        queries = [r["result"] for r in results if r["op"] == "query"]
        self.assertEqual(len(queries), 2)
        day13, day14 = queries
        self.assertEqual((day13["unallocated"], day13["leadable_today"]), (1, 1))
        self.assertEqual((day14["unallocated"], day14["leadable_today"]), (1, 0))

        p = self.inventory.query("P", 11)
        self.assertEqual(
            (p["unallocated"], p["reserved"], p["consumed"], p["leadable_today"]),
            (2, 0, 0, 0),
        )
        self.assertEqual(self.inventory.query("U", 12)["opened_day"], 12)


if __name__ == "__main__":
    unittest.main(verbosity=2)
