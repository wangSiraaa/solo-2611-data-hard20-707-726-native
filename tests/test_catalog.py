"""Catalog-version boundary and template revision semantics."""

from __future__ import annotations

import json
import sqlite3
import unittest

from coupon_service import catalog, ledger
from coupon_service.models import OrderLine, TemplateSpec
from tests.support import HistoryCase, redeem_request

# T_FOOD v2 raises the threshold but keeps the same discount/group.
FOOD_V2 = TemplateSpec(skus=("A",), minimum=1500, discount=200, group="FOOD")


class CatalogBoundaryTests(HistoryCase):
    def test_successful_redemption_pins_version_used(self):
        con = self.connect()
        try:
            with ledger._Transaction(con):
                catalog.revise(con, "T_FOOD", 2, FOOD_V2)
            response = ledger.redeem(
                con,
                "R_BIG",
                redeem_request(
                    "R_BIG",
                    "O_BIG",
                    lines=[{"sku": "A", "qty": 3, "unit_price": 600}],
                    coupon_ids=["I_FOOD"],
                ),
            )
        finally:
            con.close()
        self.assertEqual(response.result["status"], "redeemed")
        self.assertEqual(response.result["discount"], 200)

        con = self.connect()
        try:
            use = con.execute(
                "SELECT template_version, discount FROM redemption_uses "
                "WHERE order_id='O_BIG'"
            ).fetchone()
            self.assertEqual(use["template_version"], 2)
            self.assertEqual(use["discount"], 200)
            qualification = json.loads(
                con.execute(
                    "SELECT qualification_json FROM redemptions "
                    "WHERE order_id='O_BIG'"
                ).fetchone()[0]
            )
            self.assertEqual(qualification["templates"]["T_FOOD"]["version"], 2)
        finally:
            con.close()

    def test_later_revision_does_not_change_old_fact(self):
        con = self.connect()
        try:
            ledger.redeem(
                con,
                "R_BEFORE",
                redeem_request(
                    "R_BEFORE", "O_BEFORE", lines=[{"sku": "A", "qty": 2, "unit_price": 600}]
                ),
            )
            before = con.execute(
                "SELECT template_version, discount FROM redemption_uses "
                "WHERE order_id='O_BEFORE'"
            ).fetchone()
            with ledger._Transaction(con):
                catalog.revise(
                    con,
                    "T_FOOD",
                    2,
                    TemplateSpec(skus=("A",), minimum=1, discount=999, group="FOOD"),
                )
            after = con.execute(
                "SELECT template_version, discount FROM redemption_uses "
                "WHERE order_id='O_BEFORE'"
            ).fetchone()
        finally:
            con.close()
        self.assertEqual(dict(before), dict(after))
        self.assertEqual(after["template_version"], 1)
        self.assertEqual(after["discount"], 200)

    def test_immutable_version_rejects_different_spec(self):
        con = self.connect()
        try:
            with self.assertRaises(ValueError):
                with ledger._Transaction(con):
                    catalog.publish(con, "T_FOOD", 1, FOOD_V2)
        finally:
            con.close()

    def test_revision_must_move_forward(self):
        con = self.connect()
        try:
            with ledger._Transaction(con):
                catalog.revise(con, "T_FOOD", 2, FOOD_V2)
            with self.assertRaises(ValueError):
                with ledger._Transaction(con):
                    catalog.revise(
                        con,
                        "T_FOOD",
                        1,
                        TemplateSpec(skus=("A",), minimum=1, discount=1, group="FOOD"),
                    )
        finally:
            con.close()

    def test_write_transaction_holds_catalog_boundary(self):
        """A competing revision cannot interleave with an open redeem txn."""

        reader = sqlite3.connect(self.db_path, isolation_level=None, timeout=0.05)
        other = sqlite3.connect(self.db_path, isolation_level=None, timeout=0.05)
        try:
            other.row_factory = sqlite3.Row
            other.execute("BEGIN IMMEDIATE")
            # Simulate the redeem transaction having pinned version 1 and being
            # about to decrement: hold the write lock.
            pinned = catalog.get_current(other, "T_FOOD")
            self.assertEqual(pinned.version, 1)
            with self.assertRaises(sqlite3.OperationalError):
                # Different connection cannot revise in the middle of it.
                reader.execute("BEGIN IMMEDIATE")
                catalog.revise(reader, "T_FOOD", 2, FOOD_V2)
        finally:
            try:
                reader.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            reader.close()
            other.execute("ROLLBACK")
            other.close()

    def test_preflight_uses_current_version_after_revision(self):
        con = self.connect()
        try:
            with ledger._Transaction(con):
                catalog.revise(con, "T_FOOD", 2, FOOD_V2)
            evaluation = ledger.preflight_redeem(
                con, "U1", (OrderLine("A", 2, 600),), ("I_FOOD",)
            )
        finally:
            con.close()
        self.assertFalse(evaluation.ok)
        detail = [i for i in evaluation.issues if i.reason == "below_minimum"][0]
        self.assertEqual(detail.detail["template_version"], 2)


if __name__ == "__main__":
    unittest.main()
