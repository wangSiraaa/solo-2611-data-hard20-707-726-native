"""Exact rational parsing/serialisation and closed-interval algebra.

Every measurement quantity in this package is a :class:`fractions.Fraction`.
Floats are deliberately rejected as inputs: accepting them would silently
reintroduce binary rounding into a problem whose whole point is exactness.
Decimals expressed as strings (``"0.75"``) are fine -- ``Fraction`` parses
them exactly.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Iterable, Sequence

Interval = tuple[Fraction, Fraction]  # inclusive on both ends


class RationalError(ValueError):
    """Raised when a value cannot be interpreted as an exact rational."""


def parse_rational(value, *, where: str = "value") -> Fraction:
    """Parse ``value`` as an exact :class:`Fraction`.

    Accepts ``int``, ``Fraction`` and non-empty strings such as ``"-3"``,
    ``"3/4"`` or ``"0.75"``.  ``bool`` and ``float`` are rejected: the former
    is an ``int`` subclass and never meaningful here, the latter is inexact.
    """
    if isinstance(value, bool):
        raise RationalError(f"{where}: booleans are not rationals")
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float):
        raise RationalError(
            f"{where}: floats are not accepted; pass an int or a fraction string"
        )
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise RationalError(f"{where}: empty rational string")
        try:
            return Fraction(text)
        except (ValueError, ZeroDivisionError) as exc:
            raise RationalError(f"{where}: cannot parse {value!r} as a rational: {exc}") from exc
    raise RationalError(f"{where}: unsupported rational type {type(value).__name__}")


def fmt(value: Fraction) -> str:
    """Canonical exact text for a rational, always ``n/d``."""
    value = Fraction(value)
    return f"{value.numerator}/{value.denominator}"


def parse_interval(value, *, where: str = "interval") -> Interval:
    """Parse a two-element ``[lo, hi]`` sequence of rationals, checking lo<=hi."""
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise RationalError(f"{where}: expected a two-element [lo, hi] sequence")
    if len(value) != 2:
        raise RationalError(f"{where}: expected exactly two endpoints, got {len(value)}")
    lo = parse_rational(value[0], where=f"{where}[0]")
    hi = parse_rational(value[1], where=f"{where}[1]")
    if lo > hi:
        raise RationalError(f"{where}: lower endpoint {fmt(lo)} exceeds upper {fmt(hi)}")
    return lo, hi


# ---------------------------------------------------------------------------
# Closed-interval algebra.  All lists are sorted, disjoint and non-touching
# (touching closed intervals are merged because their union is again closed).
# ---------------------------------------------------------------------------


def normalize(intervals: Iterable[Interval]) -> list[Interval]:
    """Sort and merge a collection of closed intervals into a canonical union."""
    materialised = sorted((Fraction(lo), Fraction(hi)) for lo, hi in intervals)
    merged: list[Interval] = []
    for lo, hi in materialised:
        if lo > hi:  # empty, ignore
            continue
        if merged and lo <= merged[-1][1]:
            prev_lo, prev_hi = merged[-1]
            if hi > prev_hi:
                merged[-1] = (prev_lo, hi)
        else:
            merged.append((lo, hi))
    return merged


def intersection(left: Sequence[Interval], right: Sequence[Interval]) -> list[Interval]:
    """Closed intersection of two canonical (or arbitrary) interval unions."""
    a = normalize(left)
    b = normalize(right)
    out: list[Interval] = []
    i = j = 0
    while i < len(a) and j < len(b):
        lo = max(a[i][0], b[j][0])
        hi = min(a[i][1], b[j][1])
        if lo <= hi:
            out.append((lo, hi))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return normalize(out)


def contains(intervals: Sequence[Interval], point: Fraction) -> bool:
    """Membership in a closed interval union."""
    point = Fraction(point)
    for lo, hi in normalize(intervals):
        if lo <= point <= hi:
            return True
        if point < lo:
            break
    return False


def is_empty(intervals: Sequence[Interval]) -> bool:
    return len(normalize(intervals)) == 0


def as_json(intervals: Sequence[Interval]) -> list[list[str]]:
    return [[fmt(lo), fmt(hi)] for lo, hi in normalize(intervals)]
