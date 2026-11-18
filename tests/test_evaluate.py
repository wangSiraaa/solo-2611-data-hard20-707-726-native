"""Evaluation tests: endpoint interpretation, UTC duration, rejection."""

from __future__ import annotations

import unittest

from clocklab import EvaluationRejected, SessionInput, evaluate_session
from clocklab.evaluate import (
    AMBIGUOUS_LOCAL,
    NON_POSITIVE_DURATION,
    NO_LOCAL_CANDIDATE,
    UNKNOWN_ZONE,
    InvalidSession,
    resolve_endpoint,
)

from support import fresh_ledger, load_material, session


class SessionParsingTests(unittest.TestCase):
    def test_valid_session(self):
        parsed = SessionInput.from_mapping(session("S", 300, 330))
        self.assertEqual(parsed.local_start, 300)
        self.assertIsNone(parsed.start_occurrence)

    def test_rate_must_be_non_negative_integer(self):
        for rate in (-1, 1.0, True, "2"):
            with self.subTest(rate=rate):
                with self.assertRaises(InvalidSession):
                    SessionInput.from_mapping(session("S", 300, 330, rate=rate))

    def test_occurrence_must_be_known(self):
        with self.assertRaises(InvalidSession):
            SessionInput.from_mapping(
                session("S", 300, 330, start_occurrence="middle")
            )

    def test_required_fields(self):
        raw = session("S", 300, 330)
        del raw["rate_per_utc_minute"]
        with self.assertRaises(InvalidSession):
            SessionInput.from_mapping(raw)


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.ledger = fresh_ledger()
        self.catalogue = self.ledger.catalogue("r1")

    def evaluate(self, raw, evaluation_id="E"):
        return evaluate_session(
            SessionInput.from_mapping(raw), self.catalogue, evaluation_id
        )

    def test_hand_computed_r1(self):
        result = self.evaluate(
            session("J1", 300, 330, start_occurrence="earlier",
                    end_occurrence="later")
        )
        self.assertEqual(result.utc_start, 180)
        self.assertEqual(result.utc_end, 270)
        self.assertEqual(result.duration_minutes, 90)
        self.assertEqual(result.cost, 180)
        # The wall-clock difference is only 30 minutes; it must not be billed.
        self.assertNotEqual(result.duration_minutes, 330 - 300)

    def test_segments_are_reported(self):
        result = self.evaluate(
            session("J1", 300, 330, start_occurrence="earlier",
                    end_occurrence="later")
        )
        self.assertEqual(
            [(s["segment_index"], s["utc_start"], s["utc_end"], s["minutes"])
             for s in result.segments],
            [(1, 180, 240, 60), (2, 240, 270, 30)],
        )

    def test_candidates_are_preserved_in_result(self):
        result = self.evaluate(
            session("J1", 300, 330, start_occurrence="earlier",
                    end_occurrence="later")
        )
        self.assertEqual([c.utc for c in result.start.candidates], [180, 240])
        self.assertEqual([c.utc for c in result.end.candidates], [210, 270])
        self.assertEqual(result.start.chosen.utc, 180)
        self.assertEqual(result.end.chosen.utc, 270)

    def test_only_candidate_used_for_both_selectors(self):
        # local 90 is produced only by segment 0, so earlier/later/absent all
        # resolve to the same UTC instant.
        for start_occ, end_occ in ((None, None), ("earlier", "earlier"),
                                   ("later", "later"), ("earlier", "later")):
            with self.subTest(start=start_occ, end=end_occ):
                result = self.evaluate(
                    session("S2", 90, 120, start_occurrence=start_occ,
                            end_occurrence=end_occ)
                )
                self.assertEqual((result.utc_start, result.utc_end), (30, 60))
                self.assertEqual(result.duration_minutes, 30)

    def test_gap_endpoint_is_rejected(self):
        with self.assertRaises(EvaluationRejected) as ctx:
            self.evaluate(session("J_GAP", 200, 260))
        self.assertEqual(ctx.exception.code, NO_LOCAL_CANDIDATE)
        self.assertEqual(ctx.exception.detail["endpoint"], "start")

    def test_gap_end_is_rejected_too(self):
        with self.assertRaises(EvaluationRejected) as ctx:
            self.evaluate(session("GAPEND", 90, 200))
        self.assertEqual(ctx.exception.code, NO_LOCAL_CANDIDATE)
        self.assertEqual(ctx.exception.detail["endpoint"], "end")

    def test_ambiguous_without_selection_is_rejected(self):
        with self.assertRaises(EvaluationRejected) as ctx:
            self.evaluate(session("AMB", 300, 330))
        self.assertEqual(ctx.exception.code, AMBIGUOUS_LOCAL)
        self.assertEqual(ctx.exception.detail["endpoint"], "start")

    def test_ambiguous_end_without_selection_is_rejected(self):
        with self.assertRaises(EvaluationRejected) as ctx:
            self.evaluate(session("AMB2", 300, 330, start_occurrence="earlier"))
        self.assertEqual(ctx.exception.code, AMBIGUOUS_LOCAL)
        self.assertEqual(ctx.exception.detail["endpoint"], "end")

    def test_unknown_zone_is_rejected_not_raised_as_input_error(self):
        with self.assertRaises(EvaluationRejected) as ctx:
            self.evaluate(session("S", 90, 120, zone_id="nope"))
        self.assertEqual(ctx.exception.code, UNKNOWN_ZONE)

    def test_wall_clock_positive_but_real_utc_reversed(self):
        # With 'later' start (UTC 240) and 'earlier' end (UTC 210) the wall
        # clock still reads forward, but real time runs backwards.
        with self.assertRaises(EvaluationRejected) as ctx:
            self.evaluate(
                session("REV", 300, 330, start_occurrence="later",
                        end_occurrence="earlier")
            )
        self.assertEqual(ctx.exception.code, NON_POSITIVE_DURATION)
        self.assertEqual(ctx.exception.detail["utc_start"], 240)
        self.assertEqual(ctx.exception.detail["utc_end"], 210)

    def test_equal_real_time_is_rejected(self):
        with self.assertRaises(EvaluationRejected) as ctx:
            self.evaluate(session("ZERO", 90, 90))
        self.assertEqual(ctx.exception.code, NON_POSITIVE_DURATION)

    def test_zero_rate_is_allowed(self):
        result = self.evaluate(session("FREE", 90, 120, rate=0))
        self.assertEqual(result.cost, 0)

    def test_adjacent_segment_jump_uses_real_time(self):
        # local 179 -> UTC 119 (segment 0).  local 300 has candidates 180 and
        # 240; taking the earlier one gives UTC 180, exactly one minute after
        # 179 on the real clock even though the wall clock jumped 121.
        result = self.evaluate(session("JUMP", 179, 300, end_occurrence="earlier"))
        self.assertEqual((result.utc_start, result.utc_end), (119, 180))
        self.assertEqual(result.duration_minutes, 61)
        self.assertEqual(
            [s["segment_index"] for s in result.segments], [0, 1]
        )

    def test_edge_before_adjacent_segment_wrap(self):
        # local 300 has candidates 180 and 240; local 350 exists only in
        # segment 1 (UTC 230).  Choosing the later start gives a wall clock
        # that still runs forward (300 -> 350) while real time runs backward
        # (240 -> 230).
        with self.assertRaises(EvaluationRejected) as ctx:
            self.evaluate(
                session("WRAP", 300, 350, start_occurrence="later",
                        end_occurrence="earlier")
            )
        self.assertEqual(ctx.exception.code, NON_POSITIVE_DURATION)
        self.assertEqual(ctx.exception.detail["utc_start"], 240)
        self.assertEqual(ctx.exception.detail["utc_end"], 230)


class EndpointTests(unittest.TestCase):
    def test_resolve_endpoint_reports_status(self):
        zone = fresh_ledger().catalogue("r1").zone("Z")
        self.assertEqual(resolve_endpoint(zone, 90, None).status, "unique")
        self.assertEqual(resolve_endpoint(zone, 200, None).status, "no_candidate")
        self.assertEqual(resolve_endpoint(zone, 330, None).status, "ambiguous")
        selected = resolve_endpoint(zone, 330, "later")
        self.assertEqual(selected.status, "selected")
        self.assertEqual(selected.chosen.utc, 270)


if __name__ == "__main__":
    unittest.main()
