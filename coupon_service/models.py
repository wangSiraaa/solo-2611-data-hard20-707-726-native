"""Request models and strict validation for the two service entry points.

Money is always an integer number of cents.  Quantities are positive integers,
unit prices are non-negative integers.  Booleans are rejected even though
``bool`` is an ``int`` subclass in Python.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .errors import RequestError
from . import jsonutil


def _require_object(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RequestError(f"{what} must be a JSON object")
    return value


def _require_str(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise RequestError(f"{what} must be a non-empty string")
    return value


def _require_int(value: Any, what: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RequestError(f"{what} must be an integer")
    if minimum is not None and value < minimum:
        raise RequestError(f"{what} must be >= {minimum}")
    return value


@dataclass(frozen=True)
class OrderLine:
    sku: str
    qty: int
    unit_price: int

    @property
    def amount(self) -> int:
        return self.qty * self.unit_price

    def as_dict(self) -> dict[str, Any]:
        return {"sku": self.sku, "qty": self.qty, "unit_price": self.unit_price}


@dataclass(frozen=True)
class TemplateSpec:
    skus: tuple[str, ...]
    minimum: int
    discount: int
    group: str

    @classmethod
    def parse(cls, raw: Any, what: str = "template spec") -> "TemplateSpec":
        spec = _require_object(raw, what)
        skus_raw = spec.get("skus")
        if not isinstance(skus_raw, list) or not skus_raw:
            raise RequestError(f"{what}.skus must be a non-empty list")
        skus: list[str] = []
        for index, sku in enumerate(skus_raw):
            skus.append(_require_str(sku, f"{what}.skus[{index}]"))
        minimum = _require_int(spec.get("minimum"), f"{what}.minimum", minimum=0)
        discount = _require_int(spec.get("discount"), f"{what}.discount", minimum=0)
        group = _require_str(spec.get("group"), f"{what}.group")
        # The SKU collection is a set; sorting makes storage and equality stable.
        return cls(
            skus=tuple(sorted(set(skus))),
            minimum=minimum,
            discount=discount,
            group=group,
        )

    def applies_to(self, sku: str) -> bool:
        return sku in self.skus

    def as_dict(self) -> dict[str, Any]:
        return {
            "skus": list(self.skus),
            "minimum": self.minimum,
            "discount": self.discount,
            "group": self.group,
        }


@dataclass(frozen=True)
class TemplatePin:
    """An explicitly selected template revision."""

    template_id: str
    version: int
    spec: TemplateSpec

    @classmethod
    def parse(cls, raw: Any, what: str = "template") -> "TemplatePin":
        obj = _require_object(raw, what)
        template_id = _require_str(obj.get("template_id"), f"{what}.template_id")
        version = _require_int(obj.get("version"), f"{what}.version", minimum=1)
        spec = TemplateSpec.parse(obj.get("spec"), f"{what}.spec")
        return cls(template_id=template_id, version=version, spec=spec)

    def key(self) -> tuple[str, int]:
        return (self.template_id, self.version)

    def as_dict(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "version": self.version,
            "spec": self.spec.as_dict(),
        }


@dataclass(frozen=True)
class CouponInput:
    """An instance/template binding consumed by the qualification service.

    ``owner_user_id`` comes from the ledger's instance row.  ``template_version``
    is pinned explicitly; the qualification service never asks for "latest".
    """

    instance_id: str
    owner_user_id: str
    template_id: str
    template_version: int

    @classmethod
    def parse(cls, raw: Any, what: str = "coupon") -> "CouponInput":
        obj = _require_object(raw, what)
        return cls(
            instance_id=_require_str(obj.get("instance_id"), f"{what}.instance_id"),
            owner_user_id=_require_str(
                obj.get("owner_user_id"), f"{what}.owner_user_id"
            ),
            template_id=_require_str(obj.get("template_id"), f"{what}.template_id"),
            template_version=_require_int(
                obj.get("template_version"), f"{what}.template_version", minimum=1
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "owner_user_id": self.owner_user_id,
            "template_id": self.template_id,
            "template_version": self.template_version,
        }


@dataclass(frozen=True)
class QualificationRequest:
    user_id: str
    lines: tuple[OrderLine, ...]
    coupons: tuple[CouponInput, ...]
    templates: tuple[TemplatePin, ...]

    @classmethod
    def parse(cls, raw: Any) -> "QualificationRequest":
        obj = _require_object(raw, "qualification request")
        user_id = _require_str(obj.get("user_id"), "user_id")

        lines_raw = obj.get("lines")
        if not isinstance(lines_raw, list) or not lines_raw:
            raise RequestError("lines must be a non-empty list")
        lines: list[OrderLine] = []
        for index, item in enumerate(lines_raw):
            line = _require_object(item, f"lines[{index}]")
            lines.append(
                OrderLine(
                    sku=_require_str(line.get("sku"), f"lines[{index}].sku"),
                    qty=_require_int(line.get("qty"), f"lines[{index}].qty", minimum=1),
                    unit_price=_require_int(
                        line.get("unit_price"),
                        f"lines[{index}].unit_price",
                        minimum=0,
                    ),
                )
            )

        coupons_raw = obj.get("coupons")
        if not isinstance(coupons_raw, list):
            raise RequestError("coupons must be a list")
        coupons = tuple(
            CouponInput.parse(item, f"coupons[{index}]")
            for index, item in enumerate(coupons_raw)
        )

        templates_raw = obj.get("templates")
        if not isinstance(templates_raw, list):
            raise RequestError("templates must be a list")
        templates = tuple(
            TemplatePin.parse(item, f"templates[{index}]")
            for index, item in enumerate(templates_raw)
        )

        return cls(
            user_id=user_id,
            lines=tuple(lines),
            coupons=coupons,
            templates=templates,
        )

    def template_index(self) -> dict[tuple[str, int], TemplatePin]:
        return {pin.key(): pin for pin in self.templates}


# ---------------------------------------------------------------------------
# Frozen request content ("request_json")
# ---------------------------------------------------------------------------

REDEEM_CONTENT_KEYS = ("order_id", "user_id", "lines", "coupon_ids")
CANCEL_CONTENT_KEYS = ("cancel_id", "order_id", "user_id")


def redeem_content(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Domain content of a redeem request, excluding harness fields.

    Missing keys are represented as ``None`` so a malformed first request is
    still replayable byte-for-byte.  Object key order is normalised by
    :mod:`jsonutil`; line and coupon list order is preserved.
    """

    return {key: raw.get(key) for key in REDEEM_CONTENT_KEYS}


def cancel_content(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {key: raw.get(key) for key in CANCEL_CONTENT_KEYS}


def content_json(content: Mapping[str, Any]) -> str:
    return jsonutil.dumps(content)


def parse_redeem_content(raw: Any) -> tuple[str, str, tuple[OrderLine, ...], tuple[str, ...]]:
    """Validate redeem domain fields.

    Returns ``(order_id, user_id, lines, coupon_ids)``.  Raises
    :class:`RequestError` for malformed input; callers may freeze that as an
    ``INVALID_REQUEST`` rejection.
    """

    obj = _require_object(raw, "redeem request")
    order_id = _require_str(obj.get("order_id"), "order_id")
    user_id = _require_str(obj.get("user_id"), "user_id")

    lines_raw = obj.get("lines")
    if not isinstance(lines_raw, list) or not lines_raw:
        raise RequestError("lines must be a non-empty list")
    lines: list[OrderLine] = []
    for index, item in enumerate(lines_raw):
        line = _require_object(item, f"lines[{index}]")
        lines.append(
            OrderLine(
                sku=_require_str(line.get("sku"), f"lines[{index}].sku"),
                qty=_require_int(line.get("qty"), f"lines[{index}].qty", minimum=1),
                unit_price=_require_int(
                    line.get("unit_price"), f"lines[{index}].unit_price", minimum=0
                ),
            )
        )

    coupon_ids_raw = obj.get("coupon_ids")
    if not isinstance(coupon_ids_raw, list) or not coupon_ids_raw:
        raise RequestError("coupon_ids must be a non-empty list")
    coupon_ids: list[str] = []
    for index, coupon_id in enumerate(coupon_ids_raw):
        coupon_ids.append(_require_str(coupon_id, f"coupon_ids[{index}]"))

    return order_id, user_id, tuple(lines), tuple(coupon_ids)


def parse_cancel_content(raw: Any) -> tuple[str, str, str]:
    obj = _require_object(raw, "cancel request")
    cancel_id = _require_str(obj.get("cancel_id"), "cancel_id")
    order_id = _require_str(obj.get("order_id"), "order_id")
    user_id = _require_str(obj.get("user_id"), "user_id")
    return cancel_id, order_id, user_id


def unique_in_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def duplicates(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    repeated: list[str] = []
    for value in values:
        if value in seen and value not in repeated:
            repeated.append(value)
        seen.add(value)
    return repeated
