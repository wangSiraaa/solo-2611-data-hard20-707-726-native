"""Cross-cutting guarantees: entry-point separation, replay freeze, families."""

from __future__ import annotations

import unittest

from coupon_service import catalog, errors, ledger
from coupon_service.models import (
    CouponInput,
    OrderLine,
    QualificationRequest,
    TemplatePin,
    TemplateSpec,
)
from coupon_service.qualification import evaluate
from tests.support import HistoryCase, cancel_request, redeem_request


class EntryPointSeparationTests(HistoryCase):
    def test_pure_qualification_never_needs_balances(self):
        # The instance does not exist in the ledger at all; the pure service
        # still evaluates eligibility from explicitly pinned template versions.
        spec = TemplateSpec(skus=("A",), minimum=1000, discount=200, group="FOOD")
        request = QualificationRequest(
            user_id="U1",
            lines=(OrderLine("A", 2, 600),),
            coupons=(CouponInput("I_GHOST", "U1", "T_FOOD", 1),),
            templates=(TemplatePin("T_FOOD", 1, spec),),
        )
        evaluation = evaluate(request)
        self.assertTrue(evaluation.ok)
        self.assertEqual(evaluation.plan.payable, 1000)

    def test_ledger_reports_four_rejection_families(self):
        con = self.connect()
        try:
            with ledger._Transaction(con):
                catalog.revise(
                    con,
                    "T_FOOD",
                    2,
                    TemplateSpec(skus=("A",), minimum=1500, discount=200, group="FOOD"),
                )
            qualification = ledger.preflight_redeem(
                con, "U1", (OrderLine("A", 2, 600),), ("I_FOOD",)
            )
            combination = ledger.preflight_redeem(
                con, "U1", (OrderLine("A", 1, 2000),), ("I_FOOD", "I_ALT")
            )
            ownership = ledger.preflight_redeem(
                con, "U1", (OrderLine("A", 1, 2000),), ("I_OTHER",)
            )
            empty = ledger.preflight_redeem(
                con, "U1", (OrderLine("A", 1, 2000),), ("I_ALT",)
            )
        finally:
            con.close()

        self.assertEqual(qualification.primary, errors.QUALIFICATION)
        self.assertEqual(combination.primary, errors.COMBINATION)
        self.assertEqual(ownership.primary, errors.OWNERSHIP)
        self.assertEqual(empty.primary, errors.BALANCE)

    def test_preflight_writes_nothing(self):
        before = self.balances()
        con = self.connect()
        try:
            ledger.preflight_redeem(
                con, "U1", (OrderLine("A", 2, 600),), ("I_FOOD",)
            )
        finally:
            con.close()
        self.assertEqual(before, self.balances())


class RejectionFreezeTests(HistoryCase):
    def test_rejection_is_frozen_and_replayed(self):
        con = self.connect()
        try:
            first = ledger.redeem(
                con,
                "R_REJECT",
                redeem_request("R_REJECT", "O_REJECT", coupon_ids=["I_ALT"]),
            )
            replay = ledger.redeem(
                con,
                "R_REJECT",
                redeem_request("R_REJECT", "O_REJECT", coupon_ids=["I_ALT"]),
            )
        finally:
            con.close()
        self.assertEqual(first.result["status"], "REJECTED")
        self.assertEqual(first.result["code"], errors.BALANCE)
        self.assertFalse(first.result["deducted"])
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.result, first.result)
        # No redemption row and no uses were fabricated.
        self.assertIsNone(self.order_status("O_REJECT"))
        self.assertEqual(self.redemption_uses("O_REJECT"), [])

    def test_request_id_cannot_switch_between_kinds(self):
        con = self.connect()
        try:
            ledger.redeem(
                con,
                "R_SHARED",
                redeem_request("R_SHARED", "O_SHARED", coupon_ids=["I_ALT"]),
            )
            response = ledger.cancel(
                con,
                "R_SHARED",
                cancel_request("R_SHARED", "C_SHARED", order_id="O_OLD"),
            )
        finally:
            con.close()
        self.assertEqual(response.result["code"], "REQUEST_CONFLICT")
        self.assertFalse(response.result["deducted"])

    def test_new_request_id_can_retry_unsuccessful_order(self):
        con = self.connect()
        try:
            first = ledger.redeem(
                con,
                "R_REJECT",
                redeem_request("R_REJECT", "O_REJECT", coupon_ids=["I_ALT"]),
            )
            retry = ledger.redeem(
                con,
                "R_REJECT_RETRY",
                redeem_request("R_REJECT_RETRY", "O_REJECT", coupon_ids=["I_FOOD"]),
            )
        finally:
            con.close()
        self.assertEqual(first.result["status"], "REJECTED")
        self.assertEqual(retry.result["status"], "redeemed")
        self.assertEqual(self.order_status("O_REJECT"), "ACTIVE")


class ReplayAndReopenTests(HistoryCase):
    def test_documented_historical_cancel_refunds_once(self):
        con = self.connect()
        try:
            first = ledger.cancel(con, "R_CANCEL", cancel_request("R_CANCEL", "C_OLD"))
            second = ledger.cancel(con, "R_CANCEL", cancel_request("R_CANCEL", "C_OLD"))
        finally:
            con.close()
        self.assertEqual(first.result, second.result)
        self.assertTrue(second.replayed)
        self.assertEqual(self.balances()["I_FOOD"], 2)
        self.assertEqual(self.balances()["I_SHIP"], 2)

    def test_reopen_after_full_cycle(self):
        con = self.connect()
        try:
            redeem = ledger.redeem(
                con,
                "R_CYCLE",
                redeem_request("R_CYCLE", "O_CYCLE", coupon_ids=["I_FOOD"]),
            )
            cancel = ledger.cancel(
                con,
                "R_CYCLE_CANCEL",
                cancel_request("R_CYCLE_CANCEL", "C_CYCLE", order_id="O_CYCLE"),
            )
        finally:
            con.close()
        self.assertEqual(redeem.result["status"], "redeemed")
        self.assertEqual(cancel.result["status"], "cancelled")

        # Reopen: a fresh connection sees the same facts and balances.
        con = self.connect()
        try:
            replay = ledger.redeem(
                con,
                "R_CYCLE",
                redeem_request("R_CYCLE", "O_CYCLE", coupon_ids=["I_FOOD"]),
            )
        finally:
            con.close()
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.current_status, "CANCELLED")
        self.assertEqual(self.balances()["I_FOOD"], 1)


if __name__ == "__main__":
    unittest.main()
