"""Tests for the replayable LEFT JOIN derived store."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from multiset_join.catalog import SourceCatalog
from multiset_join.derive import DerivedStore, derive_slice
from multiset_join.errors import ResultConflictError, UnknownResultError
from multiset_join.model import SourceRow

MATERIALS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "materials",
    "743",
    "input.json",
)


def rows(source: str, values) -> list[SourceRow]:
    return [SourceRow(source, tuple(raw), mult) for raw, mult in values]


class PureJoinTests(unittest.TestCase):
    def test_sample_initial(self) -> None:
        r = rows("R", [(["east", "A", 2], 2), (["east", "Q", 1], 1)])
        s = rows("S", [(["A", "v", 3], 1)])
        result = derive_slice(r, s)
        totals = result["totals"]
        self.assertEqual(totals["derived_row_count"], 3)
        self.assertEqual(totals["quoted_amount"], 12)
        self.assertEqual(totals["unquoted_row_count"], 1)
        outputs = {
            (key.region, key.sku, key.quantity, key.supplier, key.price): count
            for key, count in result["outputs"].items()
        }
        self.assertEqual(
            outputs,
            {
                ("east", "A", 2, "v", 3): 2,
                ("east", "Q", 1, None, None): 1,
            },
        )

    def test_multipliers_multiply_not_dedupe(self) -> None:
        r = rows("R", [(["east", "A", 2], 3)])
        s = rows("S", [(["A", "v", 3], 2), (["A", "w", 5], 4)])
        result = derive_slice(r, s)
        outputs = {
            (key.supplier, key.price): count for key, count in result["outputs"].items()
        }
        self.assertEqual(outputs, {("v", 3): 6, ("w", 5): 12})
        self.assertEqual(result["totals"]["derived_row_count"], 18)
        self.assertEqual(
            result["totals"]["quoted_amount"], 3 * 2 * (2 * 3) + 3 * 2 * (4 * 5)
        )
        self.assertEqual(result["totals"]["unquoted_row_count"], 0)

    def test_same_business_output_from_different_source_rows_is_added(self) -> None:
        # Equal output keys must add their contributions.  Derive is fed raw
        # slices here; repeated S rows stand in for two different source
        # origins that happen to carry the same business fields.
        r = rows("R", [(["east", "A", 2], 2)])
        s = rows("S", [(["A", "v", 3], 1), (["A", "v", 3], 4)])
        result = derive_slice(r, s)
        (key, count), = result["outputs"].items()
        self.assertEqual((key.supplier, key.price), ("v", 3))
        self.assertEqual(count, 2 * 1 + 2 * 4)
        self.assertEqual(len(result["provenance"]), 2)
        self.assertEqual(
            [prov.contribution for prov in result["provenance"]], [2, 8]
        )
        self.assertEqual(result["totals"]["quoted_amount"], (2 + 8) * 2 * 3)

    def test_unmatched_left_multiple_tuples_sum(self) -> None:
        r = rows("R", [(["east", "A", 2], 2), (["east", "A", 3], 5)])
        result = derive_slice(r, [])
        outputs = {
            (key.quantity, key.is_quoted): count for key, count in result["outputs"].items()
        }
        self.assertEqual(outputs, {(2, False): 2, (3, False): 5})
        self.assertEqual(result["totals"]["unquoted_row_count"], 7)

    def test_null_rows_never_created_when_matches_exist(self) -> None:
        r = rows("R", [(["east", "A", 2], 2)])
        s = rows("S", [(["A", "v", 3], 1)])
        result = derive_slice(r, s)
        self.assertTrue(all(key.is_quoted for key in result["outputs"]))


class DerivedStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, "src.db")
        self.drv = os.path.join(self.tmp.name, "derived.db")
        with SourceCatalog(self.src) as catalog:
            catalog.import_initial(
                [
                    {"tuple": ["east", "A", 2], "multiplicity": 2},
                    {"tuple": ["east", "Q", 1], "multiplicity": 1},
                ],
                [{"tuple": ["A", "v", 3], "multiplicity": 1}],
            )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _import(self, r, s) -> None:
        with SourceCatalog(self.src) as catalog:
            catalog.import_initial(r, s)

    def test_sample_lifecycle_from_materials(self) -> None:
        with open(MATERIALS, encoding="utf-8") as handle:
            document = json.load(handle)
        with SourceCatalog(self.src) as catalog:
            catalog.import_initial(
                document["initial"]["R"], document["initial"]["S"]
            )
            catalog.apply_batch("b2", document["batches"][0]["changes"])
            catalog.apply_batch("b3", document["batches"][1]["changes"])

        with DerivedStore(self.src, self.drv) as store:
            # Histories remain buildable from their pinned versions.
            initial = store.build_result("initial", source_version=0)
            after_b2 = store.build_result("after-b2", source_version=1)
            after_b3 = store.build_result("after-b3")
        self.assertEqual(initial["totals"]["derived_row_count"], 3)
        self.assertEqual(initial["totals"]["quoted_amount"], 12)
        self.assertEqual(initial["totals"]["unquoted_row_count"], 1)

        self.assertEqual(after_b2["totals"]["derived_row_count"], 4)
        self.assertEqual(after_b2["totals"]["quoted_amount"], 24)
        self.assertEqual(after_b2["totals"]["unquoted_row_count"], 1)
        with DerivedStore(self.src, self.drv) as store:
            b2_rows = store.read_result_rows("after-b2")
        self.assertEqual(
            [(r["supplier"], r["price"], r["multiplicity"]) for r in b2_rows],
            [("w", 4, 3), (None, None, 1)],
        )

        self.assertEqual(after_b3["totals"]["derived_row_count"], 4)
        self.assertEqual(after_b3["totals"]["quoted_amount"], 0)
        self.assertEqual(after_b3["totals"]["unquoted_row_count"], 4)
        with DerivedStore(self.src, self.drv) as store:
            b3_rows = store.read_result_rows("after-b3")
        self.assertEqual(
            [(r["sku"], r["supplier"], r["price"], r["multiplicity"]) for r in b3_rows],
            [("A", None, None, 3), ("Q", None, None, 1)],
        )
        # No stale quote survives into b3.
        self.assertFalse(any(r["quoted"] for r in b3_rows))

    def test_old_result_is_immutable_after_new_batches(self) -> None:
        with DerivedStore(self.src, self.drv) as store:
            store.build_result("initial", source_version=0)
        with SourceCatalog(self.src) as catalog:
            catalog.apply_batch(
                "b2",
                [
                    {"source": "S", "tuple": ["A", "v", 3], "delta": -1},
                    {"source": "S", "tuple": ["A", "w", 4], "delta": 1},
                ],
            )
        with DerivedStore(self.src, self.drv) as store:
            again = store.build_result("initial")  # no version -> returns existing
            rows_ = store.read_result_rows("initial")
            exploded = store.explain("initial", sku="A")
        self.assertEqual(again["source_version"], 0)
        self.assertEqual(again["totals"]["quoted_amount"], 12)
        self.assertEqual(rows_[0]["supplier"], "v")
        # Provenance still points at the historical v quote, not the current w.
        self.assertEqual(exploded["contributions"][0]["right_tuple"], ["A", "v", 3])

    def test_binding_conflict_when_reusing_result_id_for_other_version(self) -> None:
        with DerivedStore(self.src, self.drv) as store:
            store.build_result("r", source_version=0)
            with self.assertRaises(ResultConflictError):
                store.build_result("r", source_version=1)

    def test_left_null_row_exits_when_match_appears(self) -> None:
        with DerivedStore(self.src, self.drv) as store:
            store.build_result("before")
        with SourceCatalog(self.src) as catalog:
            catalog.apply_batch(
                "add-q",
                [{"source": "S", "tuple": ["Q", "z", 7], "delta": 4}],
            )
        with DerivedStore(self.src, self.drv) as store:
            store.build_result("after")
            before_rows = store.read_result_rows("before", sku="Q")
            after_rows = store.read_result_rows("after", sku="Q")
        self.assertEqual(
            [(r["supplier"], r["price"], r["multiplicity"]) for r in before_rows],
            [(None, None, 1)],
        )
        self.assertEqual(
            [(r["supplier"], r["price"], r["multiplicity"]) for r in after_rows],
            [("z", 7, 4)],
        )

    def test_match_disappears_old_quote_leaves(self) -> None:
        with SourceCatalog(self.src) as catalog:
            catalog.apply_batch(
                "drop-v", [{"source": "S", "tuple": ["A", "v", 3], "delta": -1}]
            )
        with DerivedStore(self.src, self.drv) as store:
            store.build_result("after")
            rows_ = store.read_result_rows("after", sku="A")
        self.assertEqual(
            [(r["supplier"], r["price"], r["multiplicity"]) for r in rows_],
            [(None, None, 2)],
        )

    def test_both_sides_in_one_batch_uses_same_slice(self) -> None:
        with SourceCatalog(self.src) as catalog:
            catalog.apply_batch(
                "both",
                [
                    {"source": "R", "tuple": ["east", "A", 2], "delta": 2},
                    {"source": "S", "tuple": ["A", "v", 3], "delta": 3},
                ],
            )
            version = catalog.current_version()
        with DerivedStore(self.src, self.drv) as store:
            result = store.build_result("both")
            rows_ = store.read_result_rows("both", sku="A")
        self.assertEqual(result["source_version"], version)
        self.assertEqual(rows_[0]["multiplicity"], 4 * 4)
        self.assertEqual(result["totals"]["quoted_amount"], 4 * 4 * 2 * 3)

    def test_zeroed_source_tuple_contributes_nothing(self) -> None:
        with SourceCatalog(self.src) as catalog:
            catalog.apply_batch(
                "drop-r",
                [{"source": "R", "tuple": ["east", "Q", 1], "delta": -1}],
            )
        with DerivedStore(self.src, self.drv) as store:
            result = store.build_result("dropped")
            rows_ = store.read_result_rows("dropped", sku="Q")
        self.assertEqual(rows_, [])
        self.assertEqual(result["totals"]["unquoted_row_count"], 0)

    def test_region_summaries(self) -> None:
        with SourceCatalog(self.src) as catalog:
            catalog.apply_batch(
                "more",
                [
                    {"source": "R", "tuple": ["west", "A", 2], "delta": 1},
                    {"source": "R", "tuple": ["west", "Z", 9], "delta": 2},
                ],
            )
        with DerivedStore(self.src, self.drv) as store:
            store.build_result("r")
            summary = {row["region"]: row for row in store.region_summary("r")}
        self.assertEqual(summary["east"]["derived_row_count"], 3)
        self.assertEqual(summary["east"]["quoted_amount"], 12)
        self.assertEqual(summary["east"]["unquoted_row_count"], 1)
        self.assertEqual(summary["west"]["derived_row_count"], 3)
        self.assertEqual(summary["west"]["quoted_amount"], 6)  # 1 x 2 x 3
        self.assertEqual(summary["west"]["unquoted_row_count"], 2)

    def test_explain_reports_multipliers_and_formula(self) -> None:
        with DerivedStore(self.src, self.drv) as store:
            store.build_result("r")
            explanation = store.explain("r", sku="A")
        self.assertEqual(explanation["contribution_count"], 1)
        contribution = explanation["contributions"][0]
        self.assertEqual(contribution["left_tuple"], ["east", "A", 2])
        self.assertEqual(contribution["left_multiplicity"], 2)
        self.assertEqual(contribution["right_tuple"], ["A", "v", 3])
        self.assertEqual(contribution["right_multiplicity"], 1)
        self.assertEqual(contribution["contribution"], 2)
        self.assertEqual(contribution["formula"], "2 x 1 = 2")

        with DerivedStore(self.src, self.drv) as store:
            unquoted = store.explain("r", sku="Q")
        self.assertIn("NULL", unquoted["contributions"][0]["formula"])

    def test_unknown_result(self) -> None:
        with DerivedStore(self.src, self.drv) as store:
            with self.assertRaises(UnknownResultError):
                store.get_result("missing")

    def test_derived_store_does_not_own_a_source_copy(self) -> None:
        with DerivedStore(self.src, self.drv) as store:
            store.build_result("r")
            tables = {
                row[0]
                for row in store._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertNotIn("source_history", tables)
        self.assertNotIn("batches", tables)
        # The source catalog alone still works with the derived DB untouched.
        with SourceCatalog(self.src) as catalog:
            catalog.apply_batch(
                "later", [{"source": "S", "tuple": ["Q", "z", 7], "delta": 1}]
            )
            self.assertEqual(catalog.tuple_count("S", ["Q", "z", 7]), 1)

    def test_reopen_derived_store_keeps_results(self) -> None:
        with DerivedStore(self.src, self.drv) as store:
            built = store.build_result("r", source_version=0)
        with DerivedStore(self.src, self.drv) as store:
            reopened = store.get_result("r")
        self.assertEqual(reopened["totals"], built["totals"])
        self.assertEqual(reopened["source_version"], 0)

    def test_full_rebuild_matches_pure_definition_after_many_batches(self) -> None:
        batches = [
            (
                "x1",
                [
                    {"source": "R", "tuple": ["east", "A", 2], "delta": 4},
                    {"source": "S", "tuple": ["A", "v", 3], "delta": -1},
                    {"source": "S", "tuple": ["A", "w", 4], "delta": 2},
                ],
            ),
            (
                "x2",
                [
                    {"source": "R", "tuple": ["north", "B", 5], "delta": 3},
                    {"source": "S", "tuple": ["B", "u", 8], "delta": 2},
                    {"source": "R", "tuple": ["east", "Q", 1], "delta": -1},
                ],
            ),
        ]
        with SourceCatalog(self.src) as catalog:
            for batch_id, changes in batches:
                catalog.apply_batch(batch_id, changes)
            version = catalog.current_version()
            r = catalog.read_source_rows("R", version)
            s = catalog.read_source_rows("S", version)
        expected = derive_slice(r, s)["totals"]
        with DerivedStore(self.src, self.drv) as store:
            actual = store.build_result("x", source_version=version)["totals"]
        for key in (
            "derived_row_count",
            "quoted_row_count",
            "unquoted_row_count",
            "quoted_amount",
            "output_tuple_count",
        ):
            self.assertEqual(actual[key], expected[key])


if __name__ == "__main__":
    unittest.main()
