"""Coupon qualification and redemption-ledger service.

Two independent entry points share one database but no mutable state:

* :mod:`coupon_service.qualification` - pure eligibility/combination rules.
  It never reads balances and never selects coupons.
* :mod:`coupon_service.ledger` - transactional redeem/cancel, ownership,
  remaining counts, request replay and cancellation refunds.

Templates are stable ids with immutable, explicitly pinned versions; amounts
are integer cents throughout.
"""

from __future__ import annotations

from .catalog import TemplateVersion
from .ledger import (
    CancelResponse,
    RedeemResponse,
    cancel,
    current_balances,
    order_status,
    preflight_redeem,
    redeem,
)
from .models import (
    CouponInput,
    OrderLine,
    QualificationRequest,
    TemplatePin,
    TemplateSpec,
)
from .qualification import CouponPlan, Evaluation, QualificationPlan, evaluate

__all__ = [
    "TemplateVersion",
    "TemplateSpec",
    "TemplatePin",
    "OrderLine",
    "CouponInput",
    "QualificationRequest",
    "Evaluation",
    "QualificationPlan",
    "CouponPlan",
    "evaluate",
    "preflight_redeem",
    "redeem",
    "cancel",
    "current_balances",
    "order_status",
    "RedeemResponse",
    "CancelResponse",
]

__version__ = "1.0.0"
