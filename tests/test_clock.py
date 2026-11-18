"""Catalogue and point-resolution tests."""

from __future__ import annotations

import unittest

from clocklab import (
    ClockCatalogue,
    ClockStore,
    InvalidCatalogue,
    InvalidOccurrence,
    OutOfDomain,
    UnknownZone,
)
from clocklab.clock import validate_segments

from support import MULTI_FALLBACK, fresh_ledger, load_material


def utcs(candidates):
    return [
        candidate.utc if hasattr(candidate, "utc") else candidate["utc"]
        for candidate in candidates
    ]


class CatalogueValidationTests(unittest.TestCase):
    def test_material_clock_parses(self):
        catalogue = ClockCatalogue.from_document(load_material("clock.json"))
        self.assertEqual(catalogue.revision, "r1")
        zone = catalogue.zone("Z")
        self.assertEqual(zone.domain, (0, 600))
        self.assertEqual(len(zone), 3)

    def test_segments_are_contiguous_and_cover_domain(self):
        good = [
            {"utc_start": 0, "utc_end": 10, "offset": 0},
            {"utc_start": 10, "utc_end": 20, "offset": 5},
        ]
        self.assertEqual(len(validate_segments(good, (0, 20))), 2)

        cases = {
            "hole": [
                {"utc_start": 0, "utc_end": 10, "offset": 0},
                {"utc_start": 11, "utc_end": 20, "offset": 0},
            ],
            "overflow": [
                {"utc_start": 0, "utc_end": 12, "offset": 0},
                {"utc_start": 10, "utc_end": 20, "offset": 0},
            ],
            "short": [
                {"utc_start": 0, "utc_end": 10, "offset": 0},
            ],
            "late_start": [
                {"utc_start": 1, "utc_end": 20, "offset": 0},
            ],
            "empty": [
                {"utc_start": 0, "utc_end": 0, "offset": 0},
                {"utc_start": 0, "utc_end": 20, "offset": 0},
            ],
            "reversed": [
                {"utc_start": 10, "utc_end": 0, "offset": 0},
            ],
        }
        for name, segments in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(InvalidCatalogue):
                    validate_segments(segments, (0, 20))

    def test_integer_fields_are_not_coerced(self):
        for bad in (True, 1.5, "3", None):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidCatalogue):
                    validate_segments(
                        [
                            {"utc_start": 0, "utc_end": 10, "offset": 0},
                            {"utc_start": 10, "utc_end": 20, "offset": bad},
                        ],
                        (0, 20),
                    )

    def test_domain_must_be_non_empty(self):
        with self.assertRaises(InvalidCatalogue):
            validate_segments(
                [{"utc_start": 5, "utc_end": 5, "offset": 0}], (5, 5)
            )

    def test_duplicate_zone_is_rejected(self):
        with self.assertRaises(InvalidCatalogue):
            ClockCatalogue.from_document(
                {"revision": "x", "zones": [
                    {"zone_id": "A", "utc_domain": [0, 1],
                     "segments": [{"utc_start": 0, "utc_end": 1, "offset": 0}]},
                    {"zone_id": "A", "utc_domain": [0, 1],
                     "segments": [{"utc_start": 0, "utc_end": 1, "offset": 0}]},
                ]}
            )

    def test_unknown_zone(self):
        catalogue = ClockCatalogue.from_document(load_material("clock.json"))
        with self.assertRaises(UnknownZone):
            catalogue.zone("nope")


class PointResolutionTests(unittest.TestCase):
    def setUp(self):
        self.ledger = fresh_ledger()

    def test_public_anchors(self):
        self.assertEqual(utcs(self.ledger.resolve_point(90, "Z")["candidates"]), [30])
        self.assertEqual(self.ledger.resolve_point(200, "Z")["candidates"], [])
        self.assertEqual(
            utcs(self.ledger.resolve_point(330, "Z")["candidates"]), [210, 270]
        )

    def test_candidates_carry_source_segment(self):
        point = self.ledger.resolve_point(330, "Z")
        first, second = point["candidates"]
        self.assertEqual((first["segment_index"], first["offset"]), (1, 120))
        self.assertEqual((second["segment_index"], second["offset"]), (2, 60))
        self.assertEqual((first["segment_utc_start"], first["segment_utc_end"]), (120, 240))
        self.assertEqual((second["segment_utc_start"], second["segment_utc_end"]), (240, 600))

    def test_gap_has_no_candidate_and_is_not_shifted(self):
        self.assertEqual(self.ledger.resolve_point(200, "Z")["status"], "no_candidate")

    def test_half_open_local_boundaries(self):
        # Segment 0 produces local [60, 180); segment 1 starts producing 240.
        self.assertEqual(utcs(self.ledger.resolve_point(179, "Z")["candidates"]), [119])
        # local 180 is the excluded end of segment 0 and does not yet exist.
        self.assertEqual(self.ledger.resolve_point(180, "Z")["candidates"], [])
        self.assertEqual(utcs(self.ledger.resolve_point(240, "Z")["candidates"]), [120])
        # Segment 1's local range is [240, 360); segment 2 starts at 300.
        self.assertEqual(
            utcs(self.ledger.resolve_point(300, "Z")["candidates"]), [180, 240]
        )

    def test_occurrence_selection(self):
        earlier = self.ledger.resolve_point(330, "Z", occurrence="earlier")
        later = self.ledger.resolve_point(330, "Z", occurrence="later")
        self.assertEqual(earlier["chosen"]["utc"], 210)
        self.assertEqual(later["chosen"]["utc"], 270)
        self.assertEqual(earlier["status"], "selected")

    def test_single_candidate_ignores_occurrence(self):
        for occurrence in (None, "earlier", "later"):
            with self.subTest(occurrence=occurrence):
                point = self.ledger.resolve_point(90, "Z", occurrence=occurrence)
                self.assertEqual(point["chosen"]["utc"], 30)
                self.assertEqual(point["status"], "unique")

    def test_invalid_occurrence(self):
        with self.assertRaises(InvalidOccurrence):
            self.ledger.resolve_point(330, "Z", occurrence="middle")

    def test_multi_fallback_enumerates_every_candidate(self):
        ledger = fresh_ledger(MULTI_FALLBACK)
        # Local ranges are [300,600), [450,650) and [500,600); 550 is covered
        # by all three, so all three occurrences must be reported.
        point = ledger.resolve_point(550, "M")
        self.assertEqual(utcs(point["candidates"]), [250, 400, 550])
        self.assertEqual(
            [c["segment_index"] for c in point["candidates"]], [0, 1, 2]
        )
        earlier = ledger.resolve_point(550, "M", occurrence="earlier")
        later = ledger.resolve_point(550, "M", occurrence="later")
        self.assertEqual(earlier["chosen"]["utc"], 250)
        self.assertEqual(later["chosen"]["utc"], 550)

    def test_point_query_needs_no_sessions(self):
        # A freshly created ledger has no session rows at all.
        self.assertEqual(
            self.ledger.conn.execute("SELECT COUNT(*) AS n FROM session").fetchone()["n"],
            0,
        )
        self.assertEqual(utcs(self.ledger.resolve_point(90, "Z")["candidates"]), [30])

    def test_utc_to_local_is_unique(self):
        zone = self.ledger.catalogue("r1").zone("Z")
        self.assertEqual(zone.utc_to_local(0), 60)
        self.assertEqual(zone.utc_to_local(119), 179)
        self.assertEqual(zone.utc_to_local(120), 240)  # half-open: 120 is segment 1
        self.assertEqual(zone.utc_to_local(240), 300)
        with self.assertRaises(OutOfDomain):
            zone.utc_to_local(600)
        with self.assertRaises(OutOfDomain):
            zone.utc_to_local(-1)

    def test_stored_point_query_can_pin_a_revision(self):
        ledger = fresh_ledger()
        ledger.apply_revision(load_material("revision.json"))
        old = ledger.resolve_point(330, "Z", revision="r1")
        new = ledger.resolve_point(330, "Z", revision="r2")
        self.assertEqual(utcs(old["candidates"]), [210, 270])
        self.assertEqual(utcs(new["candidates"]), [270])  # r2 endpoint 330 is unique
        self.assertEqual(ledger.active_revision(), "r2")


class ClockStoreTests(unittest.TestCase):
    def test_failed_preview_does_not_install(self):
        store = ClockStore(ClockCatalogue.from_document(load_material("clock.json")))
        with self.assertRaises(InvalidCatalogue):
            store.preview_revision(
                {
                    "revision": "bad",
                    "zone_id": "Z",
                    "replace_segments": [
                        {"utc_start": 0, "utc_end": 300, "offset": 0},
                        {"utc_start": 400, "utc_end": 600, "offset": 0},
                    ],
                }
            )
        self.assertEqual(store.revisions(), ["r1"])
        self.assertEqual(store.active.revision, "r1")

    def test_revision_replaces_only_named_zone(self):
        document = {
            "revision": "r1",
            "zones": [
                {
                    "zone_id": "A",
                    "utc_domain": [0, 10],
                    "segments": [{"utc_start": 0, "utc_end": 10, "offset": 1}],
                },
                {
                    "zone_id": "B",
                    "utc_domain": [0, 10],
                    "segments": [{"utc_start": 0, "utc_end": 10, "offset": 2}],
                },
            ],
        }
        store = ClockStore(ClockCatalogue.from_document(document))
        store.apply_revision(
            {
                "revision": "r2",
                "zone_id": "A",
                "replace_segments": [
                    {"utc_start": 0, "utc_end": 10, "offset": 5}
                ],
            }
        )
        self.assertEqual(store.active.zone("A").segments[0].offset, 5)
        self.assertEqual(store.active.zone("B").segments[0].offset, 2)
        # The old revision is untouched.
        self.assertEqual(store.get("r1").zone("A").segments[0].offset, 1)


if __name__ == "__main__":
    unittest.main()
