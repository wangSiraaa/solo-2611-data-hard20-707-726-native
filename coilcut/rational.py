"""Exact rational helpers.

Lengths may be integers or exact rationals.  SQLite stores the canonical
``numerator/denominator`` string; JSON output uses an ``int`` when the value is
integral and a ``"numerator/denominator"`` string otherwise so no precision is
lost.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from typing import Any


def as_fraction(value: Any, *, field: str = "value") -> Fraction:
    if isinstance(value, bool):
        raise TypeError(f"{field} must be a number, not bool")
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, Decimal):
        return Fraction(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"{field} is empty")
        try:
            return Fraction(text)
        except (ValueError, ZeroDivisionError) as exc:
            raise ValueError(f"{field} is not an exact number: {value!r}") from exc
    if isinstance(value, float):
        # Avoid exposing binary float noise: Fraction("0.1") is exactly 1/10.
        return Fraction(Decimal(str(value)))
    raise TypeError(f"{field} has unsupported type {type(value).__name__}")


def fstr(value: Any) -> str:
    """Canonical exact string used in SQLite and request fingerprints."""
    frac = as_fraction(value)
    return f"{frac.numerator}/{frac.denominator}"


def fparse(text: str) -> Fraction:
    return Fraction(text)


def jscalar(value: Any) -> Any:
    """JSON-friendly exact scalar: int when integral, ``p/q`` string otherwise."""
    frac = as_fraction(value)
    if frac.denominator == 1:
        return frac.numerator
    return f"{frac.numerator}/{frac.denominator}"
