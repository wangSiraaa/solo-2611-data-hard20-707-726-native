"""Ledger tests: historical facts, redeem/cancel atomicity, replay, races."""

from __future__ import annotations

import json
import sqlite3
import threading
import unittest

from coupon_service import catalog, db, ledger
from coupon_service.errors import COMBINATION, BALANCE, OWNERSHIP, QUALIFICATION
from coupon_service.models import OrderLine, TemplateSpec
from tests.support import HistoryCase, cancel_request, redeem_request


class HistoryPreservationTests(HistoryCase):
    def test_material_history_is_not_reinitialised(self):
        balances = self.balances()
        # The material rows are already post-deduction facts; they must not be
        # restored to template capacity on load.
        self.assertEqual(balances, {
            "I_FOOD": 1,
            "I_SHIP": 1,
            "I_ALT": 0,
            "I_OTHER": 1,
        })
        self.assertEqual(self.order_status("O_OLD"), "ACTIVE")
        self.assertEqual(
            [use["instance_id"] for use in self.redemption_uses("O_OLD")],
            ["I_FOOD", "I_SHIP"],
        )

    def test_legacy_reader_still_reads_original_columns(self):
        from ledger_reader import snapshot

        data = snapshot(self.db_path)
        self.assertIn("I_FOOD", {row["instance_id"] for row in data["coupon_instances"]})
        self.assertEqual(data["redemptions"][0]["order_id"], "O_OLD")
        self.assertEqual(len(data["redemption_uses"]), 2)


class CancelTests(HistoryCase):
    def test_cancel_returns_recorded_uses_once(self):
        con = self.connect()
        try:
            response = ledger.cancel(con, "R_CANCEL", cancel_request("R_CANCEL", "C_OLD"))
        finally:
            con.close()
        self.assertFalse(response.replayed)
        self.assertEqual(response.result["status"], "cancelled")
        self.assertEqual(self.balances()["I_FOOD"], 2)
        self.assertEqual(self.balances()["I_SHIP"], 2)
        self.assertEqual(self.order_status("O_OLD"), "CANCELLED")

        # Replaying the exact same request id returns the frozen refund.
        con = self.connect()
        try:
            again = ledger.cancel(con, "R_CANCEL", cancel_request("R_CANCEL", "C_OLD"))
        finally:
            con.close()
        self.assertTrue(again.replayed)
        self.assertEqual(again.result, response.result)
        self.assertEqual(self.balances()["I_FOOD"], 2)

    def test_new_cancel_id_cannot_refund_a_cancelled_order(self):
        con = self.connect()
        try:
            ledger.cancel(con, "R_CANCEL", cancel_request("R_CANCEL", "C_OLD"))
            other = ledger.cancel(
                con, "R_CANCEL_2", cancel_request("R_CANCEL_2", "C_OTHER")
            )
        finally:
            con.close()
        self.assertEqual(other.result["status"], "REJECTED")
        self.assertEqual(other.result["code"], "CANCEL_CONFLICT")
        self.assertFalse(other.result["deducted"])
        self.assertEqual(self.balances()["I_FOOD"], 2)
        self.assertEqual(self.balances()["I_SHIP"], 2)

    def test_same_cancel_id_new_request_id_replays_without_refund(self):
        con = self.connect()
        try:
            first = ledger.cancel(con, "R_CANCEL", cancel_request("R_CANCEL", "C_OLD"))
            replay = ledger.cancel(
                con, "R_CANCEL_RETRY", cancel_request("R_CANCEL_RETRY", "C_OLD")
            )
        finally:
            con.close()
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.result["refunded"], first.result["refunded"])
        self.assertEqual(self.balances()["I_FOOD"], 2)

    def test_cancel_requires_same_user(self):
        con = self.connect()
        try:
            response = ledger.cancel(
                con, "R_CANCEL", cancel_request("R_CANCEL", "C_OLD", user_id="U2")
            )
        finally:
            con.close()
        self.assertEqual(response.result["code"], OWNERSHIP)
        self.assertEqual(self.balances()["I_FOOD"], 1)

    def test_cancel_refund_uses_original_uses_after_template_revision(self):
        con = self.connect()
        try:
            with ledger._Transaction(con):
                catalog.revise(
                    con,
                    "T_FOOD",
                    2,
                    TemplateSpec(skus=("A",), minimum=99999, discount=999, group="FOOD"),
                )
            response = ledger.cancel(con, "R_CANCEL", cancel_request("R_CANCEL", "C_OLD"))
        finally:
            con.close()
        # Refund is 1 use each per the persisted uses, not the revised template.
        self.assertEqual(response.result["status"], "cancelled")
        self.assertEqual(self.balances()["I_FOOD"], 2)
        self.assertEqual(self.balances()["I_SHIP"], 2)


class RedeemReplayTests(HistoryCase):
    def test_historical_request_replays_without_rededuction(self):
        con = self.connect()
        try:
            request = json.loads(
                con.execute(
                    "SELECT request_json FROM redemptions WHERE order_id='O_OLD'"
                ).fetchone()[0]
            )
            response = ledger.redeem(con, "R_OLD", request)
        finally:
            con.close()
        self.assertTrue(response.replayed)
        self.assertEqual(response.result["payable"], 900)
        self.assertEqual(response.current_status, "ACTIVE")
        self.assertEqual(self.balances()["I_FOOD"], 1)
        self.assertEqual(self.balances()["I_SHIP"], 1)

    def test_request_id_content_mismatch_is_frozen_rejection(self):
        con = self.connect()
        try:
            request = json.loads(
                con.execute(
                    "SELECT request_json FROM redemptions WHERE order_id='O_OLD'"
                ).fetchone()[0]
            )
            changed = dict(request)
            changed["lines"] = [{"sku": "A", "qty": 3, "unit_price": 600}]
            response = ledger.redeem(con, "R_OLD", changed)
            replay = ledger.redeem(con, "R_OLD", changed)
            # The frozen first result is still intact for the original content.
            original = ledger.redeem(con, "R_OLD", request)
        finally:
            con.close()
        self.assertEqual(response.result["code"], "REQUEST_CONFLICT")
        self.assertFalse(response.result["deducted"])
        # The mismatch cannot overwrite the frozen first result, so repeating it
        # is a deterministic rejection rather than a replay of frozen output.
        self.assertFalse(replay.replayed)
        self.assertEqual(replay.result, response.result)
        self.assertEqual(self.balances()["I_FOOD"], 1)
        self.assertTrue(original.replayed)
        self.assertEqual(original.result["payable"], 900)

    def test_object_key_order_does_not_affect_replay(self):
        con = self.connect()
        try:
            original = json.loads(
                con.execute(
                    "SELECT request_json FROM redemptions WHERE order_id='O_OLD'"
                ).fetchone()[0]
            )
            reordered = {
                "user_id": original["user_id"],
                "coupon_ids": original["coupon_ids"],
                "lines": [
                    {
                        "unit_price": line["unit_price"],
                        "sku": line["sku"],
                        "qty": line["qty"],
                    }
                    for line in original["lines"]
                ],
                "order_id": original["order_id"],
            }
            response = ledger.redeem(con, "R_OLD", reordered)
        finally:
            con.close()
        self.assertTrue(response.replayed)
        self.assertEqual(response.result["payable"], 900)

    def test_list_order_is_significant_for_content_equality(self):
        con = self.connect()
        try:
            request = json.loads(
                con.execute(
                    "SELECT request_json FROM redemptions WHERE order_id='O_OLD'"
                ).fetchone()[0]
            )
            changed = dict(request)
            changed["coupon_ids"] = list(reversed(request["coupon_ids"]))
            response = ledger.redeem(con, "R_OLD", changed)
        finally:
            con.close()
        self.assertEqual(response.result["code"], "REQUEST_CONFLICT")

    def test_new_request_id_same_successful_order_replays_original_fact(self):
        con = self.connect()
        try:
            request = json.loads(
                con.execute(
                    "SELECT request_json FROM redemptions WHERE order_id='O_OLD'"
                ).fetchone()[0]
            )
            response = ledger.redeem(con, "R_OLD_AGAIN", request)
        finally:
            con.close()
        self.assertTrue(response.replayed)
        self.assertEqual(response.result["status"], "redeemed")
        self.assertEqual(self.balances()["I_FOOD"], 1)
        self.assertEqual(self.balances()["I_SHIP"], 1)

    def test_order_id_bound_to_content_even_after_cancel(self):
        con = self.connect()
        try:
            request = json.loads(
                con.execute(
                    "SELECT request_json FROM redemptions WHERE order_id='O_OLD'"
                ).fetchone()[0]
            )
            ledger.cancel(con, "R_CANCEL", cancel_request("R_CANCEL", "C_OLD"))
            # Same order id + same content: replay, no new deduction despite
            # balances having been refunded.
            replay = ledger.redeem(con, "R_OLD_AFTER_CANCEL", request)
            changed = dict(request)
            changed["lines"] = [{"sku": "A", "qty": 1, "unit_price": 600}]
            mismatch = ledger.redeem(con, "R_OLD_CHANGED", changed)
        finally:
            con.close()
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.result["status"], "redeemed")
        self.assertEqual(replay.current_status, "CANCELLED")
        self.assertEqual(self.balances()["I_FOOD"], 2)
        self.assertEqual(mismatch.result["code"], "REQUEST_CONFLICT")

    def test_new_request_id_can_retry_non_successful_order(self):
        con = self.connect()
        try:
            first = ledger.redeem(
                con,
                "R_ALT_FAIL",
                redeem_request("R_ALT_FAIL", "O_ALT", coupon_ids=["I_ALT"]),
            )
            # Give I_ALT a remaining use out-of-band, then retry under a new id.
            con.execute("UPDATE coupon_instances SET remaining=1 WHERE instance_id='I_ALT'")
            con.execute("UPDATE current_templates SET version=1 WHERE template_id='T_ALT'")
            retry = ledger.redeem(
                con,
                "R_ALT_RETRY",
                redeem_request("R_ALT_RETRY", "O_ALT", coupon_ids=["I_ALT"]),
            )
        finally:
            con.close()
        self.assertEqual(first.result["code"], BALANCE)
        self.assertEqual(retry.result["status"], "redeemed")
        self.assertEqual(self.balances()["I_ALT"], 0)


class AtomicityTests(HistoryCase):
    def test_balance_failure_consumes_no_other_coupon(self):
        # A zero-balance instance in a *different* group: eligibility is fine
        # for both coupons, but one has no uses, so neither may be consumed.
        con = self.connect()
        try:
            con.execute(
                "INSERT INTO coupon_instances(instance_id,user_id,template_id,remaining) "
                "VALUES('I_SHIP_EMPTY','U1','T_SHIP',0)"
            )
            response = ledger.redeem(
                con,
                "R_MIXED",
                redeem_request(
                    "R_MIXED",
                    "O_MIXED",
                    coupon_ids=["I_FOOD", "I_SHIP_EMPTY"],
                ),
            )
        finally:
            con.close()
        self.assertEqual(response.result["status"], "REJECTED")
        self.assertEqual(response.result["code"], BALANCE)
        # I_FOOD still has its use; the empty instance stays at zero.
        self.assertEqual(self.balances()["I_FOOD"], 1)
        self.assertEqual(self.balances()["I_SHIP_EMPTY"], 0)

    def test_combination_failure_consumes_nothing(self):
        con = self.connect()
        try:
            # Give I_ALT a use so the only blocker is the FOOD group conflict.
            con.execute("UPDATE coupon_instances SET remaining=1 WHERE instance_id='I_ALT'")
            response = ledger.redeem(
                con,
                "R_MUTUAL",
                redeem_request(
                    "R_MUTUAL",
                    "O_MUTUAL",
                    lines=[{"sku": "A", "qty": 1, "unit_price": 2000}],
                    coupon_ids=["I_FOOD", "I_ALT"],
                ),
            )
        finally:
            con.close()
        self.assertEqual(response.result["code"], COMBINATION)
        self.assertEqual(self.balances()["I_FOOD"], 1)
        self.assertEqual(self.balances()["I_ALT"], 1)


class ConcurrencyTests(HistoryCase):
    def test_two_connections_race_for_last_use(self):
        # Make both instances have exactly one use, and create a second user's
        # instance so two different orders compete for the same balance.
        con = self.connect()
        try:
            con.execute("UPDATE coupon_instances SET remaining=1 WHERE instance_id='I_FOOD'")
        finally:
            con.close()

        results: list[tuple[str, dict]] = []
        barrier = threading.Barrier(2)

        def worker(name: str):
            connection = db.connect(self.db_path)
            try:
                barrier.wait()
                response = ledger.redeem(
                    connection,
                    f"R_RACE_{name}",
                    redeem_request(
                        f"R_RACE_{name}", f"O_RACE_{name}", coupon_ids=["I_FOOD"]
                    ),
                )
                results.append((name, response.result))
            finally:
                connection.close()

        threads = [
            threading.Thread(target=worker, args=(name,)) for name in ("a", "b")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        successes = [r for _, r in results if r["status"] == "redeemed"]
        failures = [r for _, r in results if r["status"] == "REJECTED"]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["code"], BALANCE)
        self.assertEqual(self.balances()["I_FOOD"], 0)

    def test_reopen_after_writes_sees_consistent_state(self):
        con = self.connect()
        try:
            ledger.redeem(
                con,
                "R_REOPEN",
                redeem_request("R_REOPEN", "O_REOPEN", coupon_ids=["I_SHIP"]),
            )
        finally:
            con.close()
        # Fresh connection / process-equivalent reopen.
        con = self.connect()
        try:
            row = con.execute(
                "SELECT status FROM redemptions WHERE order_id='O_REOPEN'"
            ).fetchone()
            self.assertEqual(row["status"], "ACTIVE")
            remaining = con.execute(
                "SELECT remaining FROM coupon_instances WHERE instance_id='I_SHIP'"
            ).fetchone()[0]
            self.assertEqual(remaining, 0)
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
