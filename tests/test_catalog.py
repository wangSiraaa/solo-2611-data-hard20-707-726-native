"""Tests for the independent signed-multiset source catalog."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from multiset_join.catalog import SourceCatalog
from multiset_join.errors import (
    BatchConflictError,
    CatalogError,
    NegativeCountError,
    UnknownBatchError,
    ValidationError,
)
from multiset_join.model import merge_batch, normalize_initial_rows


class CatalogTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "src.db")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def catalog(self) -> SourceCatalog:
        return SourceCatalog(self.db)

    def imported(self, r=None, s=None) -> SourceCatalog:
        catalog = self.catalog()
        catalog.import_initial(
            r if r is not None else [{"tuple": ["east", "A", 2], "multiplicity": 2}],
            s if s is not None else [{"tuple": ["A", "v", 3], "multiplicity": 1}],
        )
        return catalog


class ImportTests(CatalogTestCase):
    def test_duplicate_complete_tuples_add_not_dedupe(self) -> None:
        rows = normalize_initial_rows(
            "R",
            [
                {"tuple": ["east", "A", 2], "multiplicity": 2},
                {"tuple": ["east", "A", 2], "multiplicity": 5},
            ],
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].multiplicity, 7)

    def test_zero_multiplicity_not_retained(self) -> None:
        rows = normalize_initial_rows(
            "S",
            [
                {"tuple": ["A", "v", 3], "multiplicity": 0},
                {"tuple": ["A", "w", 4], "multiplicity": 2},
            ],
        )
        self.assertEqual([(row.tuple, row.multiplicity) for row in rows], [(("A", "w", 4), 2)])

    def test_tuple_field_validation(self) -> None:
        bad = [
            ("R", ["east", "A", 0]),  # quantity must be positive
            ("R", ["east", "A", -1]),
            ("R", ["east", "A", True]),  # bool is not an integer count
            ("S", ["A", "v", -1]),  # price non-negative
            ("S", ["A"]),  # complete tuple only
            ("S", ["A", None, 3]),  # no NULL source fields
        ]
        for source, raw in bad:
            with self.subTest(source=source, raw=raw):
                with self.assertRaises(ValidationError):
                    normalize_initial_rows(source, [{"tuple": raw, "multiplicity": 1}])

    def test_reimport_same_content_is_noop_and_different_is_rejected(self) -> None:
        with self.imported() as catalog:
            digest = catalog.initial_digest()
            again = catalog.import_initial(
                [{"tuple": ["east", "A", 2], "multiplicity": 2}],
                [{"tuple": ["A", "v", 3], "multiplicity": 1}],
            )
            self.assertEqual(again["status"], "already_imported")
            self.assertEqual(again["initial_digest"], digest)
            with self.assertRaises(CatalogError):
                catalog.import_initial(
                    [{"tuple": ["east", "A", 2], "multiplicity": 9}],
                    [],
                )


class BatchMergeTests(CatalogTestCase):
    def test_sign_order_does_not_change_acceptance(self) -> None:
        up = [{"source": "R", "tuple": ["east", "A", 2], "delta": 1}]
        down = [{"source": "R", "tuple": ["east", "A", 2], "delta": -1}]
        self.assertEqual(merge_batch(up + down), [])
        self.assertEqual(merge_batch(down + up), [])

    def test_zero_net_delta_is_dropped(self) -> None:
        with self.imported() as catalog:
            before = catalog.current_version()
            result = catalog.apply_batch(
                "noop",
                [
                    {"source": "S", "tuple": ["A", "v", 3], "delta": 1},
                    {"source": "S", "tuple": ["A", "v", 3], "delta": -1},
                ],
            )
            self.assertEqual(result.status, "applied")
            self.assertEqual(result.changes, ())
            self.assertEqual(catalog.tuple_count("S", ["A", "v", 3]), 1)
            self.assertEqual(catalog.current_version(), before + 1)

    def test_counter_balance_is_legal_but_unbalanced_is_rejected(self) -> None:
        with self.imported() as catalog:
            # -1 then +1 nets to zero deltas and is accepted.
            out = catalog.apply_batch(
                "balance",
                [
                    {"source": "S", "tuple": ["A", "v", 3], "delta": -1},
                    {"source": "S", "tuple": ["A", "v", 3], "delta": 1},
                ],
            )
            self.assertEqual(out.changes, ())
            self.assertEqual(catalog.tuple_count("S", ["A", "v", 3]), 1)

            # A -2 on a tuple whose count is 1 still rejects the whole batch,
            # even though a legal +1 is in the same batch.
            with self.assertRaises(NegativeCountError):
                catalog.apply_batch(
                    "bad",
                    [
                        {"source": "S", "tuple": ["A", "v", 3], "delta": -2},
                        {"source": "R", "tuple": ["east", "A", 2], "delta": 1},
                    ],
                )
            self.assertEqual(catalog.tuple_count("S", ["A", "v", 3]), 1)
            self.assertEqual(catalog.tuple_count("R", ["east", "A", 2]), 2)

    def test_rejection_is_atomic(self) -> None:
        with self.imported() as catalog:
            version = catalog.current_version()
            with self.assertRaises(NegativeCountError):
                catalog.apply_batch(
                    "bad",
                    [
                        {"source": "R", "tuple": ["east", "A", 2], "delta": 5},
                        {"source": "S", "tuple": ["A", "v", 3], "delta": -2},
                    ],
                )
            self.assertEqual(catalog.current_version(), version)
            self.assertEqual(catalog.tuple_count("R", ["east", "A", 2]), 2)
            self.assertEqual(catalog.tuple_count("S", ["A", "v", 3]), 1)
            self.assertIsNone(
                catalog._batch_by_id("bad"), "rejected batch must not be recorded"
            )

    def test_batch_can_zero_a_tuple_and_it_is_not_retained(self) -> None:
        with self.imported() as catalog:
            catalog.apply_batch(
                "drop-v",
                [{"source": "S", "tuple": ["A", "v", 3], "delta": -1}],
            )
            self.assertEqual(catalog.tuple_count("S", ["A", "v", 3]), 0)
            self.assertEqual(catalog.read_source_rows("S"), [])
            self.assertEqual(
                [row.tuple for row in catalog.read_source_rows("S", 0)],
                [("A", "v", 3)],
            )
            with self.assertRaises(NegativeCountError):
                catalog.apply_batch(
                    "drop-again",
                    [{"source": "S", "tuple": ["A", "v", 3], "delta": -1}],
                )

    def test_duplicate_change_entries_merge_before_check(self) -> None:
        with self.imported() as catalog:
            # Two -1 entries merge to -2, which is negative for count 1.
            with self.assertRaises(NegativeCountError):
                catalog.apply_batch(
                    "double-delete",
                    [
                        {"source": "S", "tuple": ["A", "v", 3], "delta": -1},
                        {"source": "S", "tuple": ["A", "v", 3], "delta": -1},
                    ],
                )
            self.assertEqual(catalog.tuple_count("S", ["A", "v", 3]), 1)

    def test_batch_size_limit(self) -> None:
        with self.imported() as catalog:
            changes = [
                {"source": "R", "tuple": ["east", "A", 2], "delta": 0}
                for _ in range(501)
            ]
            with self.assertRaises(ValidationError):
                catalog.apply_batch("too-big", changes)


class ReplayTests(CatalogTestCase):
    def test_same_content_reordered_is_replay(self) -> None:
        changes = [
            {"source": "S", "tuple": ["A", "v", 3], "delta": -1},
            {"source": "S", "tuple": ["A", "w", 4], "delta": 1},
            {"source": "R", "tuple": ["east", "A", 2], "delta": 1},
        ]
        reordered = list(reversed(changes))
        with self.imported() as catalog:
            first = catalog.apply_batch("b2", changes)
            version_after = catalog.current_version()
            counts = {
                (source, tuple(row.tuple)): row.multiplicity
                for source in ("R", "S")
                for row in catalog.read_source_rows(source)
            }
            second = catalog.apply_batch("b2", reordered)
            self.assertEqual(first.status, "applied")
            self.assertEqual(second.status, "replayed")
            self.assertEqual(second.source_version, first.source_version)
            self.assertEqual(catalog.current_version(), version_after)
            counts_after = {
                (source, tuple(row.tuple)): row.multiplicity
                for source in ("R", "S")
                for row in catalog.read_source_rows(source)
            }
            self.assertEqual(counts, counts_after)
            self.assertEqual(catalog.tuple_count("R", ["east", "A", 2]), 3)

    def test_different_content_for_same_id_is_rejected(self) -> None:
        with self.imported() as catalog:
            catalog.apply_batch(
                "b2", [{"source": "S", "tuple": ["A", "w", 4], "delta": 1}]
            )
            with self.assertRaises(BatchConflictError):
                catalog.apply_batch(
                    "b2", [{"source": "S", "tuple": ["A", "w", 4], "delta": 2}]
                )
            self.assertEqual(catalog.tuple_count("S", ["A", "w", 4]), 1)

    def test_replay_lookup_does_not_reapply_and_rejects_text_mismatch(self) -> None:
        changes = [{"source": "S", "tuple": ["A", "w", 4], "delta": 1}]
        with self.imported() as catalog:
            catalog.apply_batch("b2", changes)
            catalog.apply_batch(
                "b3", [{"source": "S", "tuple": ["A", "w", 4], "delta": -1}]
            )
            record = catalog.replay_batch("b2", changes)
            self.assertEqual(record.accepted_version, 1)
            self.assertEqual(catalog.tuple_count("S", ["A", "w", 4]), 0)
            with self.assertRaises(BatchConflictError):
                catalog.replay_batch(
                    "b2", [{"source": "S", "tuple": ["A", "w", 4], "delta": 5}]
                )
            with self.assertRaises(UnknownBatchError):
                catalog.replay_batch("nope", [])


class SliceTests(CatalogTestCase):
    def test_historical_slices_are_readable_after_later_batches(self) -> None:
        with self.imported() as catalog:
            catalog.apply_batch(
                "b2",
                [
                    {"source": "S", "tuple": ["A", "v", 3], "delta": -1},
                    {"source": "S", "tuple": ["A", "w", 4], "delta": 1},
                    {"source": "R", "tuple": ["east", "A", 2], "delta": 1},
                ],
            )
            catalog.apply_batch(
                "b3", [{"source": "S", "tuple": ["A", "w", 4], "delta": -1}]
            )
            self.assertEqual(catalog.current_version(), 2)
            self.assertEqual(catalog.tuple_count("S", ["A", "v", 3], 0), 1)
            self.assertEqual(catalog.tuple_count("S", ["A", "v", 3], 1), 0)
            self.assertEqual(catalog.tuple_count("S", ["A", "w", 4], 1), 1)
            self.assertEqual(catalog.tuple_count("S", ["A", "w", 4], 2), 0)
            self.assertEqual(catalog.tuple_count("R", ["east", "A", 2], 0), 2)
            self.assertEqual(catalog.tuple_count("R", ["east", "A", 2], 1), 3)
            self.assertEqual(catalog.read_source_rows("R", 0)[0].multiplicity, 2)
            self.assertEqual(catalog.read_source_rows("R", 1)[0].multiplicity, 3)
            fingerprints = {catalog.slice_fingerprint(v) for v in (0, 1, 2)}
            self.assertEqual(len(fingerprints), 3)

    def test_unknown_versions_are_rejected(self) -> None:
        from multiset_join.errors import UnknownSliceError

        with self.imported() as catalog:
            with self.assertRaises(UnknownSliceError):
                catalog.read_slice(5)

    def test_source_catalog_works_without_derived_store(self) -> None:
        catalog = SourceCatalog(self.db)
        catalog.import_initial([], [{"tuple": ["A", "v", 3], "multiplicity": 4}])
        self.assertEqual(catalog.tuple_count("S", ["A", "v", 3]), 4)
        self.assertEqual(catalog.total_multiplicity("S"), 4)
        catalog.close()

    def test_reopen_persists_source_state(self) -> None:
        with self.imported() as catalog:
            catalog.apply_batch(
                "b2", [{"source": "S", "tuple": ["A", "w", 4], "delta": 1}]
            )
            version = catalog.current_version()
            digest = catalog.slice_fingerprint()
        with SourceCatalog(self.db) as reopened:
            self.assertEqual(reopened.current_version(), version)
            self.assertEqual(reopened.slice_fingerprint(), digest)
            self.assertEqual(reopened.tuple_count("S", ["A", "w", 4]), 1)
            self.assertEqual(reopened.get_batch("b2").change_count, 1)


if __name__ == "__main__":
    unittest.main()
