"""Unit tests for the custody handoff warehouse."""

import json
import os
import tempfile
import unittest

from asset_handoff.catalog import CONTAINER, ITEM, AssetCatalog
from asset_handoff.errors import (
    CustodianMismatchError,
    DuplicateHandoffError,
    HandoffNotFoundError,
    PreparedStaleError,
)
from asset_handoff.warehouse import (
    ASSET_HANDOFF_FORMAT,
    STATUS_COMMITTED,
    STATUS_PREPARED,
    HandoffWarehouse,
)

FOREST = [
    ("C1", CONTAINER, None),
    ("C2", CONTAINER, "C1"),
    ("C3", CONTAINER, None),
    ("I1", ITEM, "C2"),
    ("I2", ITEM, "C1"),
]


def make_stores(tmpdir):
    catalog = AssetCatalog(os.path.join(tmpdir, "catalog.sqlite"))
    catalog.add_assets(
        [{"asset_id": a, "type": t} for a, t, _ in FOREST]
    )
    catalog.revise_membership(
        attaches=[
            {"child_id": a, "parent_id": p}
            for a, _, p in FOREST if p is not None
        ]
    )
    warehouse = HandoffWarehouse(
        os.path.join(tmpdir, "warehouse.sqlite"), catalog
    )
    return catalog, warehouse


class WarehouseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.catalog, self.warehouse = make_stores(self.tmp.name)
        # A holds C1/C2/I1/I2; B already holds the sibling box C3.
        self.warehouse.set_custodians(
            {"C1": "A", "C2": "A", "I1": "A", "I2": "A", "C3": "B"}
        )

    def tearDown(self):
        self.warehouse.close()
        self.catalog.close()
        self.tmp.cleanup()

    def test_prepare_freezes_expanded_members_and_their_revision(self):
        record = self.warehouse.prepare("H1", ["C1", "C2"], "A", "B")
        self.assertEqual(record["status"], STATUS_PREPARED)
        self.assertEqual(
            [m["asset_id"] for m in record["members"]],
            ["C1", "C2", "I1", "I2"],
        )
        # Types are snapshotted into the frozen rows.
        self.assertEqual(
            {m["asset_id"]: m["type"] for m in record["members"]},
            {"C1": CONTAINER, "C2": CONTAINER, "I1": ITEM, "I2": ITEM},
        )
        self.assertEqual(record["catalog_revision"], self.catalog.revision)
        # Preparing changes no current custodian.
        self.assertEqual(self.warehouse.get_custodian("I1"), "A")

    def test_commit_atomically_transfers_every_member_not_just_root(self):
        self.warehouse.prepare("H1", ["C1", "C2"], "A", "B")
        record = self.warehouse.commit("H1")
        self.assertEqual(record["status"], STATUS_COMMITTED)
        self.assertEqual(
            self.warehouse.current_custodians(),
            {"C1": "B", "C2": "B", "C3": "B", "I1": "B", "I2": "B"},
        )

    def test_outer_box_ownership_does_not_imply_contents(self):
        # I1 is held by X while every ancestor belongs to A: the request must
        # be rejected wholesale and leave every custodian untouched.
        self.warehouse.set_custodian("I1", "X")
        before = self.warehouse.current_custodians()
        with self.assertRaises(CustodianMismatchError) as ctx:
            self.warehouse.prepare("H1", ["C1"], "A", "B")
        self.assertEqual(ctx.exception.mismatches, [("I1", "X")])
        self.assertEqual(self.warehouse.current_custodians(), before)
        # Nothing was prepared.
        with self.assertRaises(HandoffNotFoundError):
            self.warehouse.get_handoff("H1")

    def test_missing_custodian_record_is_a_mismatch_not_inheritance(self):
        self.warehouse.conn.execute("DELETE FROM custody WHERE asset_id = 'I2'")
        with self.assertRaises(CustodianMismatchError) as ctx:
            self.warehouse.prepare("H1", ["C1"], "A", "B")
        self.assertEqual(ctx.exception.mismatches, [("I2", None)])

    def test_duplicate_handoff_id_rejected(self):
        self.warehouse.prepare("H1", ["C3"], "B", "A")
        with self.assertRaises(DuplicateHandoffError):
            self.warehouse.prepare("H1", ["C3"], "B", "A")
        self.warehouse.commit("H1")
        with self.assertRaises(DuplicateHandoffError):
            self.warehouse.prepare_and_commit("H1", ["C3"], "A", "B")

    def test_catalog_change_after_prepare_invalidates_commit(self):
        self.warehouse.prepare("H1", ["C1", "C2"], "A", "B")
        # A purely physical move inside the scanned set still changes the
        # catalog revision and therefore invalidates the preparation.
        self.catalog.revise_membership(
            moves=[{"asset_id": "I2", "new_parent": "C2"}]
        )
        with self.assertRaises(PreparedStaleError):
            self.warehouse.commit("H1")
        # Custody was never touched by the failed commit.
        self.assertEqual(self.warehouse.get_custodian("C1"), "A")

    def test_custody_change_after_prepare_invalidates_commit(self):
        self.warehouse.prepare("H1", ["C1"], "A", "B")
        self.warehouse.set_custodian("I1", "X")
        with self.assertRaises(PreparedStaleError):
            self.warehouse.commit("H1")

    def test_unrelated_catalog_edit_also_invalidates_prepare(self):
        self.warehouse.prepare("H1", ["C1"], "A", "B")
        self.catalog.add_assets([{"asset_id": "NEW", "type": ITEM}])
        with self.assertRaises(PreparedStaleError):
            self.warehouse.commit("H1")

    def test_reprepare_then_commit_works_against_current_state(self):
        self.warehouse.prepare("H1", ["C1", "C2"], "A", "B")
        self.catalog.revise_membership(
            moves=[{"asset_id": "I1", "new_parent": "C3"}]
        )
        with self.assertRaises(PreparedStaleError):
            self.warehouse.commit("H1")
        # A new request id consumes the current catalog; membership now
        # excludes I1, which physically left the C1 subtree.
        record = self.warehouse.prepare_and_commit("H1b", ["C1"], "A", "B")
        self.assertEqual(
            [m["asset_id"] for m in record["members"]], ["C1", "C2", "I2"]
        )
        # I1 stayed with A: custody does not follow the physical move.
        self.assertEqual(self.warehouse.get_custodian("I1"), "A")

    def test_frozen_history_survives_later_forest_changes(self):
        first = self.warehouse.prepare_and_commit(
            "H1", ["C1", "C2"], "A", "B"
        )
        revision_at_h1 = first["catalog_revision"]
        self.catalog.revise_membership(
            moves=[{"asset_id": "I1", "new_parent": "C3"}]
        )
        history = self.warehouse.get_handoff("H1")
        self.assertEqual(history["status"], STATUS_COMMITTED)
        self.assertEqual(
            [m["asset_id"] for m in history["members"]],
            ["C1", "C2", "I1", "I2"],
        )
        self.assertEqual(history["catalog_revision"], revision_at_h1)
        # The current C1 subtree has shrunk, but history did not recalculate.
        self.assertEqual(self.catalog.subtree("C1"), ["C1", "C2", "I2"])

    def test_artifact_export_matches_asset_handoff_format(self):
        self.warehouse.prepare_and_commit("H1", ["C1", "C2"], "A", "B")
        path = os.path.join(self.tmp.name, "H1.json")
        payload = self.warehouse.export_artifact("H1", path)
        on_disk = json.loads(open(path, encoding="utf-8").read())
        self.assertEqual(on_disk, payload)
        self.assertEqual(on_disk["format"], ASSET_HANDOFF_FORMAT)
        self.assertEqual(on_disk["handoff_id"], "H1")
        self.assertEqual(on_disk["from"], "A")
        self.assertEqual(on_disk["to"], "B")
        self.assertEqual(
            [m["asset_id"] for m in on_disk["members"]],
            ["C1", "C2", "I1", "I2"],
        )
        with self.assertRaises(HandoffNotFoundError):
            self.warehouse.export_artifact(
                "never", os.path.join(self.tmp.name, "x.json")
            )

    def test_prepared_handoff_cannot_be_exported(self):
        self.warehouse.prepare("P1", ["C3"], "B", "A")
        with self.assertRaises(PreparedStaleError):
            self.warehouse.export_artifact(
                "P1", os.path.join(self.tmp.name, "P1.json")
            )

    def test_physical_move_never_changes_custody(self):
        self.warehouse.prepare_and_commit("H1", ["C1", "C2"], "A", "B")
        self.catalog.revise_membership(
            moves=[{"asset_id": "I1", "new_parent": "C3"}]
        )
        self.assertEqual(self.warehouse.get_custodian("I1"), "B")
        self.assertEqual(self.warehouse.get_custodian("C3"), "B")

    def test_second_handoff_consumes_current_catalog_only(self):
        self.warehouse.prepare_and_commit("H1", ["C1", "C2"], "A", "B")
        self.catalog.revise_membership(
            moves=[{"asset_id": "I1", "new_parent": "C3"}]
        )
        # H2: current C1 subtree (3 identities) from B to C; I1 remains B.
        record = self.warehouse.prepare_and_commit("H2", ["C1"], "B", "C")
        self.assertEqual(len(record["members"]), 3)
        self.assertEqual(
            self.warehouse.current_custodians(),
            {"C1": "C", "C2": "C", "C3": "B", "I1": "B", "I2": "C"},
        )
        h1 = self.warehouse.get_handoff("H1")
        self.assertEqual(len(h1["members"]), 4)
        self.assertIn("I1", [m["asset_id"] for m in h1["members"]])

    def test_persistence_across_reopen_keeps_history_and_responsibility(self):
        self.warehouse.prepare_and_commit("H1", ["C1", "C2"], "A", "B")
        catalog_path = self.catalog.path
        warehouse_path = self.warehouse.path
        self.warehouse.close()
        self.catalog.close()

        catalog2 = AssetCatalog(catalog_path)
        warehouse2 = HandoffWarehouse(warehouse_path, catalog2)
        history = warehouse2.get_handoff("H1")
        self.assertEqual(len(history["members"]), 4)
        self.assertEqual(warehouse2.get_custodian("I2"), "B")
        warehouse2.close()
        catalog2.close()


if __name__ == "__main__":
    unittest.main()
