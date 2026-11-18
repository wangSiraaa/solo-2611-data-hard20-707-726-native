"""Pure coupon-qualification service.

This module is deliberately storage-free: it consumes an explicit list of
order lines, coupon instances (each with its owner and an *explicitly pinned*
template id + version), and the pinned template specs.  It never reads a
balance and never selects coupons on its own.

Semantics
---------
* A coupon applies when at least one order line has an applicable SKU.
* ``applicable gross`` is the sum of ``qty * unit_price`` over **all** lines
  whose SKU is in the template's SKU set.  Duplicate SKU lines each count.
* The threshold is compared against that applicable gross *before* any other
  coupon's discount.
* Each qualifying coupon contributes its fixed ``discount``.
* At most one instance per mutually exclusive group; distinct groups stack.
* Total discount may not exceed the order's full original gross.
* The caller supplies the exact coupon list; there is no auto-selection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from . import errors
from .errors import Issue, issue
from .models import (
    CouponInput,
    OrderLine,
    QualificationRequest,
    TemplatePin,
    TemplateSpec,
    duplicates,
)

# Lower value wins when several independent issues exist.  Combination is
# checked before ownership/qualification so that `I_FOOD + I_ALT` is reported
# as a group conflict even if `I_ALT` also happens to have no balance.
_FAMILY_PRIORITY = {
    errors.INVALID_REQUEST: 0,
    errors.COMBINATION: 1,
    errors.OWNERSHIP: 2,
    errors.QUALIFICATION: 3,
    errors.STALE_CATALOG: 3,
    errors.BALANCE: 4,
    errors.NOT_FOUND: 5,
}


def primary_family(issues: Iterable[Issue]) -> str | None:
    best: Issue | None = None
    for item in issues:
        if best is None:
            best = item
        elif _FAMILY_PRIORITY.get(item.family, 99) < _FAMILY_PRIORITY.get(
            best.family, 99
        ):
            best = item
    return best.family if best else None


@dataclass(frozen=True)
class CouponPlan:
    """The part of a coupon's qualification frozen into the ledger."""

    instance_id: str
    template_id: str
    template_version: int
    group: str
    discount: int
    applicable_gross: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "template_id": self.template_id,
            "template_version": self.template_version,
            "group": self.group,
            "discount": self.discount,
            "applicable_gross": self.applicable_gross,
        }


@dataclass(frozen=True)
class QualificationPlan:
    user_id: str
    gross: int
    total_discount: int
    payable: int
    coupons: tuple[CouponPlan, ...]
    templates: dict[str, dict[str, Any]]
    groups: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "gross": self.gross,
            "discount": self.total_discount,
            "payable": self.payable,
            "coupons": [coupon.as_dict() for coupon in self.coupons],
            "templates": self.templates,
            "groups": self.groups,
        }


@dataclass(frozen=True)
class Evaluation:
    ok: bool
    issues: tuple[Issue, ...]
    plan: QualificationPlan | None

    @property
    def primary(self) -> str | None:
        return primary_family(self.issues)

    def reason_dicts(self) -> list[dict[str, Any]]:
        return [item.as_dict() for item in self.issues]

    def reject_payload(self) -> dict[str, Any]:
        return {
            "status": "REJECTED",
            "code": self.primary,
            "issues": self.reason_dicts(),
            "deducted": False,
        }


def _gross(lines: Iterable[OrderLine]) -> int:
    # Iterate every line: duplicate SKU rows must not be collapsed.
    total = 0
    for line in lines:
        total += line.qty * line.unit_price
    return total


def _applicable_gross(lines: Iterable[OrderLine], spec: TemplateSpec) -> tuple[bool, int]:
    """Return ``(has_applicable_line, total)`` for *spec*'s SKU set.

    Duplicate SKU rows each contribute; a free (unit_price 0) applicable line
    still counts as applicable even though it adds nothing to the total.
    """

    has_applicable = False
    total = 0
    for line in lines:
        if spec.applies_to(line.sku):
            has_applicable = True
            total += line.qty * line.unit_price
    return has_applicable, total


def evaluate(request: QualificationRequest) -> Evaluation:
    """Evaluate eligibility, ownership and combination for *request*.

    Balance is intentionally absent: this function cannot read it.
    """

    issues: list[Issue] = []

    # --- duplicate instance ids in one application ------------------------
    repeated = duplicates([coupon.instance_id for coupon in request.coupons])
    for instance_id in repeated:
        issues.append(issue("duplicate_coupon_instance", instance_id=instance_id))

    template_index: dict[tuple[str, int], TemplatePin] = request.template_index()

    # --- ownership and template pinning -----------------------------------
    coupons: list[tuple[CouponInput, TemplatePin]] = []
    for coupon in request.coupons:
        if coupon.owner_user_id != request.user_id:
            issues.append(
                issue(
                    "instance_not_owned",
                    instance_id=coupon.instance_id,
                    owner_user_id=coupon.owner_user_id,
                    request_user_id=request.user_id,
                )
            )
        pin = template_index.get((coupon.template_id, coupon.template_version))
        if pin is None:
            issues.append(
                issue(
                    "unknown_template_version",
                    instance_id=coupon.instance_id,
                    template_id=coupon.template_id,
                    template_version=coupon.template_version,
                )
            )
            continue
        coupons.append((coupon, pin))

    # --- applicability and thresholds (each coupon independently) ----------
    gross = _gross(request.lines)
    planned: list[CouponPlan] = []
    for coupon, pin in coupons:
        has_applicable, applicable = _applicable_gross(request.lines, pin.spec)
        if not has_applicable:
            issues.append(
                issue(
                    "template_not_applicable",
                    instance_id=coupon.instance_id,
                    template_id=pin.template_id,
                    template_version=pin.version,
                    skus=list(pin.spec.skus),
                )
            )
            continue
        if applicable < pin.spec.minimum:
            issues.append(
                issue(
                    "below_minimum",
                    instance_id=coupon.instance_id,
                    template_id=pin.template_id,
                    template_version=pin.version,
                    minimum=pin.spec.minimum,
                    applicable_gross=applicable,
                )
            )
            continue
        planned.append(
            CouponPlan(
                instance_id=coupon.instance_id,
                template_id=pin.template_id,
                template_version=pin.version,
                group=pin.spec.group,
                discount=pin.spec.discount,
                applicable_gross=applicable,
            )
        )

    # --- group exclusivity -------------------------------------------------
    by_group: dict[str, list[str]] = {}
    for coupon in planned:
        by_group.setdefault(coupon.group, []).append(coupon.instance_id)
    for group, instance_ids in by_group.items():
        if len(instance_ids) > 1:
            issues.append(
                issue(
                    "mutually_exclusive_group",
                    group=group,
                    instance_ids=instance_ids,
                )
            )

    # --- discount cap ------------------------------------------------------
    total_discount = sum(coupon.discount for coupon in planned)
    if total_discount > gross:
        issues.append(
            issue(
                "discount_exceeds_gross",
                gross=gross,
                total_discount=total_discount,
            )
        )

    if issues:
        return Evaluation(ok=False, issues=tuple(issues), plan=None)

    templates: dict[str, dict[str, Any]] = {}
    for _, pin in coupons:
        templates[pin.template_id] = {
            "version": pin.version,
            "spec": pin.spec.as_dict(),
        }
    groups: dict[str, str] = {}
    for coupon in planned:
        groups[coupon.group] = coupon.instance_id

    plan = QualificationPlan(
        user_id=request.user_id,
        gross=gross,
        total_discount=total_discount,
        payable=gross - total_discount,
        coupons=tuple(planned),
        templates=templates,
        groups=groups,
    )
    return Evaluation(ok=True, issues=(), plan=plan)


def evaluate_with_balances(
    request: QualificationRequest, remaining: dict[str, int]
) -> Evaluation:
    """Deprecated shim: qualification rules stay pure.

    Balance is a ledger concern; use
    :func:`coupon_service.ledger.preflight_redeem` for a read-only check that
    also reports balance rejections.  This helper exists only so callers that
    explicitly hand over balances keep working; it never reads a database.
    """

    evaluation = evaluate(request)
    issues = list(evaluation.issues)
    for coupon in request.coupons:
        available = remaining.get(coupon.instance_id, 0)
        if available < 1:
            issues.append(
                issue(
                    "no_remaining_uses",
                    instance_id=coupon.instance_id,
                    remaining=available,
                )
            )
    if not issues:
        return evaluation
    return Evaluation(ok=False, issues=tuple(issues), plan=None)
