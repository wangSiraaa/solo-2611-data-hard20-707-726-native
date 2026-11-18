"""Exact rational helpers.

All calibration numbers, measurements, limits and converted values are exact
:class:`fractions.Fraction` values.  Consistent edge sets are checked by exact
cross-multiplication; no binary floating point is ever used for correctness.
"""
from __future__ import annotations

import json
from fractions import Fraction


def fraction(value) -> Fraction:
    """Coerce an integer / Fraction into an exact Fraction (never a float)."""
    if isinstance(value, Fraction):
        return value
    if isinstance(value, bool):  # bool is an int subclass; reject it loudly
        raise ValueError("boolean is not a rational value")
    if isinstance(value, int):
        return Fraction(value)
    raise TypeError(f"expected int or Fraction, got {type(value).__name__}")


def parse_ratio(obj, *, key: str = "value", allow_nonpositive_denom: bool = False) -> Fraction:
    """Parse ``{"n": int, "d": positive int}`` into an exact Fraction."""
    if not isinstance(obj, dict):
        raise ValueError(f"{key}: expected an object with integer n and d")
    if set(obj) != {"n", "d"}:
        raise ValueError(f"{key}: expected exactly keys 'n' and 'd'")
    n, d = obj["n"], obj["d"]
    if isinstance(n, bool) or not isinstance(n, int):
        raise ValueError(f"{key}.n must be an integer")
    if isinstance(d, bool) or not isinstance(d, int):
        raise ValueError(f"{key}.d must be an integer")
    if d == 0:
        raise ValueError(f"{key}.d must be non-zero")
    if d < 0 and not allow_nonpositive_denom:
        raise ValueError(f"{key}.d must be positive")
    return Fraction(n, d)


def ratio_json(value: Fraction) -> dict:
    """Serialise an exact Fraction as a reduced ``{"n", "d"}`` with ``d > 0``."""
    f = Fraction(value)  # normalises sign onto the numerator
    return {"n": f.numerator, "d": f.denominator}


def format_fraction(value) -> str:
    f = Fraction(value)
    return str(f.numerator) if f.denominator == 1 else f"{f.numerator}/{f.denominator}"


def parse_ratio_text(text: str) -> Fraction:
    """Parse ``"9/5"`` or ``"-5463/20"`` (used to read our own DB / CLI)."""
    text = str(text).strip()
    if "/" in text:
        n, d = text.split("/", 1)
        return Fraction(int(n), int(d))
    return Fraction(int(text))


def ratio_text(value: Fraction) -> str:
    f = Fraction(value)
    return f"{f.numerator}/{f.denominator}"


def canonical_json(obj) -> str:
    """Deterministic JSON used for hashing and snapshot storage."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
