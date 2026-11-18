"""Directed affine edges between units of the same quantity.

An edge ``from u to v`` with ``y = a*x + b`` means: a value ``x`` expressed in
``u`` converts to ``y`` expressed in ``v``.  ``a`` is a positive rational,
``b`` any rational.  The reverse direction is derived exactly:

    x = (y - b) / a = (1/a) * y + (-b/a)

Absolute quantities use both ``a`` and ``b``; differences use ``a`` only (the
zero point is irrelevant for a span).  Categories are never mixed.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .errors import ValidationError
from .rational import parse_ratio, ratio_json

KIND_ABSOLUTE = "absolute"
KIND_DIFFERENCE = "difference"
KINDS = (KIND_ABSOLUTE, KIND_DIFFERENCE)


@dataclass(frozen=True)
class Affine:
    """Exact directed affine map ``y = a*x + b``."""

    a: Fraction
    b: Fraction

    @property
    def reverse(self) -> "Affine":
        # x = y/a - b/a
        return Affine(1 / self.a, -self.b / self.a)

    def apply_absolute(self, x: Fraction) -> Fraction:
        return self.a * x + self.b

    def apply_difference(self, x: Fraction) -> Fraction:
        return self.a * x

    def compose_into(self, inner: "Affine") -> "Affine":
        """Return the map ``self ∘ inner`` (apply ``inner`` then ``self``)."""
        return Affine(self.a * inner.a, self.a * inner.b + self.b)

    def normalize(self, kind: str) -> "Affine":
        """Zero the offset for differences (category-specific interpretation)."""
        if kind == KIND_DIFFERENCE:
            return Affine(self.a, Fraction(0))
        if kind == KIND_ABSOLUTE:
            return self
        raise ValidationError(f"unknown kind {kind!r}")

    def to_json(self) -> dict:
        return {"a": ratio_json(self.a), "b": ratio_json(self.b)}


@dataclass(frozen=True)
class Edge:
    edge_id: str
    from_unit: str
    to_unit: str
    a: Fraction
    b: Fraction

    @property
    def affine(self) -> Affine:
        return Affine(self.a, self.b)

    def to_json(self) -> dict:
        return {
            "edge_id": self.edge_id,
            "from": self.from_unit,
            "to": self.to_unit,
            "a": ratio_json(self.a),
            "b": ratio_json(self.b),
        }


def _require_str(obj: dict, key: str, where: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{where}.{key} must be a non-empty string")
    return value


def edge_from_json(obj: dict, where: str = "edge") -> Edge:
    """Parse and validate a single edge object."""
    if not isinstance(obj, dict):
        raise ValidationError(f"{where} must be an object")
    edge_id = _require_str(obj, "edge_id", where)
    from_unit = _require_str(obj, "from", where)
    to_unit = _require_str(obj, "to", where)
    try:
        a = parse_ratio(obj.get("a"), key=f"{where}.a")
        b = parse_ratio(obj.get("b"), key=f"{where}.b")
    except ValueError as exc:
        raise ValidationError(f"{where}: {exc}") from exc
    if a <= 0:
        raise ValidationError(f"{where}.a must be strictly positive")
    return Edge(edge_id, from_unit, to_unit, a, b)
