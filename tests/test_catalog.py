"""Unit tests for the identity-only nested catalog."""

import os
import tempfile
import unittest

from asset_handoff.catalog import CONTAINER, ITEM, MAX_DEPTH, AssetCatalog
from asset_handoff.errors import (
    CycleError,
    DepthExceededError,
    DuplicateAssetError,
    InvalidParentError,
    UnknownAssetError,
)


def build(catalog, assets, edges):
    catalog.add_assets(assets)
    catalog.revise_membership(
        attaches=[{"child_id": c, "parent_id": p} for c, p in edges]
    )


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.catalog = AssetCatalog(os.path.join(self.tmp.name, "c.sqlite"))

    def tearDown(self):
        self.catalog.close()
        self.tmp.cleanup()

    def test_initial_revision_is_one_and_mutations_bump_it(self):
        self.assertEqual(self.catalog.revision, 1)
        self.catalog.add_assets(
            [{"asset_id": "C1", "type": CONTAINER},
             {"asset_id": "I1", "type": ITEM}]
        )
        self.assertEqual(self.catalog.revision, 2)
        self.catalog.revise_membership(
            attaches=[{"child_id": "I1", "parent_id": "C1"}]
        )
        self.assertEqual(self.catalog.revision, 3)

    def test_duplicate_identity_rejected_in_store_and_batch(self):
        self.catalog.add_assets([{"asset_id": "X", "type": ITEM}])
        with self.assertRaises(DuplicateAssetError):
            self.catalog.add_assets([{"asset_id": "X", "type": ITEM}])
        with self.assertRaises(DuplicateAssetError):
            self.catalog.add_assets(
                [{"asset_id": "Y", "type": ITEM},
                 {"asset_id": "Y", "type": CONTAINER}]
            )

    def test_only_containers_parent(self):
        build(
            self.catalog,
            [{"asset_id": "C1", "type": CONTAINER},
             {"asset_id": "I1", "type": ITEM},
             {"asset_id": "I2", "type": ITEM}],
            [("I1", "C1")],
        )
        with self.assertRaises(InvalidParentError):
            self.catalog.revise_membership(
                moves=[{"asset_id": "I1", "new_parent": "I2"}]
            )
        with self.assertRaises(InvalidParentError):
            self.catalog.revise_membership(
                attaches=[{"child_id": "I2", "parent_id": "I1"}]
            )

    def test_unknown_assets_raise(self):
        with self.assertRaises(UnknownAssetError):
            self.catalog.expand_roots(["nope"])
        with self.assertRaises(UnknownAssetError):
            self.catalog.parent_chain("nope")

    def test_parent_chain_and_subtree(self):
        build(
            self.catalog,
            [{"asset_id": a, "type": t} for a, t in (
                ("C1", CONTAINER), ("C2", CONTAINER),
                ("I1", ITEM), ("I2", ITEM))],
            [("C2", "C1"), ("I1", "C2"), ("I2", "C1")],
        )
        self.assertEqual(self.catalog.parent_chain("I1"), ["I1", "C2", "C1"])
        self.assertEqual(self.catalog.parent_chain("C1"), ["C1"])
        self.assertEqual(self.catalog.subtree("C1"), ["C1", "C2", "I1", "I2"])

    def test_expand_dedupes_nested_scans_by_identity(self):
        # Scenario forest: C1 { C2 { I1 }, I2 }
        build(
            self.catalog,
            [{"asset_id": a, "type": t} for a, t in (
                ("C1", CONTAINER), ("C2", CONTAINER),
                ("I1", ITEM), ("I2", ITEM))],
            [("C2", "C1"), ("I1", "C2"), ("I2", "C1")],
        )
        # Scanning the outer box and the inner box must yield four identities,
        # not five, and both containers stay visible.
        self.assertEqual(
            self.catalog.expand_roots(["C1", "C2"]),
            ["C1", "C2", "I1", "I2"],
        )
        # Same set in reverse scan order keeps the same deduped identities;
        # only traversal order changes (C2's subtree is now visited first).
        self.assertEqual(
            set(self.catalog.expand_roots(["C2", "C1"])),
            {"C1", "C2", "I1", "I2"},
        )

    def test_same_named_different_assets_are_not_deduped(self):
        # Identity is asset_id only: two distinct ids are two assets even when
        # a caller might consider them "the same name".
        build(
            self.catalog,
            [{"asset_id": "BOX-1", "type": CONTAINER},
             {"asset_id": "BOX-2", "type": CONTAINER},
             {"asset_id": "TAG-1", "type": ITEM},
             {"asset_id": "TAG-2", "type": ITEM}],
            [("TAG-1", "BOX-1"), ("TAG-2", "BOX-2")],
        )
        self.assertEqual(
            self.catalog.expand_roots(["BOX-1", "BOX-2"]),
            ["BOX-1", "TAG-1", "BOX-2", "TAG-2"],
        )

    def test_cycle_rejected_even_when_intermediate_state_looks_valid(self):
        build(
            self.catalog,
            [{"asset_id": a, "type": CONTAINER} for a in ("C1", "C2", "C3")],
            [("C2", "C1"), ("C3", "C2")],
        )
        # First make C3 a sibling root forest: C1->C2 and C3.
        self.catalog.revise_membership(detaches=["C3"])
        self.assertEqual(self.catalog.parent_chain("C3"), ["C3"])

        # The next batch's final forest would be the ring
        # C3 -> C1 -> C2 -> C3 (three edges, all distinct assets).
        # Both batch orderings must reject it against the final forest.
        move = {"asset_id": "C2", "new_parent": "C3"}
        attach_c3 = {"child_id": "C3", "parent_id": "C1"}
        attach_c1 = {"child_id": "C1", "parent_id": "C2"}
        with self.assertRaises(CycleError):
            self.catalog.revise_membership(
                attaches=[attach_c3, attach_c1], moves=[move]
            )
        with self.assertRaises(CycleError):
            self.catalog.revise_membership(
                moves=[move], attaches=[attach_c1, attach_c3]
            )
        # Rejected batch changes nothing.
        self.assertEqual(self.catalog.parent_chain("C2"), ["C2", "C1"])
        self.assertIsNone(self.catalog.get_parent("C3"))

    def test_self_parent_is_a_cycle(self):
        self.catalog.add_assets([{"asset_id": "C1", "type": CONTAINER}])
        with self.assertRaises(CycleError):
            self.catalog.revise_membership(
                attaches=[{"child_id": "C1", "parent_id": "C1"}]
            )

    def test_batch_validated_against_final_forest(self):
        # C2 starts under C1; detach then re-attach elsewhere in one batch is
        # legal even though an attach-only intermediate attempt would fail.
        build(
            self.catalog,
            [{"asset_id": a, "type": CONTAINER} for a in ("C1", "C2", "C3")],
            [("C2", "C1")],
        )
        revision = self.catalog.revise_membership(
            moves=[{"asset_id": "C2", "new_parent": "C3"}]
        )
        self.assertEqual(self.catalog.get_parent("C2"), "C3")
        self.assertEqual(self.catalog.parent_chain("C2"), ["C2", "C3"])
        self.assertEqual(revision, self.catalog.revision)

    def test_detach_makes_root_and_attach_requires_root(self):
        build(
            self.catalog,
            [{"asset_id": "C1", "type": CONTAINER},
             {"asset_id": "I1", "type": ITEM}],
            [("I1", "C1")],
        )
        self.catalog.revise_membership(detaches=["I1"])
        self.assertIsNone(self.catalog.get_parent("I1"))
        # Re-detaching a root fails.
        with self.assertRaises(InvalidParentError):
            self.catalog.revise_membership(detaches=["I1"])
        self.catalog.revise_membership(
            attaches=[{"child_id": "I1", "parent_id": "C1"}]
        )
        # Attaching an already-parented child fails rather than duplicating.
        with self.assertRaises(DuplicateAssetError):
            self.catalog.revise_membership(
                attaches=[{"child_id": "I1", "parent_id": "C1"}]
            )

    def test_ambiguous_batch_touching_one_asset_twice_rejected(self):
        build(
            self.catalog,
            [{"asset_id": a, "type": CONTAINER} for a in ("C1", "C2", "C3")],
            [("C2", "C1")],
        )
        with self.assertRaises(DuplicateAssetError):
            self.catalog.revise_membership(
                detaches=["C2"],
                attaches=[{"child_id": "C2", "parent_id": "C3"}],
            )

    def test_depth_limit_enforced_on_final_forest(self):
        # Build a chain at exactly the limit (depth MAX_DEPTH is legal).
        chain = [f"C{i}" for i in range(MAX_DEPTH + 1)]
        self.catalog.add_assets(
            [{"asset_id": c, "type": CONTAINER} for c in chain]
        )
        self.catalog.revise_membership(
            attaches=[
                {"child_id": child, "parent_id": parent}
                for parent, child in zip(chain, chain[1:])
            ]
        )
        self.assertEqual(len(self.catalog.parent_chain(chain[-1])) - 1, MAX_DEPTH)
        # One more level exceeds it.
        self.catalog.add_assets([{"asset_id": "DEEP", "type": ITEM}])
        with self.assertRaises(DepthExceededError):
            self.catalog.revise_membership(
                attaches=[{"child_id": "DEEP", "parent_id": chain[-1]}]
            )

    def test_depth_check_holds_with_memoized_prefixes(self):
        # Two branches off one root, inserted shallow-first so validation
        # memoizes prefixes before the deep leaf walk hits them.
        containers = [f"D{i}" for i in range(MAX_DEPTH)]  # D0..D7
        self.catalog.add_assets(
            [{"asset_id": c, "type": CONTAINER} for c in containers]
            + [{"asset_id": "LEAF", "type": ITEM}]
        )
        # Shallow edge first; the deep chain fills memoized prefixes.
        self.catalog.revise_membership(
            attaches=[{"child_id": "D1", "parent_id": "D0"}]
        )
        self.catalog.revise_membership(
            attaches=[
                {"child_id": child, "parent_id": parent}
                for parent, child in zip(containers[1:-1], containers[2:])
            ]
        )
        # The leaf at exactly depth 8 is accepted via a memoized prefix.
        self.catalog.revise_membership(
            attaches=[{"child_id": "LEAF", "parent_id": containers[-1]}]
        )
        self.assertEqual(
            len(self.catalog.parent_chain("LEAF")) - 1, MAX_DEPTH
        )

    def test_depth_exceeded_through_memoized_prefix(self):
        # A depth-8 chain exists first (so its prefixes are memoized);
        # attaching one more node below the deepest one must be rejected via
        # the memo branch, not silently accepted because the prefix was cached.
        chain = [f"D{i}" for i in range(MAX_DEPTH + 1)]  # D0..D8, depth 8
        self.catalog.add_assets(
            [{"asset_id": c, "type": CONTAINER} for c in chain]
            + [{"asset_id": "X", "type": ITEM}]
        )
        self.catalog.revise_membership(
            attaches=[
                {"child_id": child, "parent_id": parent}
                for parent, child in zip(chain[:-1], chain[1:])
            ]
        )
        with self.assertRaises(DepthExceededError):
            self.catalog.revise_membership(
                attaches=[{"child_id": "X", "parent_id": chain[-1]}]
            )
        # The rejected batch leaves the forest at its legal depth-8 shape.
        self.assertIsNone(self.catalog.get_parent("X"))

    def test_state_persists_across_reopen_with_no_custody_columns(self):
        build(
            self.catalog,
            [{"asset_id": "C1", "type": CONTAINER},
             {"asset_id": "I1", "type": ITEM}],
            [("I1", "C1")],
        )
        revision = self.catalog.revision
        path = self.catalog.path
        self.catalog.close()

        reopened = AssetCatalog(path)
        self.assertEqual(reopened.revision, revision)
        self.assertEqual(reopened.subtree("C1"), ["C1", "I1"])
        columns = {
            row[1]
            for row in reopened.conn.execute("PRAGMA table_info(assets)")
        }
        self.assertNotIn("custodian", columns)
        reopened.close()


if __name__ == "__main__":
    unittest.main()
