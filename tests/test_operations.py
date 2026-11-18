"""End-to-end: replay the material operations, then inspect with the reader."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from coupon_service import db, ledger
from coupon_service.operations import run_operations
from coupon_service.snapshot import legacy_snapshot
from tests.support import MATERIAL_DB, WORKSPACE


class OperationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name) / "history.sqlite"
        shutil.copyfile(MATERIAL_DB, self.work)
        self.operations = WORKSPACE / "materials" / "709" / "operations.jsonl"

    def test_operations_advance_history_as_documented(self):
        outcomes = run_operations(self.work, self.operations)
        self.assertEqual(len(outcomes), 5)

        # 1) template revision → T_FOOD v2 current
        self.assertEqual(outcomes[0].response["version"], 2)

        # 2) O_NEW fails qualification; frozen, no deduction
        new = outcomes[1].response["result"]
        self.assertEqual(new["status"], "REJECTED")
        self.assertEqual(new["code"], "QUALIFICATION")
        self.assertFalse(new["deducted"])

        # 3) cancel O_OLD returns two uses
        cancel = outcomes[2].response["result"]
        self.assertEqual(cancel["status"], "cancelled")

        # 4) replay of R_CANCEL is the frozen result
        self.assertTrue(outcomes[3].response["replayed"])
        self.assertEqual(outcomes[3].response["result"], cancel)

        # 5) I_FOOD + I_ALT same FOOD group → combination rejection
        mutual = outcomes[4].response["result"]
        self.assertEqual(mutual["code"], "COMBINATION")

        con = db.connect(self.work)
        try:
            balances = {
                row["instance_id"]: row["remaining"]
                for row in con.execute(
                    "SELECT instance_id, remaining FROM coupon_instances"
                )
            }
            self.assertEqual(balances["I_FOOD"], 2)
            self.assertEqual(balances["I_SHIP"], 2)
            self.assertEqual(balances["I_ALT"], 0)
            self.assertEqual(balances["I_OTHER"], 1)
            status = con.execute(
                "SELECT status FROM redemptions WHERE order_id='O_OLD'"
            ).fetchone()[0]
            self.assertEqual(status, "CANCELLED")
            # O_NEW never became a redemption.
            self.assertIsNone(
                con.execute(
                    "SELECT 1 FROM redemptions WHERE order_id='O_NEW'"
                ).fetchone()
            )
        finally:
            con.close()

    def test_operations_are_replayable(self):
        first = run_operations(self.work, self.operations)
        second = run_operations(self.work, self.operations)
        # Redemptions are replayed under their frozen request ids; cancels do not
        # refund twice; the template revision is idempotent.
        self.assertEqual(first[2].response["result"], second[2].response["result"])
        self.assertTrue(second[2].response["replayed"] or second[2].response["result"]["status"] == "cancelled")
        con = db.connect(self.work)
        try:
            balances = {
                row["instance_id"]: row["remaining"]
                for row in con.execute(
                    "SELECT instance_id, remaining FROM coupon_instances"
                )
            }
        finally:
            con.close()
        self.assertEqual(balances["I_FOOD"], 2)
        self.assertEqual(balances["I_SHIP"], 2)

    def test_legacy_snapshot_after_operations(self):
        run_operations(self.work, self.operations)
        data = legacy_snapshot(self.work)
        self.assertEqual(data["cancellations"][0]["cancel_id"], "C_OLD")
        self.assertEqual(data["redemptions"][0]["order_id"], "O_OLD")
        self.assertEqual(len(data["redemption_uses"]), 2)

    def test_material_file_is_untouched(self):
        before = MATERIAL_DB.read_bytes()
        run_operations(self.work, self.operations)
        self.assertEqual(MATERIAL_DB.read_bytes(), before)


class TemplateChangeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name) / "history.sqlite"
        shutil.copyfile(MATERIAL_DB, self.work)

    def test_revision_changes_new_eligibility_not_display(self):
        from coupon_service import catalog
        from coupon_service.models import OrderLine, TemplateSpec

        con = db.connect(self.work)
        try:
            db.ensure_schema(con)
            lines = (OrderLine("A", 2, 600),)
            before = ledger.preflight_redeem(con, "U1", lines, ("I_FOOD",))
            self.assertTrue(before.ok)
            with ledger._Transaction(con):
                catalog.revise(
                    con,
                    "T_FOOD",
                    2,
                    TemplateSpec(skus=("A",), minimum=1500, discount=200, group="FOOD"),
                )
            after = ledger.preflight_redeem(con, "U1", lines, ("I_FOOD",))
            self.assertFalse(after.ok)
            self.assertIn("below_minimum", [i.reason for i in after.issues])
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
