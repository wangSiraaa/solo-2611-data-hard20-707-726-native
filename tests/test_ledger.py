"""Ledger tests: atomicity, idempotency, immutability, summaries, revisions."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from clocklab import (
    ClockCatalogue,
    ConflictingEvaluation,
    ConflictingSession,
    InvalidCatalogue,
    Ledger,
    UnknownEvaluation,
    UnknownSession,
)
from clocklab.evaluate import EvaluationRejected

from support import fresh_ledger, load_material, session


J1 = session("J1", 300, 330, start_occurrence="earlier", end_occurrence="later")
J_GAP = session("J_GAP", 200, 260)


class LedgerBasicsTests(unittest.TestCase):
    def setUp(self):
        self.ledger = fresh_ledger()

    def test_session_registration_is_idempotent(self):
        self.ledger.register_session(J1)
        self.ledger.register_session(J1)
        self.assertEqual(len(self.ledger.list_sessions()), 1)

    def test_session_conflict_is_rejected(self):
        self.ledger.register_session(J1)
        changed = dict(J1, rate_per_utc_minute=3)
        with self.assertRaises(ConflictingSession):
            self.ledger.register_session(changed)
        self.assertEqual(
            self.ledger.get_session("J1")["definition"]["rate_per_utc_minute"], 2
        )

    def test_evaluate_unknown_session(self):
        with self.assertRaises(UnknownSession):
            self.ledger.evaluate("missing", "E")

    def test_read_unknown_evaluation(self):
        with self.assertRaises(UnknownEvaluation):
            self.ledger.read_evaluation("missing")


class EvaluationPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.ledger = fresh_ledger()
        self.ledger.register_session(J1)
        self.ledger.register_session(J_GAP)

    def test_success_stores_all_facts(self):
        outcome = self.ledger.evaluate("J1", "E1")["outcome"]
        row = self.ledger.conn.execute(
            "SELECT * FROM evaluation WHERE evaluation_id = 'E1'"
        ).fetchone()
        self.assertEqual(row["status"], "ok")
        self.assertEqual((row["utc_start"], row["utc_end"]), (180, 270))
        self.assertEqual(row["duration_minutes"], 90)
        self.assertEqual(row["rate_per_utc_minute"], 2)
        self.assertEqual(row["cost"], 180)
        self.assertEqual(outcome["segments"], [
            {"segment_index": 1, "utc_start": 180, "utc_end": 240, "offset": 120,
             "minutes": 60, "segment_utc_start": 120, "segment_utc_end": 240},
            {"segment_index": 2, "utc_start": 240, "utc_end": 270, "offset": 60,
             "minutes": 30, "segment_utc_start": 240, "segment_utc_end": 600},
        ])

    def test_rejection_stores_no_pay_facts(self):
        result = self.ledger.evaluate("J_GAP", "G1")
        self.assertEqual(result["status"], "rejected")
        row = self.ledger.conn.execute(
            "SELECT * FROM evaluation WHERE evaluation_id = 'G1'"
        ).fetchone()
        self.assertEqual(row["status"], "rejected")
        for column in ("utc_start", "utc_end", "duration_minutes",
                       "rate_per_utc_minute", "cost"):
            self.assertIsNone(row[column])
        self.assertEqual(self.ledger.list_evaluations(include_rejected=False), [])

    def test_identical_replay_returns_same_fact(self):
        first = self.ledger.evaluate("J1", "E1")
        second = self.ledger.evaluate("J1", "E1")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["outcome"], second["outcome"])
        self.assertEqual(
            self.ledger.conn.execute(
                "SELECT COUNT(*) AS n FROM evaluation"
            ).fetchone()["n"],
            1,
        )

    def test_identical_rejection_replay(self):
        self.ledger.evaluate("J_GAP", "G1")
        replay = self.ledger.evaluate("J_GAP", "G1")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["status"], "rejected")

    def test_conflicting_reuse_of_evaluation_id(self):
        self.ledger.evaluate("J1", "E1")
        with self.assertRaises(ConflictingEvaluation):
            self.ledger.evaluate("J_GAP", "E1")
        # Nothing new was written and E1 is still the J1 success.
        stored = self.ledger.read_evaluation("E1")
        self.assertEqual(stored["session_id"], "J1")
        self.assertEqual(stored["stored_cost"], 180)

    def test_new_evaluation_under_revision(self):
        self.ledger.evaluate("J1", "E1")
        self.ledger.apply_revision(load_material("revision.json"))
        result = self.ledger.evaluate("J1", "E2")
        self.assertEqual(result["outcome"]["revision"], "r2")
        self.assertEqual(result["outcome"]["utc_start"], 210)
        self.assertEqual(result["outcome"]["utc_end"], 270)
        self.assertEqual(result["outcome"]["duration_minutes"], 60)
        self.assertEqual(result["outcome"]["cost"], 120)

    def test_old_evaluation_keeps_its_own_table_and_amount(self):
        self.ledger.evaluate("J1", "E1")
        self.ledger.apply_revision(load_material("revision.json"))
        stored = self.ledger.read_evaluation("E1")
        self.assertEqual(stored["revision"], "r1")
        self.assertEqual(stored["stored_duration_minutes"], 90)
        self.assertEqual(stored["stored_cost"], 180)
        # Its embedded snapshot is r1, not the current active r2.
        self.assertEqual(
            stored["outcome"]["catalogue_snapshot"]["revision"], "r1"
        )
        snapshot_zone = stored["outcome"]["catalogue_snapshot"]["zones"][0]
        self.assertEqual(
            [seg["offset"] for seg in snapshot_zone["segments"]], [60, 120, 60]
        )
        self.assertEqual(
            stored["outcome"]["end"]["chosen"]["offset"], 60
        )
        # And the ledger still remembers r1 itself.
        self.assertEqual(
            self.ledger.resolve_point(330, "Z", revision="r1")["status"],
            "ambiguous",
        )


class RevisionAtomicityTests(unittest.TestCase):
    def setUp(self):
        self.ledger = fresh_ledger()
        self.ledger.register_session(J1)
        self.ledger.evaluate("J1", "E1")

    def _state(self):
        return (
            self.ledger.active_revision(),
            [r["revision"] for r in self.ledger.list_revisions()],
            self.ledger.conn.execute(
                "SELECT COUNT(*) AS n FROM evaluation"
            ).fetchone()["n"],
            self.ledger.read_evaluation("E1")["stored_cost"],
        )

    def test_invalid_revision_changes_nothing(self):
        before = self._state()
        bad = {
            "revision": "r_bad",
            "zone_id": "Z",
            "replace_segments": [
                {"utc_start": 0, "utc_end": 300, "offset": 0},
                {"utc_start": 400, "utc_end": 600, "offset": 0},
            ],
        }
        with self.assertRaises(InvalidCatalogue):
            self.ledger.apply_revision(bad)
        self.assertEqual(self._state(), before)

    def test_invalid_revision_bad_offset_type_changes_nothing(self):
        before = self._state()
        bad = {
            "revision": "r_bad2",
            "zone_id": "Z",
            "replace_segments": [
                {"utc_start": 0, "utc_end": 120, "offset": 60},
                {"utc_start": 120, "utc_end": 240, "offset": 90.5},
                {"utc_start": 240, "utc_end": 600, "offset": 60},
            ],
        }
        with self.assertRaises(InvalidCatalogue):
            self.ledger.apply_revision(bad)
        self.assertEqual(self._state(), before)

    def test_failed_evaluation_after_revision_leaves_no_pay(self):
        # In r2 the local 200 gap still exists, so J_GAP stays rejected, and a
        # conflicting E1 must not disturb the stored success.
        self.ledger.apply_revision(load_material("revision.json"))
        self.ledger.register_session(J_GAP)
        self.ledger.evaluate("J_GAP", "G2")
        with self.assertRaises(ConflictingEvaluation):
            self.ledger.evaluate("J_GAP", "E1")
        self.assertEqual(self.ledger.read_evaluation("E1")["stored_cost"], 180)

    def test_revision_replay_is_idempotent(self):
        self.ledger.apply_revision(load_material("revision.json"))
        self.ledger.apply_revision(load_material("revision.json"))
        self.assertEqual(
            [r["revision"] for r in self.ledger.list_revisions()], ["r1", "r2"]
        )

    def test_same_revision_name_with_new_content_is_refused(self):
        document = load_material("revision.json")
        document = dict(document, revision="r1")
        with self.assertRaises(Exception):
            self.ledger.apply_revision(document)


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.ledger = fresh_ledger()
        self.ledger.register_session(J1)
        self.ledger.evaluate("J1", "E1")  # real UTC [180, 270), 2/min

    def test_full_interval(self):
        summary = self.ledger.summarize(0, 600)
        self.assertEqual(summary["total_minutes"], 90)
        self.assertEqual(summary["total_cost"], 180)

    def test_intersection_not_whole_session(self):
        summary = self.ledger.summarize(180, 200)
        self.assertEqual(summary["total_minutes"], 20)
        self.assertEqual(summary["total_cost"], 40)
        item = summary["intersections"][0]
        self.assertEqual(item["session_minutes"], 90)
        self.assertEqual(item["overlap_utc_start"], 180)
        self.assertEqual(item["overlap_utc_end"], 200)
        self.assertEqual(item["overlap_minutes"], 20)

    def test_middle_slice(self):
        summary = self.ledger.summarize(200, 250)
        self.assertEqual(summary["total_minutes"], 50)
        self.assertEqual(summary["total_cost"], 100)

    def test_window_straddling_session_start(self):
        summary = self.ledger.summarize(100, 200)
        self.assertEqual(summary["total_minutes"], 20)
        self.assertEqual(summary["total_cost"], 40)

    def test_window_straddling_session_end(self):
        summary = self.ledger.summarize(250, 400)
        self.assertEqual(summary["total_minutes"], 20)
        self.assertEqual(summary["total_cost"], 40)

    def test_no_overlap(self):
        summary = self.ledger.summarize(0, 180)
        self.assertEqual(summary["total_minutes"], 0)
        self.assertEqual(summary["total_cost"], 0)

    def test_half_open_intersection(self):
        # [180, 181) is one minute; [179, 180) is none.
        self.assertEqual(self.ledger.summarize(180, 181)["total_minutes"], 1)
        self.assertEqual(self.ledger.summarize(179, 180)["total_minutes"], 0)
        self.assertEqual(self.ledger.summarize(270, 271)["total_minutes"], 0)

    def test_rejected_sessions_never_contribute(self):
        self.ledger.register_session(J_GAP)
        self.ledger.evaluate("J_GAP", "G1")
        summary = self.ledger.summarize(0, 600)
        self.assertEqual(summary["total_minutes"], 90)
        self.assertEqual(summary["total_cost"], 180)
        self.assertEqual(
            [i["session_id"] for i in summary["intersections"]], ["J1"]
        )

    def test_old_and_new_evaluations_are_separate_facts(self):
        self.ledger.apply_revision(load_material("revision.json"))
        self.ledger.evaluate("J1", "E2")  # r2: [210, 270), 60 min
        summary = self.ledger.summarize(0, 600, latest_per_session=False)
        # Both facts are stored; the all-facts view reports both eras.
        self.assertEqual(
            {i["evaluation_id"]: i["overlap_minutes"] for i in summary["intersections"]},
            {"E1": 90, "E2": 60},
        )

    def test_default_summary_settles_newest_fact_per_session(self):
        self.ledger.apply_revision(load_material("revision.json"))
        self.ledger.evaluate("J1", "E2")
        summary = self.ledger.summarize(0, 600)
        # The same work is never billed twice: E2 replaced E1 for settlement,
        # while both facts remain readable individually.
        self.assertEqual(summary["total_minutes"], 60)
        self.assertEqual(summary["total_cost"], 120)
        self.assertEqual(
            [i["evaluation_id"] for i in summary["intersections"]], ["E2"]
        )
        self.assertEqual(self.ledger.read_evaluation("E1")["stored_cost"], 180)

    def test_summary_can_pin_a_revision(self):
        self.ledger.apply_revision(load_material("revision.json"))
        self.ledger.evaluate("J1", "E2")
        self.assertEqual(
            self.ledger.summarize(0, 600, revision="r1")["total_minutes"], 90
        )
        self.assertEqual(
            self.ledger.summarize(0, 600, revision="r2")["total_minutes"], 60
        )
        self.assertEqual(
            self.ledger.summarize(0, 600, session_id="J1",
                                  revision="r2")["total_cost"],
            120,
        )

    def test_latest_rejection_removes_older_success_from_settlement(self):
        self.ledger.evaluate("J1", "E1")  # success under r1
        # A revision in which local 300 still resolves but local 330 is a gap.
        self.ledger.apply_revision(
            {
                "revision": "r3",
                "zone_id": "Z",
                "replace_segments": [
                    {"utc_start": 0, "utc_end": 120, "offset": 60},
                    {"utc_start": 120, "utc_end": 150, "offset": 180},
                    {"utc_start": 150, "utc_end": 400, "offset": -100},
                    {"utc_start": 400, "utc_end": 600, "offset": -400},
                ],
            }
        )
        self.assertEqual(self.ledger.resolve_point(300, "Z")["chosen"]["utc"], 120)
        self.assertEqual(self.ledger.resolve_point(330, "Z")["status"], "no_candidate")
        self.assertEqual(self.ledger.evaluate("J1", "E3")["status"], "rejected")
        # E1 is still readable, but the session's newest determination is a
        # rejection, so it contributes nothing to the default settlement view.
        self.assertEqual(self.ledger.read_evaluation("E1")["stored_cost"], 180)
        self.assertEqual(self.ledger.summarize(0, 600)["total_cost"], 0)
        # The historical view still exposes the old fact explicitly.
        self.assertEqual(
            self.ledger.summarize(0, 600, latest_per_session=False)["total_cost"],
            180,
        )

    def test_group_by_zone_and_revision(self):
        self.ledger.apply_revision(load_material("revision.json"))
        self.ledger.evaluate("J1", "E2")
        by_revision = self.ledger.summarize(0, 600, group_by="revision",
                                            latest_per_session=False)
        self.assertEqual(
            {g["key"]: g["minutes"] for g in by_revision["groups"]},
            {"r1": 90, "r2": 60},
        )
        by_zone = self.ledger.summarize(0, 600, group_by="zone")
        self.assertEqual(by_zone["groups"][0]["key"], "Z")

    def test_bad_bounds(self):
        with self.assertRaises(Exception):
            self.ledger.summarize(500, 100)
        with self.assertRaises(Exception):
            self.ledger.summarize(0, 10, group_by="nope")


class PersistenceTests(unittest.TestCase):
    def test_round_trip_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.sqlite"
            ledger = Ledger.open(path)
            ledger.install_catalogue(
                ClockCatalogue.from_document(load_material("clock.json"))
            )
            ledger.register_session(J1)
            ledger.evaluate("J1", "E1")
            ledger.apply_revision(load_material("revision.json"))
            ledger.evaluate("J1", "E2")
            ledger.close()

            reopened = Ledger.open(path)
            try:
                self.assertEqual(reopened.active_revision(), "r2")
                self.assertEqual(reopened.read_evaluation("E1")["stored_cost"], 180)
                self.assertEqual(reopened.read_evaluation("E2")["stored_cost"], 120)
                self.assertEqual(
                    [r["revision"] for r in reopened.list_revisions()], ["r1", "r2"]
                )
            finally:
                reopened.close()

    def test_schema_enforces_rejected_rows_have_no_pay(self):
        ledger = fresh_ledger()
        with self.assertRaises(sqlite3.IntegrityError):
            ledger.conn.execute(
                "INSERT INTO evaluation (evaluation_id, evaluation_seq, session_id,"
                " revision, zone_id, status, content_hash, outcome_json, cost)"
                " VALUES ('X', 99, 'S', 'r1', 'Z', 'rejected', 'h', '{}', 5)"
            )

    def test_events_are_recorded(self):
        ledger = fresh_ledger()
        ledger.register_session(J1)
        ledger.evaluate("J1", "E1")
        ledger.register_session(J_GAP)
        ledger.evaluate("J_GAP", "G1")
        ledger.apply_revision(load_material("revision.json"))
        ops = [event["op"] for event in ledger.events()]
        self.assertIn("evaluate", ops)
        self.assertIn("apply_clock_revision", ops)
        statuses = [
            event["detail"].get("status")
            for event in ledger.events()
            if event["op"] == "evaluate"
        ]
        self.assertIn("created", statuses)
        self.assertEqual(
            [e["detail"]["status"] for e in ledger.events()
             if e["op"] == "apply_clock_revision"],
            ["created", "activated"],
        )

    def test_invalid_revision_is_logged_without_changing_active(self):
        ledger = fresh_ledger()
        with self.assertRaises(InvalidCatalogue):
            ledger.apply_revision(
                {"revision": "bad", "zone_id": "Z", "replace_segments": []}
            )
        self.assertEqual(ledger.active_revision(), "r1")
        rejected = [
            e for e in ledger.events()
            if e["op"] == "apply_clock_revision"
            and e["detail"].get("status") == "rejected"
        ]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["detail"]["active"], "r1")


if __name__ == "__main__":
    unittest.main()
