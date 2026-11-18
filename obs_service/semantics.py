"""Exact continuous-range semantics over the non-negative reals.

Representation
--------------
A non-empty set of admissible values is a :class:`Range` ``[lo, hi]`` with
open/closed flags and explicit unbounded markers:

* ``lo is None``  -> lower bound is 0, closed (only because values are
  restricted to the non-negative reals).
* ``hi is None``  -> no finite upper bound (``+inf`` contribution).
* ``lo == hi`` and both ends closed -> the singleton ``{lo}``.
* The empty set is **not** representable as a :class:`Range`; it is only
  ever produced by intersection (``None``) or by averaging over no samples.

No floats are used anywhere: endpoints are :class:`fractions.Fraction`.
A rendered form such as ``[4, 17/3)`` is exact, and, in contrast to a
midpoint or a finite grid, retains knots you cannot recompute exactly
afterwards (``+inf``, open vs closed ends).

NOTATION: ``MISSING`` is deliberately **not** a Range.  It is "no
observation", which is distinct from every measured set, including the
closed singleton ``{0}`` (``LE(0)``) and the empty set (which is not an
observation at all).
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Optional

POS_INF = "INF"

KINDS = ("EXACT", "LT", "LE", "GT", "GE", "BETWEEN", "MISSING")


class SemanticsError(ValueError):
    """An observation/threshold is malformed or denotes an empty range."""


class RevConflict(SemanticsError):
    """The same (sample, project, revision) was registered with different content."""


def parse_rational(value, *, what: str = "endpoint") -> Fraction:
    """Parse a non-negative rational given as int or fraction string.

    Accepts JSON integers, numeric strings such as ``"5"``/``"10/3"`` and
    ``p/q`` forms.  Floats are rejected: binary floating point cannot
    represent every rational exactly.
    """
    if isinstance(value, bool):
        raise SemanticsError(f"{what}: boolean is not a rational")
    if isinstance(value, int):
        result = Fraction(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise SemanticsError(f"{what}: empty string")
        try:
            result = Fraction(text)
        except (ValueError, ZeroDivisionError):
            raise SemanticsError(f"{what}: cannot parse rational {value!r}") from None
    else:
        raise SemanticsError(
            f"{what}: unsupported JSON number type {type(value).__name__}; "
            "use an integer or a fraction string (floats are not exact)"
        )
    if result < 0:
        raise SemanticsError(f"{what}: negative value {result} not allowed")
    return result


@dataclass(frozen=True)
class Range:
    """A non-empty continuous interval in [0, +inf].  See module docs."""

    lo: Optional[Fraction]
    lo_closed: bool
    hi: Optional[Fraction]
    hi_closed: bool

    def __post_init__(self) -> None:
        if self.lo is not None and self.lo < 0:
            raise SemanticsError("negative lower endpoint")
        if self.hi is not None and self.hi < 0:
            raise SemanticsError("negative upper endpoint")
        if self.lo is not None and self.hi is not None:
            if self.lo > self.hi:
                raise SemanticsError("lower endpoint exceeds upper endpoint")

    # -- constructors ---------------------------------------------------
    @staticmethod
    def singleton(x: Fraction) -> "Range":
        return Range(x, True, x, True)

    @staticmethod
    def between(l: Fraction, u: Fraction, lo_closed: bool, hi_closed: bool) -> "Range":
        if l > u:
            raise SemanticsError("BETWEEN lower endpoint exceeds upper endpoint")
        if l == u and not (lo_closed and hi_closed):
            raise SemanticsError(
                "empty observation: equal endpoints require both ends closed"
            )
        return Range(l, lo_closed, u, hi_closed)

    @staticmethod
    def lt(u: Fraction) -> "Range":
        if u <= 0:
            raise SemanticsError("empty observation: LT(0) has no non-negative value")
        return Range(None, True, u, False)

    @staticmethod
    def le(u: Fraction) -> "Range":
        # U = 0 gives the singleton {0}; U > 0 gives [0, U].
        if u == 0:
            return Range.singleton(Fraction(0))
        return Range(None, True, u, True)

    @staticmethod
    def gt(l: Fraction) -> "Range":
        return Range(l, False, None, False)

    @staticmethod
    def ge(l: Fraction) -> "Range":
        return Range(l, True, None, False)

    # -- core algebra ---------------------------------------------------
    def is_singleton(self) -> bool:
        return self.lo is not None and self.hi is not None and self.lo == self.hi

    def contains(self, x: Fraction) -> bool:
        if x < 0:
            return False
        if self.lo is not None:
            if x < self.lo or (x == self.lo and not self.lo_closed):
                return False
        if self.hi is not None:
            if x > self.hi or (x == self.hi and not self.hi_closed):
                return False
        return True

    def intersect(self, other: "Range") -> Optional["Range"]:
        """Return the intersection, or ``None`` when it is empty."""
        # Lower end.
        if self.lo is None:
            lo, closed = other.lo, other.lo_closed
        elif other.lo is None:
            lo, closed = self.lo, self.lo_closed
        elif self.lo > other.lo:
            lo, closed = self.lo, self.lo_closed
        elif other.lo > self.lo:
            lo, closed = other.lo, other.lo_closed
        else:
            lo, closed = self.lo, self.lo_closed and other.lo_closed
        # Upper end.
        if self.hi is None:
            hi, hc = other.hi, other.hi_closed
        elif other.hi is None:
            hi, hc = self.hi, self.hi_closed
        elif self.hi < other.hi:
            hi, hc = self.hi, self.hi_closed
        elif other.hi < self.hi:
            hi, hc = other.hi, other.hi_closed
        else:
            hi, hc = self.hi, self.hi_closed and other.hi_closed
        # Emptiness test.
        if lo is not None and hi is not None:
            if lo > hi:
                return None
            if lo == hi and not (closed and hc):
                return None
        return Range(lo, closed, hi, hc)

    def __repr__(self) -> str:  # compact but unambiguous
        left = "0" if self.lo is None else format_fraction(self.lo)
        right = "+inf" if self.hi is None else format_fraction(self.hi)
        return f"{'[' if self.lo_closed else '('}{left},{right}{']' if self.hi_closed else ')'}"

    def to_dict(self) -> dict:
        return {
            "lo": None if self.lo is None else str(self.lo),
            "lo_closed": self.lo_closed,
            "hi": None if self.hi is None else str(self.hi),
            "hi_closed": self.hi_closed,
            "display": repr(self),
        }


def format_fraction(x: Fraction) -> str:
    """Render an exact rational as ``n`` or ``n/d`` (no decimals)."""
    if x.denominator == 1:
        return str(x.numerator)
    return f"{x.numerator}/{x.denominator}"


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Observation:
    """A parsed observation: either a MISSING marker or a non-empty Range."""

    kind: str
    range: Optional[Range]

    @property
    def is_missing(self) -> bool:
        return self.kind == "MISSING"

    def canonical(self) -> dict:
        if self.is_missing:
            return {"kind": "MISSING"}
        r = self.range
        return {
            "kind": "RANGE",
            "lo": None if r.lo is None else str(r.lo),
            "lo_closed": r.lo_closed,
            "hi": None if r.hi is None else str(r.hi),
            "hi_closed": r.hi_closed,
        }

    def __repr__(self) -> str:
        return "MISSING" if self.is_missing else repr(self.range)


def parse_observation(obj: dict) -> Observation:
    """Parse a JSON observation object; reject empty ranges with SemanticsError."""
    if not isinstance(obj, dict):
        raise SemanticsError("observation must be an object")
    kind = obj.get("kind")
    if kind == "MISSING":
        for key in ("value", "lower", "upper", "lower_closed", "upper_closed"):
            if key in obj:
                raise SemanticsError("MISSING must not carry endpoints")
        return Observation("MISSING", None)
    if kind in ("EXACT", "LT", "LE", "GT", "GE"):
        endpoint_key = {
            "EXACT": "value",
            "LT": "upper",
            "LE": "upper",
            "GT": "lower",
            "GE": "lower",
        }[kind]
        if endpoint_key not in obj:
            raise SemanticsError(f"{kind} requires {endpoint_key!r}")
        endpoint = parse_rational(obj[endpoint_key], what=f"{kind}.{endpoint_key}")
        if kind == "EXACT":
            rng = Range.singleton(endpoint)
        elif kind == "LT":
            rng = Range.lt(endpoint)
        elif kind == "LE":
            rng = Range.le(endpoint)
        elif kind == "GT":
            rng = Range.gt(endpoint)
        else:
            rng = Range.ge(endpoint)
        return Observation(kind, rng)
    if kind == "BETWEEN":
        if "lower" not in obj or "upper" not in obj:
            raise SemanticsError("BETWEEN requires lower and upper")
        l = parse_rational(obj["lower"], what="BETWEEN.lower")
        u = parse_rational(obj["upper"], what="BETWEEN.upper")
        lc = obj.get("lower_closed", True)
        uc = obj.get("upper_closed", True)
        if not isinstance(lc, bool) or not isinstance(uc, bool):
            raise SemanticsError("BETWEEN closure flags must be booleans")
        return Observation(kind, Range.between(l, u, lc, uc))
    raise SemanticsError(f"unknown observation kind {kind!r}")


def parse_threshold(value, *, revision=None) -> Fraction:
    """Parse a threshold value (non-negative rational)."""
    return parse_rational(value, what=f"threshold rev {revision}" if revision else "threshold")


# ---------------------------------------------------------------------------
# Threshold predicate  x > T
# ---------------------------------------------------------------------------

# status codes returned by threshold_status
TRUE = "true"        # x > T for every value in the range
FALSE = "false"      # x > T for no value in the range
UNCERTAIN = "uncertain"  # both satisfiable and falsifiable inside the range


def threshold_status(rng: Range, threshold: Fraction) -> str:
    """Classify ``x > T`` over a non-empty continuous range.

    A range above T exists iff ``rng ∩ (T, +inf) != ∅``; a range at/below T
    exists iff ``rng ∩ [0, T] != ∅``.  Using the exact intersection primitive
    keeps this honest for continuous sets (no sampling).
    """
    if rng is None:
        raise SemanticsError("threshold predicate needs a non-empty range")
    above = rng.intersect(Range(threshold, False, None, False))   # (T, +inf)
    at_or_below = rng.intersect(Range(None, True, threshold, True))  # [0, T]
    if above is not None and at_or_below is None:
        return TRUE
    if above is None and at_or_below is not None:
        return FALSE
    if above is not None and at_or_below is not None:
        return UNCERTAIN
    # Cannot happen for a non-empty range and a valid threshold.
    raise SemanticsError("internal: range neither exceeds nor meets threshold")


# ---------------------------------------------------------------------------
# Average of non-MISSING ranges
# ---------------------------------------------------------------------------

def average_of_ranges(ranges: list) -> Optional[dict]:
    """Arithmetic mean of the given (non-MISSING) ranges, analysed exactly.

    The mean of independent per-sample ranges is ``[sum lo_i / n, sum hi_i / n]``
    with the same open/closed flags at each end; an unbounded sample makes the
    upper bound ``+inf``.  Returns ``None`` when there are no samples
    (the mean is then *undefined*).

    Keys: n, lo, lo_closed, hi, hi_closed, lower_attainable,
    upper_attainable, upper_unbounded.  The infimum/supremum of the mean box
    are attained iff the corresponding finite endpoint is included; otherwise
    they are limits only.
    """
    n = len(ranges)
    if n == 0:
        return None
    for r in ranges:
        if r is None:
            raise SemanticsError("average_of_ranges received an empty range")
    denom = Fraction(n)
    lo_c = all(r.lo_closed for r in ranges)
    # All ranges have lo == 0 when lo is None; Fraction(0) is the lower sum.
    lo = sum((r.lo if r.lo is not None else Fraction(0) for r in ranges), Fraction(0)) / denom
    if any(r.hi is None for r in ranges):
        hi = None
        hi_c = False
    else:
        hi = sum((r.hi for r in ranges), Fraction(0)) / denom
        hi_c = all(r.hi_closed for r in ranges)
    # The mean is a continuous box; its infimum/supremum are attained iff the
    # corresponding endpoint is finite and included.  (No assumption that the
    # mean is a singleton: a non-degenerate range is attainable at its ends.)
    lower_attainable = lo_c
    upper_attainable = (hi is not None) and hi_c
    return {
        "n": n,
        "lo": lo,
        "lo_closed": lo_c,
        "hi": None if hi is None else hi,
        "hi_closed": False if hi is None else hi_c,
        "lower_attainable": lower_attainable,
        "upper_attainable": upper_attainable,
        "upper_unbounded": hi is None,
    }
