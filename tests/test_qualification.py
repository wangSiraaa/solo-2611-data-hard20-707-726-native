"""Pure qualification tests: no ledger mutation, no balances."""

from __future__ import annotations

import unittest

from coupon_service import errors
from coupon_service.models import (
    CouponInput,
    OrderLine,
    QualificationRequest,
    TemplatePin,
    TemplateSpec,
)
from coupon_service.qualification import evaluate

FOOD_V1 = TemplateSpec(skus=("A",), minimum=1000, discount=200, group="FOOD")
FOOD_V2 = TemplateSpec(skus=("A",), minimum=1500, discount=200, group="FOOD")
SHIP_V1 = TemplateSpec(skus=("A", "B"), minimum=0, discount=100, group="SHIP")
ALT_V1 = TemplateSpec(skus=("A",), minimum=1000, discount=100, group="FOOD")


def coupon(instance_id, template_id="T_FOOD", version=1, owner="U1"):
    return CouponInput(
        instance_id=instance_id,
        owner_user_id=owner,
        template_id=template_id,
        template_version=version,
    )


def request(lines, coupons, templates, user="U1"):
    return QualificationRequest(
        user_id=user,
        lines=tuple(lines),
        coupons=tuple(coupons),
        templates=tuple(templates),
    )


def pin(template_id, version, spec):
    return TemplatePin(template_id=template_id, version=version, spec=spec)


class QualificationTests(unittest.TestCase):
    def test_duplicate_sku_lines_all_count_towards_gross(self):
        lines = [
            OrderLine("A", 1, 600),
            OrderLine("A", 1, 600),
        ]
        evaluation = evaluate(
            request(
                lines,
                [coupon("I_FOOD")],
                [pin("T_FOOD", 1, FOOD_V1)],
            )
        )
        self.assertTrue(evaluation.ok)
        self.assertEqual(evaluation.plan.gross, 1200)
        self.assertEqual(evaluation.plan.payable, 1000)
        self.assertEqual(evaluation.plan.coupons[0].applicable_gross, 1200)

    def test_threshold_uses_applicable_gross_before_any_discount(self):
        lines = [OrderLine("B", 1, 1200)]
        evaluation = evaluate(
            request(
                lines,
                [coupon("I_FOOD")],
                [pin("T_FOOD", 1, FOOD_V1)],
            )
        )
        self.assertFalse(evaluation.ok)
        self.assertIn("template_not_applicable", [i.reason for i in evaluation.issues])

    def test_raises_threshold_rejects_new_version_but_v1_passes(self):
        lines = [OrderLine("A", 2, 600)]
        with_v1 = evaluate(
            request(lines, [coupon("I_FOOD")], [pin("T_FOOD", 1, FOOD_V1)])
        )
        with_v2 = evaluate(
            request(
                lines,
                [coupon("I_FOOD", version=2)],
                [pin("T_FOOD", 2, FOOD_V2)],
            )
        )
        self.assertTrue(with_v1.ok)
        self.assertFalse(with_v2.ok)
        self.assertEqual(with_v2.primary, errors.QUALIFICATION)
        self.assertIn("below_minimum", [i.reason for i in with_v2.issues])

    def test_same_group_rejected_different_groups_stack(self):
        lines = [OrderLine("A", 1, 2000)]
        same_group = evaluate(
            request(
                lines,
                [coupon("I_FOOD"), coupon("I_ALT", "T_ALT")],
                [pin("T_FOOD", 1, FOOD_V1), pin("T_ALT", 1, ALT_V1)],
            )
        )
        self.assertFalse(same_group.ok)
        self.assertEqual(same_group.primary, errors.COMBINATION)

        stacked = evaluate(
            request(
                lines,
                [coupon("I_FOOD"), coupon("I_SHIP", "T_SHIP")],
                [pin("T_FOOD", 1, FOOD_V1), pin("T_SHIP", 1, SHIP_V1)],
            )
        )
        self.assertTrue(stacked.ok)
        self.assertEqual(stacked.plan.total_discount, 300)
        self.assertEqual(stacked.plan.payable, 1700)

    def test_total_discount_cannot_exceed_gross(self):
        lines = [OrderLine("A", 1, 150)]
        stacked = evaluate(
            request(
                lines,
                [coupon("I_FOOD"), coupon("I_SHIP", "T_SHIP")],
                [pin("T_FOOD", 1, FOOD_V1), pin("T_SHIP", 1, SHIP_V1)],
            )
        )
        self.assertFalse(stacked.ok)
        self.assertIn("below_minimum", [i.reason for i in stacked.issues])

        # Two independently-qualifying coupons whose fixed discounts together
        # exceed the full original gross.
        low_food = TemplateSpec(skus=("A",), minimum=100, discount=200, group="FOOD")
        cap = evaluate(
            request(
                [OrderLine("A", 1, 250)],
                [coupon("I_FOOD"), coupon("I_SHIP", "T_SHIP")],
                [pin("T_FOOD", 1, low_food), pin("T_SHIP", 1, SHIP_V1)],
            )
        )
        self.assertIn("discount_exceeds_gross", [i.reason for i in cap.issues])

    def test_duplicate_instance_in_one_request_rejected(self):
        lines = [OrderLine("A", 1, 2000)]
        evaluation = evaluate(
            request(
                lines,
                [coupon("I_FOOD"), coupon("I_FOOD")],
                [pin("T_FOOD", 1, FOOD_V1)],
            )
        )
        self.assertFalse(evaluation.ok)
        self.assertIn(
            "duplicate_coupon_instance", [i.reason for i in evaluation.issues]
        )

    def test_instance_not_owned_is_ownership_family(self):
        lines = [OrderLine("A", 1, 2000)]
        evaluation = evaluate(
            request(
                lines,
                [coupon("I_OTHER", owner="U2")],
                [pin("T_FOOD", 1, FOOD_V1)],
            )
        )
        self.assertFalse(evaluation.ok)
        self.assertEqual(evaluation.primary, errors.OWNERSHIP)

    def test_unknown_template_version_rejected(self):
        lines = [OrderLine("A", 1, 2000)]
        evaluation = evaluate(
            request(lines, [coupon("I_FOOD", version=9)], [pin("T_FOOD", 1, FOOD_V1)])
        )
        self.assertFalse(evaluation.ok)
        self.assertIn(
            "unknown_template_version", [i.reason for i in evaluation.issues]
        )

    def test_no_auto_selection_empty_coupons_is_just_no_discount(self):
        lines = [OrderLine("A", 1, 2000)]
        evaluation = evaluate(request(lines, [], []))
        self.assertTrue(evaluation.ok)
        self.assertEqual(evaluation.plan.total_discount, 0)
        self.assertEqual(evaluation.plan.payable, 2000)

    def test_gross_counts_only_applicable_skus_for_threshold(self):
        lines = [OrderLine("A", 1, 900), OrderLine("C", 1, 5000)]
        evaluation = evaluate(
            request(lines, [coupon("I_FOOD")], [pin("T_FOOD", 1, FOOD_V1)])
        )
        self.assertFalse(evaluation.ok)
        # Whole-order gross is 5900 but applicable gross is only 900.
        self.assertIn("below_minimum", [i.reason for i in evaluation.issues])


if __name__ == "__main__":
    unittest.main()
