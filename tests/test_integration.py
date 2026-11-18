"""End-to-end: reopen persistence, atomicity, rejection side effects, scale."""
from __future__ import annotations

import json
import random
import tempfile
import unittest
from pathlib import Path

from support import MATERIALS, MaterialServiceCase

from address_delivery import AddressService
from address_delivery.catalog import Catalog
from address_delivery.db import connect
from address_delivery.errors import InputFormatError
from address_delivery.input import parse_csv_text


class ReopenTests(unittest.TestCase):
    """Close the process-level service, reopen the same file, verify state."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "persist.db"

    def test_reopen_preserves_jobs_revisions_and_frozen_facts(self):
        svc = AddressService(str(self.db))
        svc.load_initial(MATERIALS / "catalog.json", MATERIALS / "coverage.json")
        svc.import_csv(MATERIALS / "addresses.csv")
        svc.apply_revision(MATERIALS and (MATERIALS / "revision.json"))
        frozen_before = svc.delivery.get_job("P2")
        svc.close()

        svc = AddressService.open(self.db)
        try:
            self.assertEqual(svc.catalog.revision, "r2")
            self.assertEqual(svc.catalog.version, 2)
            frozen_after = svc.delivery.get_job("P2")
            self.assertEqual(frozen_before["street_id"], frozen_after["street_id"])
            self.assertEqual(frozen_before["catalog_version"], frozen_after["catalog_version"])
            self.assertEqual(frozen_before["station_id"], frozen_after["station_id"])
            # A reopened service still replays and still refuses deactivated S2.
            self.assertEqual(svc.replay_submitted("P2").outcome, "replayed")
            self.assertEqual(
                svc.submit_new("P7", "C1", "西", "东路", 2).outcome,
                "failed_street_inactive",
            )
        finally:
            svc.close()

    def test_catalog_only_database_can_be_reopened(self):
        catalog_doc = json.loads((MATERIALS / "catalog.json").read_text("utf-8"))
        coverage_doc = json.loads((MATERIALS / "coverage.json").read_text("utf-8"))
        conn = connect(self.db)
        try:
            Catalog(conn).load_initial(catalog_doc, coverage_doc)
        finally:
            conn.close()
        conn = connect(self.db)
        try:
            catalog = Catalog(conn)
            self.assertEqual(catalog.resolve("C1", "东", "东路", 9).street.street_id, "S1")
            self.assertEqual(catalog.get_street("S2")["name"], "东路")
        finally:
            conn.close()

    def test_rejected_file_leaves_no_rows_across_reopen(self):
        svc = AddressService(str(self.db))
        svc.load_initial(MATERIALS / "catalog.json", MATERIALS / "coverage.json")
        bad = Path(self.tmp.name) / "bad.csv"
        bad.write_text(
            "parcel_id,city_id,district_text,street_text,house_no\n"
            "P1,C1,东,东路,2\n"
            "P2,C1,东,东路,2\n"
            "P3,C1,东,东路,oops\n",
            encoding="utf-8",
        )
        with self.assertRaises(InputFormatError):
            svc.import_csv(bad)
        svc.close()

        svc = AddressService.open(self.db)
        try:
            self.assertEqual(svc.delivery.list_jobs(), [])
            for pid in ("P1", "P2", "P3"):
                self.assertEqual(svc.delivery.get_attempts(pid), [])
        finally:
            svc.close()


class AtomicityTests(MaterialServiceCase):
    def test_job_and_attempt_commit_together(self):
        self.svc.import_csv(MATERIALS / "addresses.csv")
        job = self.svc.delivery.get_job("P2")
        attempts = self.svc.delivery.get_attempts("P2")
        self.assertIsNotNone(job)
        self.assertTrue(attempts[-1]["job_created"])

    def test_injected_failure_rolls_back_job_and_attempt(self):
        """A failure after the INSERT must not leave a partial job behind."""
        import address_delivery.delivery as delivery_mod

        original = delivery_mod.DeliveryStore._insert_attempt

        def boom(self, *args, **kwargs):
            raise RuntimeError("injected failure")

        delivery_mod.DeliveryStore._insert_attempt = boom
        try:
            with self.assertRaises(RuntimeError):
                self.svc.submit_new("PX", "C1", "东", "学园路", 2)
        finally:
            delivery_mod.DeliveryStore._insert_attempt = original
        self.assertIsNone(self.svc.delivery.get_job("PX"))
        self.assertEqual(self.svc.delivery.get_attempts("PX"), [])
        # The store remains usable.
        result = self.svc.submit_new("PX", "C1", "东", "学园路", 2)
        self.assertEqual(result.outcome, "submitted")

    def test_replay_writes_attempt_only(self):
        self.svc.import_csv(MATERIALS / "addresses.csv")
        before_job = self.svc.delivery.get_job("P2")
        self.svc.replay_submitted("P2")
        after_job = self.svc.delivery.get_job("P2")
        self.assertEqual(before_job["submitted_at"], after_job["submitted_at"])
        self.assertEqual(after_job["catalog_version"], before_job["catalog_version"])
        self.assertEqual(len(self.svc.delivery.get_attempts("P2")), 2)


class ScaleTests(unittest.TestCase):
    """5 cities, 50 streets, 500 parcels - also checks update/invalidation."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc = AddressService(str(Path(self.tmp.name) / "scale.db"))
        self.addCleanup(self.svc.close)
        rng = random.Random(20241118)
        self.rng = rng
        cities, districts, streets, coverages = [], [], [], []
        street_no = 0
        for c in range(5):
            city_id = f"C{c}"
            cities.append({"city_id": city_id, "name": f"城市{c}"})
            for d in range(2):  # 10 districts
                district_id = f"D{c}_{d}"
                districts.append(
                    {
                        "district_id": district_id,
                        "city_id": city_id,
                        "name": f"区{d}",
                        "aliases": ["同名"],
                    }
                )
                for s in range(5):  # 50 streets total
                    street_id = f"S{street_no:02d}"
                    street_no += 1
                    streets.append(
                        {
                            "street_id": street_id,
                            "district_id": district_id,
                            "name": "同名路" if s == 0 else f"路{s}",
                            "aliases": [],
                            "active": True,
                        }
                    )
                    coverages.append(
                        {
                            "coverage_id": f"K{street_id}",
                            "street_id": street_id,
                            "station_id": f"ST{street_id}",
                            "lo": 1,
                            "hi": 100,
                            "parity": "any",
                        }
                    )
        self.svc.catalog.load_initial(
            {"revision": "s1", "cities": cities, "districts": districts, "streets": streets},
            {"revision": "s1", "coverages": coverages},
        )
        self.assertEqual(len(cities), 5)
        self.assertEqual(len(streets), 50)

    def test_500_parcels_are_attempted_independently(self):
        from address_delivery.models import AddressInput

        items = []
        for i in range(500):
            street = self.rng.choice(self.svc.catalog.list_streets())
            items.append(
                AddressInput(
                    f"Z{i:03d}",
                    street["city_id"],
                    "同名",  # matches the two districts within the city
                    street["name"],
                    (i % 100) + 1,
                    source_file="scale.csv",
                    source_row=i + 2,
                )
            )
        batch = self.svc.attempt_items(items)
        self.assertEqual(len(batch.rows), 500)
        # Every parcel either committed or failed with a structured outcome;
        # none raised, and successful rows have a job.
        for result in batch.rows:
            self.assertIn(result.outcome, {"submitted"} | {
                "failed_ambiguous_address", "failed_street_unknown",
                "failed_street_inactive", "failed_station_none",
                "failed_station_ambiguous", "failed_district_unknown",
                "failed_city_unknown", "replayed",
            })
            if result.outcome == "submitted":
                self.assertIsNotNone(result.job)
        self.assertEqual(len(self.svc.delivery.list_jobs()), batch.as_dict()["submitted"])

    def test_revision_invalidates_only_unfrozen_resolution(self):
        from address_delivery.models import AddressInput

        items = [
            AddressInput("F1", "C0", "同名", "同名路", 4),
            AddressInput("F2", "C0", "区1", "路1", 4),
        ]
        results = {r.parcel_id: r for r in self.svc.attempt_items(items).rows}
        # Both streets named 同名路 in the two districts -> ambiguous, no job.
        self.assertEqual(results["F1"].outcome, "failed_ambiguous_address")
        self.assertEqual(results["F2"].outcome, "submitted")
        frozen = self.svc.delivery.get_job("F2")

        # Rename the street F2 resolved through; the frozen parcel is unaffected.
        self.svc.apply_revision(
            {"revision": "s2", "operations": [
                {"op": "add_street_alias", "street_id": results["F2"].job["street_id"], "alias": "新名"}
            ]}
        )
        replay = self.svc.replay_submitted("F2")
        self.assertEqual(replay.outcome, "replayed")
        self.assertEqual(replay.job["street_id"], frozen["street_id"])
        # Subsequent resolution of the *new* name works (cache invalidation is
        # automatic because nothing is cached across revisions).
        new = self.svc.catalog.resolve("C0", "区1", "新名", 4)
        self.assertEqual(new.status, "unique")

    def test_batch_fetch_index_supports_scoped_lookup(self):
        # Same street text across two districts of one city is genuinely
        # ambiguous; per-city scoping keeps other cities out of the candidate set.
        res = self.svc.catalog.resolve("C0", "同名", "同名路", 1)
        self.assertEqual(res.status, "ambiguous")
        self.assertEqual({c.city_id for c in res.candidates}, {"C0"})
        res_other = self.svc.catalog.resolve("C1", "同名", "同名路", 1)
        self.assertEqual({c.city_id for c in res_other.candidates}, {"C1"})


if __name__ == "__main__":
    unittest.main()
