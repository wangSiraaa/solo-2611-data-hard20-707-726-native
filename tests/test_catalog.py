"""Catalog behaviour: scoped names, full-chain resolution, normalisation, coverage."""
from __future__ import annotations

import unittest

from support import MATERIALS, MaterialServiceCase, fresh_synthetic_service

from address_delivery.errors import CatalogError
from address_delivery.models import AddressInput


class ScopedNameTests(MaterialServiceCase):
    def test_cross_district_same_street_name_is_ambiguous_not_an_error(self):
        res = self.svc.catalog.resolve("C1", "同区", "东路", 2)
        self.assertEqual(res.status, "ambiguous")
        self.assertEqual({c.street_id for c in res.candidates}, {"S1", "S2"})

    def test_district_alias_scope_disambiguates_full_chain(self):
        # 同区 matches both districts -> ambiguous; 东 selects only D1 -> S1.
        res = self.svc.catalog.resolve("C1", "东", "东路", 9)
        self.assertEqual(res.status, "unique")
        self.assertEqual(res.street.street_id, "S1")

    def test_unknown_district_text_yields_unknown(self):
        res = self.svc.catalog.resolve("C1", "不存在区", "东路", 1)
        self.assertEqual(res.status, "unknown")
        self.assertEqual(res.candidates, ())

    def test_unknown_city_is_reported_separately(self):
        res = self.svc.catalog.resolve("C9", "东", "东路", 1)
        self.assertEqual(res.status, "unknown")
        self.assertFalse(res.city_found)

    def test_district_name_is_scoped_to_city(self):
        svc = fresh_synthetic_service()
        self.addCleanup(svc.close)
        # "北区" exists in C1 and C2, but city confines the lookup.
        res = svc.catalog.resolve("C2", "北区", "中心路", 1)
        self.assertEqual(res.status, "unique")
        self.assertEqual(res.street.street_id, "S3")
        res_c1 = svc.catalog.resolve("C1", "北区", "中心路", 1)
        self.assertEqual(res_c1.street.street_id, "S1")

    def test_street_alias_only_valid_inside_parent_scope(self):
        # "学,园" is an alias of S3 in D1; under D2 there is no such street.
        self.assertEqual(self.svc.catalog.resolve("C1", "东", "学,园", 1).status, "unique")
        self.assertEqual(self.svc.catalog.resolve("C1", "西", "学,园", 1).status, "unknown")

    def test_inactive_street_still_identifiable_and_still_a_candidate(self):
        self.apply_revision()
        # S2 is now inactive but must remain a candidate so P1 stays ambiguous.
        res = self.svc.catalog.resolve("C1", "同区", "东路", 2)
        self.assertEqual(res.status, "ambiguous")
        self.assertEqual({c.street_id for c in res.candidates}, {"S1", "S2"})
        # Historical stable-ID lookup still works for the inactive street.
        street = self.svc.catalog.get_street("S2")
        self.assertIsNotNone(street)
        self.assertFalse(street["active"])
        self.assertEqual(street["deactivated_version"], 2)

    def test_names_are_never_merged(self):
        # Different internal whitespace is a different alias, per spec.
        self.assertEqual(self.svc.catalog.resolve("C1", "东", "学  园", 1).status, "unique")
        self.assertEqual(self.svc.catalog.resolve("C1", "东", "学 园", 1).status, "unknown")

    def test_nfc_and_trim_only(self):
        # "Café路" alias is NFC; decomposed input must match after NFC.
        import unicodedata

        decomposed = unicodedata.normalize("NFD", "Café路")
        self.assertNotEqual(decomposed, "Café路")
        res = self.svc.catalog.resolve("C1", "  东  ", decomposed, 1)
        self.assertEqual(res.status, "unique")
        self.assertEqual(res.street.street_id, "S3")


class CoverageTests(MaterialServiceCase):
    def test_parity_any_odd_even(self):
        self.assertEqual([h.coverage_id for h in self.svc.catalog.coverage_hits("S1", 5)], ["K1", "K2"])
        self.assertEqual([h.coverage_id for h in self.svc.catalog.coverage_hits("S1", 8)], ["K1", "K3"])
        self.assertEqual([h.coverage_id for h in self.svc.catalog.coverage_hits("S2", 8)], ["K4"])
        self.assertEqual([h.coverage_id for h in self.svc.catalog.coverage_hits("S2", 7)], [])
        # Range endpoints are inclusive.
        self.assertEqual({h.coverage_id for h in self.svc.catalog.coverage_hits("S1", 20)}, {"K1"})
        self.assertEqual(self.svc.catalog.coverage_hits("S1", 21), [])

    def test_same_station_overlap_is_not_ambiguous(self):
        station, hits, stations = self.svc.catalog.station_for("S1", 5)
        self.assertEqual(station, "A")
        self.assertEqual(stations, ["A"])
        self.assertEqual(len(hits), 2)  # K1 + K2 both hit, same station

    def test_distinct_station_overlap_is_ambiguous(self):
        station, hits, stations = self.svc.catalog.station_for("S1", 9)
        self.assertIsNone(station)
        self.assertEqual(stations, ["A", "X"])

    def test_parity_endpoints_are_inclusive(self):
        svc = fresh_synthetic_service()
        self.addCleanup(svc.close)
        # S4: even 2..10 station P, odd 1..9 station Q.
        self.assertEqual(svc.catalog.station_for("S4", 9)[0], "Q")   # odd high endpoint
        self.assertEqual(svc.catalog.station_for("S4", 10)[0], "P")  # even high endpoint
        self.assertEqual(svc.catalog.station_for("S4", 8)[0], "P")   # even
        self.assertEqual(svc.catalog.station_for("S4", 1)[0], "Q")   # odd low endpoint
        # 11 is even but outside P's range and Q is odd-only -> no station.
        self.assertIsNone(svc.catalog.station_for("S4", 11)[0])

    def test_bad_house_number_rejected_by_coverage_query(self):
        with self.assertRaises(ValueError):
            self.svc.catalog.coverage_hits("S1", 0)


class CatalogIndependenceTests(MaterialServiceCase):
    def test_catalog_works_without_delivery_tables(self):
        import tempfile
        from address_delivery.catalog import Catalog

        with tempfile.TemporaryDirectory() as tmp:
            conn = __import__("address_delivery.db", fromlist=["connect"]).connect(
                f"{tmp}/cat.db"
            )
            try:
                catalog = Catalog(conn)
                catalog.load_initial(
                    __import__("json").loads((MATERIALS / "catalog.json").read_text("utf-8")),
                    __import__("json").loads((MATERIALS / "coverage.json").read_text("utf-8")),
                )
                tables = {
                    r[0]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertNotIn("jobs", tables)
                self.assertNotIn("attempts", tables)
                self.assertEqual(
                    catalog.resolve("C1", "同区", "学园路", 2).street.street_id, "S3"
                )
            finally:
                conn.close()

    def test_batch_resolution_is_independent(self):
        items = [
            AddressInput("A1", "C1", "同区", "东路", 2),
            AddressInput("A2", "C1", "同区", "学园路", 2),
            AddressInput("A3", "C1", "东", "东路", 9),
        ]
        statuses = [r.status for r in self.svc.catalog.resolve_batch(items)]
        self.assertEqual(statuses, ["ambiguous", "unique", "unique"])


class CatalogValidationTests(MaterialServiceCase):
    def test_duplicate_ids_rejected(self):
        doc = {
            "cities": [{"city_id": "C1", "name": "a"}],
            "districts": [],
            "streets": [
                {"street_id": "S1", "district_id": "D1", "name": "x", "aliases": [], "active": True}
            ],
        }
        from address_delivery.catalog import Catalog

        with self.assertRaises(CatalogError):
            Catalog(self.svc.conn).load_initial(doc, {"coverages": []})

    def test_unknown_parent_rejected(self):
        doc = {
            "cities": [{"city_id": "C1", "name": "a"}],
            "districts": [{"district_id": "D1", "city_id": "C9", "name": "x", "aliases": []}],
            "streets": [],
        }
        from address_delivery.catalog import Catalog

        with self.assertRaises(CatalogError):
            Catalog(self.svc.conn).load_initial(doc, {"coverages": []})


if __name__ == "__main__":
    unittest.main()
