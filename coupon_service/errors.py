"""Structured, machine-readable rejection reasons.

The public API never signals a business rejection by raising a bare
``ValueError``; it returns a frozen result whose ``status`` is ``REJECTED`` and
whose ``code`` distinguishes the four rejection families required by the spec:

* ``QUALIFICATION`` - template applicability, threshold, tenant/ownership of
  the *template*, group/discount caps, stale inputs, unknown versions.
* ``COMBINATION``   - two instances from the same mutually exclusive group, or
  duplicate coupon instances in one request.
* ``OWNERSHIP``     - an instance belongs to another user.
* ``BALANCE``       - an owned, applicable instance has no remaining uses.

Request-replay conflicts (same ``request_id`` with different content, or an
order already bound to other content) and cancellation-replay conflicts are
their own families because they are not eligibility questions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


QUALIFICATION = "QUALIFICATION"
COMBINATION = "COMBINATION"
OWNERSHIP = "OWNERSHIP"
BALANCE = "BALANCE"
REQUEST_CONFLICT = "REQUEST_CONFLICT"
CANCEL_CONFLICT = "CANCEL_CONFLICT"
INVALID_REQUEST = "INVALID_REQUEST"
STALE_CATALOG = "STALE_CATALOG"
NOT_FOUND = "NOT_FOUND"

# Fine-grained reason codes.  The family is derived from the prefix so callers
# can switch on either granularity.
REASONS: dict[str, str] = {
    # qualification
    "unknown_coupon": QUALIFICATION,
    "unknown_template_version": QUALIFICATION,
    "template_not_applicable": QUALIFICATION,
    "below_minimum": QUALIFICATION,
    "discount_exceeds_gross": QUALIFICATION,
    "stale_template_version": STALE_CATALOG,
    # combination
    "duplicate_coupon_instance": COMBINATION,
    "mutually_exclusive_group": COMBINATION,
    # ownership
    "instance_not_owned": OWNERSHIP,
    "order_user_mismatch": OWNERSHIP,
    # balance
    "no_remaining_uses": BALANCE,
    # request replay
    "request_id_content_mismatch": REQUEST_CONFLICT,
    "order_content_mismatch": REQUEST_CONFLICT,
    "request_id_kind_mismatch": REQUEST_CONFLICT,
    # cancel replay
    "already_cancelled": CANCEL_CONFLICT,
    "cancel_id_content_mismatch": CANCEL_CONFLICT,
    # invalid input
    "invalid_request": INVALID_REQUEST,
    # lookup
    "not_found": NOT_FOUND,
}


def family(reason: str) -> str:
    return REASONS.get(reason, INVALID_REQUEST)


@dataclass(frozen=True)
class Issue:
    """A single rejection reason with optional context."""

    reason: str
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def family(self) -> str:
        return family(self.reason)

    def as_dict(self) -> dict[str, Any]:
        return {"reason": self.reason, "family": self.family, "detail": self.detail}


def issue(reason: str, **detail: Any) -> Issue:
    return Issue(reason=reason, detail=detail)


class RequestError(ValueError):
    """Invalid input shape (not a business rejection)."""

    def __init__(self, message: str, reason: str = "invalid_request", **detail: Any):
        super().__init__(message)
        self.reason = reason
        self.detail = detail
