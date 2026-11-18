"""End-to-end tests: scenario replay, read-only client, and scale bounds."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from asset_handoff.catalog import CONTAINER, ITEM, AssetCatalog
from asset_handoff.replay import run_operations
from asset_handoff.warehouse import HandoffWarehouse

ROOT = Path(__file__).resolve().parents[1]
MATERIALS = ROOT / "materials" / "755"
READER = MATERIALS / "handoff_reader.py"


class ScenarioReplayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_materials_scenario(self):
        report = run_operations(
            MATERIALS / "assets.json",
            MATERIALS / "operations.jsonl",
            self.out / "catalog.sqlite",
            self.out / "warehouse.sqlite",
            self.out / "artifacts",
            reader_path=READER,
        )

        # Initial catalog revision and the single explicit move land on rev 2.
        self.assertEqual(report["initial_revision"], 1)
        handoffs = {
            op["request_id"]: op
            for op in report["operations"]
            if op["operation"] == "prepare_and_commit_handoff"
        }
        # Old record still has four frozen identities (including moved I1)...
        self.assertEqual(handoffs["H1"]["member_count"], 4)
        self.assertEqual(
            handoffs["H1"]["members"], ["C1", "C2", "I1", "I2"]
        )
        # ...the new record only three.
        self.assertEqual(handoffs["H2"]["member_count"], 3)
        self.assertEqual(handoffs["H2"]["members"], ["C1", "C2", "I2"])
        self.assertEqual(handoffs["H2"]["membership_revision"], 2)

        # The physical move must not have moved custody with it.
        move_op = next(
            op for op in report["operations"]
            if op["operation"] == "revise_membership"
        )
        self.assertEqual(move_op["custody_after_move"]["I1"], "B")

        read_op = next(
            op for op in report["operations"]
            if op["operation"] == "read_handoff"
        )
        self.assertEqual(read_op["frozen_member_count"], 4)
        self.assertIn("I1", read_op["frozen_members"])

        custodians = next(
            op for op in report["operations"]
            if op["operation"] == "read_current_custodians"
        )["custodians"]
        self.assertEqual(
            custodians,
            {"C1": "C", "C2": "C", "C3": "B", "I1": "B", "I2": "C"},
        )

    def test_readonly_client_reads_actual_artifacts(self):
        run_operations(
            MATERIALS / "assets.json",
            MATERIALS / "operations.jsonl",
            self.out / "catalog.sqlite",
            self.out / "warehouse.sqlite",
            self.out / "artifacts",
            reader_path=READER,
        )
        for handoff_id, expected_count in (("H1", 4), ("H2", 3)):
            proc = subprocess.run(
                [sys.executable, str(READER),
                 str(self.out / "artifacts" / f"{handoff_id}.json")],
                check=True,
                capture_output=True,
                text=True,
            )
            lines = proc.stdout.splitlines()
            self.assertIn(handoff_id, lines[0])
            self.assertIn(f"COUNT {expected_count}", lines)
            if handoff_id == "H1":
                # The independent reader still sees I1 in the old handoff.
                self.assertIn("I1 ITEM", lines)

    def test_artifact_contains_frozen_revision_and_members(self):
        run_operations(
            MATERIALS / "assets.json",
            MATERIALS / "operations.jsonl",
            self.out / "catalog.sqlite",
            self.out / "warehouse.sqlite",
            self.out / "artifacts",
        )
        h1 = json.loads(
            (self.out / "artifacts" / "H1.json").read_text(encoding="utf-8")
        )
        h2 = json.loads(
            (self.out / "artifacts" / "H2.json").read_text(encoding="utf-8")
        )
        self.assertEqual(h1["format"], "asset-handoff/1")
        self.assertEqual(h1["membership_revision"], 1)
        self.assertEqual(h2["membership_revision"], 2)
        self.assertEqual(
            [m["asset_id"] for m in h2["members"]], ["C1", "C2", "I2"]
        )

    def test_stores_are_independent_sqlite_databases(self):
        run_operations(
            MATERIALS / "assets.json",
            MATERIALS / "operations.jsonl",
            self.out / "catalog.sqlite",
            self.out / "warehouse.sqlite",
            self.out / "artifacts",
        )
        import sqlite3

        catalog_tables = {
            row[0]
            for row in sqlite3.connect(self.out / "catalog.sqlite")
            .execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        warehouse_tables = {
            row[0]
            for row in sqlite3.connect(self.out / "warehouse.sqlite")
            .execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        self.assertEqual(catalog_tables, {"assets", "edges", "meta"})
        self.assertEqual(
            warehouse_tables,
            {"custody", "handoffs", "handoff_members", "meta"},
        )


class ScaleTest(unittest.TestCase):
    """Bounds from the brief: 200 assets, 50 containers, depth 8."""

    def test_two_hundred_asset_forest(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        catalog = AssetCatalog(os.path.join(tmp.name, "big-c.sqlite"))
        warehouse = HandoffWarehouse(
            os.path.join(tmp.name, "big-w.sqlite"), catalog
        )

        containers = [f"C{i:02d}" for i in range(50)]
        items = [f"I{i:03d}" for i in range(150)]
        catalog.add_assets(
            [{"asset_id": c, "type": CONTAINER} for c in containers]
            + [{"asset_id": i, "type": ITEM} for i in items]
        )

        # Chain the 50 containers in chains of 8 (container depth 0..7), so
        # leaf items land exactly at depth 8, never beyond it.
        attaches = []
        for idx, container in enumerate(containers):
            if idx % 8 != 0:  # every 8th container starts a new chain root
                attaches.append(
                    {"child_id": container,
                     "parent_id": containers[idx - 1]}
                )
        for idx, item in enumerate(items):
            attaches.append(
                {"child_id": item, "parent_id": containers[idx % 50]}
            )
        catalog.revise_membership(attaches=attaches)

        self.assertEqual(len(catalog.list_assets()), 200)
        deepest = max(
            len(catalog.parent_chain(asset["asset_id"])) - 1
            for asset in catalog.list_assets()
        )
        self.assertLessEqual(deepest, 8)

        warehouse.set_custodians(
            {asset["asset_id"]: "A" for asset in catalog.list_assets()}
        )

        # Expanding all chain roots must return every identity exactly once.
        chain_roots = [c for idx, c in enumerate(containers) if idx % 8 == 0]
        expanded = catalog.expand_roots(chain_roots)
        self.assertEqual(len(expanded), 200)
        self.assertEqual(len(set(expanded)), 200)

        # Whole-forest handoff in one atomic commit.
        record = warehouse.prepare_and_commit("BIG", chain_roots, "A", "B")
        self.assertEqual(len(record["members"]), 200)
        self.assertTrue(
            all(v == "B" for v in warehouse.current_custodians().values())
        )

        # A later reshuffle freezes nothing retroactively.
        catalog.revise_membership(
            moves=[{"asset_id": "I000", "new_parent": containers[-1]}]
        )
        self.assertEqual(len(warehouse.get_handoff("BIG")["members"]), 200)
        catalog.close()
        warehouse.close()


if __name__ == "__main__":
    unittest.main()
