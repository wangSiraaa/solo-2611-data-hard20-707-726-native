"""Edge cases: revision atomicity, failure/success ordering, duplicates, queries."""
from __future__ import annotations

import unittest

from support import MATERIALS, MaterialServiceCase

from address_delivery.delivery import (
    FAILED_STREET_UNKNOWN,
    REPLAYED,
    REJECTED_IDENTITY,
    SUBMITTED,
)
from address_delivery.errors import CatalogError
from address_delivery.models import AddressInput


class RevisionAtomicityTests(MaterialServiceCase):
    def test_failed_revision_applies_nothing(self):
        before_version = self.svc.catalog.version
        before_s1 = self.svc.catalog.get_street("S1")
        revision = {
            "revision": "rX",
            "operations": [
                # Valid op first...
                {"op": "add_street_alias", "street_id": "S1", "alias": "新增"},
                # ...then one that must abort the whole revision.
                {"op": "deactivate_street", "street_id": "NOPE"},
            ],
        }
        with self.assertRaises(CatalogError):
            self.svc.apply_revision(revision)
        self.assertEqual(self.svc.catalog.version, before_version)
        self.assertEqual(self.svc.catalog.revision, "r1")
        self.assertEqual(self.svc.catalog.get_street("S1"), before_s1)

    def test_unknown_operation_aborts_revision(self):
        with self.assertRaises(CatalogError):
            self.svc.apply_revision(
                {"revision": "rX", "operations": [{"op": "explode_street"}]}
            )
        self.assertEqual(self.svc.catalog.version, 1)

    def test_cannot_remove_primary_name_as_alias(self):
        with self.assertRaises(CatalogError):
            self.svc.apply_revision(
                {"revision": "rX", "operations": [
                    {"op": "remove_street_alias", "street_id": "S1", "alias": "东路"}
                ]}
            )

    def test_add_and_remove_alias_roundtrip(self):
        self.svc.apply_revision(
            {"revision": "r2a", "operations": [
                {"op": "add_street_alias", "street_id": "S1", "alias": "临时"}
            ]}
        )
        self.assertEqual(self.svc.catalog.resolve("C1", "东", "临时", 2).street.street_id, "S1")
        self.svc.apply_revision(
            {"revision": "r2b", "operations": [
                {"op": "remove_street_alias", "street_id": "S1", "alias": "临时"}
            ]}
        )
        self.assertEqual(self.svc.catalog.resolve("C1", "东", "临时", 2).status, "unknown")


class FailureSuccessOrderingTests(MaterialServiceCase):
    def test_failures_do_not_freeze_identity(self):
        # P4 fails under r1 (street unknown).
        self.svc.import_csv(MATERIALS / "addresses.csv")
        self.assertEqual(self.svc.delivery.get_attempts("P4")[0]["outcome"], FAILED_STREET_UNKNOWN)
        self.assertIsNone(self.svc.delivery.get_job("P4"))
        # Submit P4 with different content while still uncommitted: allowed.
        other = self.svc.submit_new("P4", "C1", "东", "学园路", 2)
        self.assertEqual(other.outcome, SUBMITTED)
        # Now identity is frozen to the successful submission.
        conflict = self.svc.submit_new("P4", "C1", "东", "东部支路", 3)
        self.assertEqual(conflict.outcome, REJECTED_IDENTITY)

    def test_retry_after_success_replays_instead_of_resolving(self):
        self.svc.import_csv(MATERIALS / "addresses.csv")
        first = self.svc.submit_new("P4", "C1", "东", "学园路", 2)
        self.assertEqual(first.outcome, SUBMITTED)
        self.apply_revision()  # would make the original P4 resolvable to S1
        retried = self.svc.retry(["P4"])[0]
        # Replay, not a re-resolve to S1.
        self.assertEqual(retried.outcome, REPLAYED)
        self.assertEqual(retried.job["street_id"], "S3")

    def test_duplicate_parcel_ids_in_one_batch(self):
        items = [
            AddressInput("DUP", "C1", "东", "学园路", 2, source_file="a.csv", source_row=2),
            AddressInput("DUP", "C1", "东", "学园路", 2, source_file="a.csv", source_row=3),
            AddressInput("DUP", "C1", "东", "学园路", 3, source_file="a.csv", source_row=4),
        ]
        results = self.svc.attempt_items(items).rows
        self.assertEqual([r.outcome for r in results], [SUBMITTED, REPLAYED, REJECTED_IDENTITY])
        self.assertEqual(self.svc.delivery.get_job("DUP")["house_no"], 2)

    def test_identity_is_normalised_not_raw(self):
        first = self.svc.submit_new("N1", "C1", "东", "学园路", 2)
        self.assertEqual(first.outcome, SUBMITTED)
        # Leading/trailing spaces differ in raw text but not in identity.
        second = self.svc.submit_new("N1", "C1", "  东 ", " 学园路 ", 2)
        self.assertEqual(second.outcome, REPLAYED)


class QueryEntryPointTests(MaterialServiceCase):
    def test_resolve_shows_coverage_per_candidate_but_stays_ambiguous(self):
        payload = self.svc.resolve_address("C1", "同区", "东路", 2)
        self.assertEqual(payload["status"], "ambiguous")
        self.assertIsNone(payload["coverage"]["station_id"])
        # Both candidates report their own coverage; S1 house 2 -> station A.
        by_street = {c["street_id"]: c for c in payload["candidate_coverage"]}
        self.assertEqual(by_street["S1"]["station_id"], "A")
        # S2 only covers even numbers, 2 is even -> station B.
        self.assertEqual(by_street["S2"]["station_id"], "B")
        # Coverage never collapses the candidate set.
        self.assertEqual(len(payload["candidates"]), 2)

    def test_resolve_unidentified_shows_no_coverage(self):
        payload = self.svc.resolve_address("C1", "东", "不存在路", 2)
        self.assertEqual(payload["status"], "unknown")
        self.assertIsNone(payload["coverage"]["station_id"])
        self.assertEqual(payload["candidate_coverage"], [])

    def test_catalog_street_historical_lookup(self):
        self.apply_revision()
        street = self.svc.catalog_street("S2")
        self.assertFalse(street["active"])
        self.assertEqual(street["names"], ["东路"])
        self.assertEqual(street["deactivated_version"], 2)
        self.assertEqual(street["district_id"], "D2")


if __name__ == "__main__":
    unittest.main()
