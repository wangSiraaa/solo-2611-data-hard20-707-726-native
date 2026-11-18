"""Shared test helpers: fresh working copies of the material history."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from coupon_service import db

WORKSPACE = Path(__file__).resolve().parent.parent
MATERIAL_DB = WORKSPACE / "materials" / "709" / "history.sqlite"


class HistoryCase(unittest.TestCase):
    """Each test gets its own copy of the frozen material database."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "history.sqlite"
        shutil.copyfile(MATERIAL_DB, self.db_path)
        con = db.connect(self.db_path)
        try:
            db.ensure_schema(con)
        finally:
            con.close()

    def connect(self):
        return db.connect(self.db_path)

    def balances(self) -> dict[str, int]:
        con = self.connect()
        try:
            rows = con.execute(
                "SELECT instance_id, remaining FROM coupon_instances"
            ).fetchall()
            return {row["instance_id"]: int(row["remaining"]) for row in rows}
        finally:
            con.close()

    def order_status(self, order_id: str):
        con = self.connect()
        try:
            row = con.execute(
                "SELECT status FROM redemptions WHERE order_id=?", (order_id,)
            ).fetchone()
            return row["status"] if row else None
        finally:
            con.close()

    def redemption_uses(self, order_id: str):
        con = self.connect()
        try:
            return [
                dict(row)
                for row in con.execute(
                    "SELECT * FROM redemption_uses WHERE order_id=? "
                    "ORDER BY instance_id",
                    (order_id,),
                )
            ]
        finally:
            con.close()


def redeem_request(
    request_id: str,
    order_id: str,
    user_id: str = "U1",
    lines=None,
    coupon_ids=None,
) -> dict:
    return {
        "op": "redeem",
        "request_id": request_id,
        "order_id": order_id,
        "user_id": user_id,
        "lines": lines
        if lines is not None
        else [{"sku": "A", "qty": 2, "unit_price": 600}],
        "coupon_ids": coupon_ids if coupon_ids is not None else ["I_FOOD"],
    }


def cancel_request(
    request_id: str,
    cancel_id: str,
    order_id: str = "O_OLD",
    user_id: str = "U1",
) -> dict:
    return {
        "op": "cancel",
        "request_id": request_id,
        "cancel_id": cancel_id,
        "order_id": order_id,
        "user_id": user_id,
    }
