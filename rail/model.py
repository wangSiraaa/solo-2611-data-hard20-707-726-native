"""Domains records, canonical content hashing and the pure composition model.

The physical layout is materialised every time a train's composition version
changes (prefix sums of car lengths are stored in ``train_cars``).  That keeps
end operations O(n) but platform scans O(k) over the cargo of the train, with no
floating point anywhere: lengths and ``local_position`` are positive rationals
kept as ``(numerator, denominator)`` integer pairs.

Only synthetic vehicles live here.  The service never talks to a real train.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from fractions import Fraction

ORIENTATIONS = (1, -1)


class ValidationError(ValueError):
    """Input is structurally invalid or violates a domain precondition."""


class ConflictError(Exception):
    """The same id was replayed with different content."""


class NotFoundError(LookupError):
    """A referenced record does not exist."""


class InactiveError(Exception):
    """A train that is not an active composition was referenced."""


class UnloadError(Exception):
    """A deferred unload operation was refused atomically."""


# ---------------------------------------------------------------------------
# JSON / Fraction helpers
# ---------------------------------------------------------------------------


def frac_to_json(value: Fraction | int) -> dict:
    """Canonical fraction object, e.g. 5/2 -> {"n": 5, "d": 2}."""
    f = Fraction(value)
    return {"n": f.numerator, "d": f.denominator}


def frac_from_json(obj) -> Fraction:
    if isinstance(obj, Fraction):
        return obj
    if isinstance(obj, int):
        return Fraction(obj)
    if not isinstance(obj, dict) or "n" not in obj or "d" not in obj:
        raise ValidationError(f"not a fraction object: {obj!r}")
    try:
        n, d = int(obj["n"]), int(obj["d"])
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"bad fraction object: {obj!r}") from exc
    if isinstance(obj["n"], bool) or isinstance(obj["d"], bool):
        raise ValidationError(f"bad fraction object: {obj!r}")
    if d <= 0:
        raise ValidationError(f"fraction denominator must be positive: {obj!r}")
    return Fraction(n, d)


def parse_positive_rational(value, what: str) -> Fraction:
    """Accept int, numeric string, {"n","d"} or Fraction; must be > 0."""
    if isinstance(value, bool):
        raise ValidationError(f"{what} must be a positive rational, got bool")
    try:
        if isinstance(value, (int, str, Fraction)):
            f = Fraction(value)
        else:
            f = frac_from_json(value)
    except (ValueError, ZeroDivisionError, TypeError) as exc:
        raise ValidationError(f"{what} must be a positive rational: {value!r}") from exc
    if f <= 0:
        raise ValidationError(f"{what} must be > 0: {value!r}")
    return f


def parse_int(value, what: str) -> int:
    if isinstance(value, bool):
        raise ValidationError(f"{what} must be an integer, got bool")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("+-").isdigit():
        return int(value)
    raise ValidationError(f"{what} must be an integer: {value!r}")


def parse_nonnegative_rational(value, what: str) -> Fraction:
    """Like ``parse_positive_rational`` but 0 is a valid point."""
    if isinstance(value, bool):
        raise ValidationError(f"{what} must be a rational, got bool")
    try:
        if isinstance(value, (int, str, Fraction)):
            f = Fraction(value)
        else:
            f = frac_from_json(value)
    except (ValueError, ZeroDivisionError, TypeError) as exc:
        raise ValidationError(f"{what} must be a rational: {value!r}") from exc
    if f < 0:
        raise ValidationError(f"{what} must be >= 0: {value!r}")
    return f


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(obj) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


def normalize_id(value, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{what} must be a non-empty string")
    return value


# ---------------------------------------------------------------------------
# Pure composition model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CarSpec:
    """Permanent identity of a car: id and length.  Never mutated."""

    car_id: str
    length: Fraction


@dataclass(frozen=True)
class CarPlacement:
    """One slot of an ordered composition: car, orientation, prefix offset."""

    car_id: str
    orientation: int
    start: Fraction
    length: Fraction

    @property
    def end(self) -> Fraction:
        return self.start + self.length

    def map_local(self, local_position: Fraction) -> Fraction:
        """Map a car-local position onto the train axis.

        +1: head-relative offset is ``local``; -1: ``length - local``.
        """
        if self.orientation == 1:
            return self.start + local_position
        return self.start + (self.length - local_position)

    def to_json(self) -> dict:
        return {
            "car_id": self.car_id,
            "orientation": self.orientation,
            "start": frac_to_json(self.start),
            "end": frac_to_json(self.end),
        }


@dataclass(frozen=True)
class Composition:
    """An ordered train: head-to-tail placements plus the total length."""

    placements: tuple[CarPlacement, ...]

    @property
    def length(self) -> Fraction:
        return self.placements[-1].end if self.placements else Fraction(0)

    def version(self) -> str:
        payload = {
            "cars": [
                {
                    "car_id": p.car_id,
                    "orientation": p.orientation,
                    "start": frac_to_json(p.start),
                    "end": frac_to_json(p.end),
                }
                for p in self.placements
            ],
            "length": frac_to_json(self.length),
        }
        return content_hash(payload)

    def by_car(self) -> dict[str, CarPlacement]:
        return {p.car_id: p for p in self.placements}


def build_composition(sequence: list[tuple[str, int]], specs: dict[str, CarSpec]) -> Composition:
    """Turn ``[(car_id, orientation), ...]`` into placements with prefix sums."""
    placements: list[CarPlacement] = []
    offset = Fraction(0)
    seen: set[str] = set()
    for car_id, orientation in sequence:
        if car_id in seen:
            raise ValidationError(f"car {car_id!r} appears twice in one composition")
        seen.add(car_id)
        spec = specs[car_id]
        if orientation not in ORIENTATIONS:
            raise ValidationError(f"orientation of {car_id!r} must be +1 or -1")
        placements.append(CarPlacement(car_id, orientation, offset, spec.length))
        offset += spec.length
    return Composition(tuple(placements))


def reverse_composition(sequence: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """Train reversal: reverse the car order AND flip every orientation."""
    return [(car_id, -orientation) for car_id, orientation in reversed(sequence)]


def reverse_placements(placements: tuple[CarPlacement, ...], specs: dict[str, CarSpec]) -> list[tuple[str, int]]:
    return reverse_composition([(p.car_id, p.orientation) for p in placements])


END_MAP = {
    ("tail", "head"): "target_then_other",
    ("tail", "tail"): "target_then_reverse_other",
    ("head", "tail"): "other_then_target",
    ("head", "head"): "reverse_other_then_target",
}


def couple_sequences(
    target: list[tuple[str, int]],
    other: list[tuple[str, int]],
    target_end: str,
    other_end: str,
) -> tuple[list[tuple[str, int]], str]:
    """Join ``other`` onto ``target`` keeping target's own head-to-tail frame.

    Returns the new sequence and the layout rule that was applied.
    """
    if target_end not in ("head", "tail") or other_end not in ("head", "tail"):
        raise ValidationError("ends must be 'head' or 'tail'")
    rule = END_MAP[(target_end, other_end)]
    if rule == "target_then_other":
        seq = list(target) + list(other)
    elif rule == "target_then_reverse_other":
        seq = list(target) + reverse_composition(other)
    elif rule == "other_then_target":
        seq = list(other) + list(target)
    else:  # reverse_other_then_target
        seq = reverse_composition(other) + list(target)
    ids = [c for c, _ in seq]
    if len(ids) != len(set(ids)):
        raise ValidationError("couple would place the same car in the train twice")
    return seq, rule


def split_sequence(
    sequence: list[tuple[str, int]],
    end: str,
    n: int,
) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """Remove ``n`` cars from ``end``; both pieces keep the pre-split order."""
    if end not in ("head", "tail"):
        raise ValidationError("split end must be 'head' or 'tail'")
    if n <= 0:
        raise ValidationError("split n must be a positive integer")
    if n >= len(sequence):
        raise ValidationError(
            f"split n={n} must be smaller than the total car count {len(sequence)}"
        )
    if end == "head":
        return list(sequence[:n]), list(sequence[n:])
    return list(sequence[-n:]), list(sequence[:-n])
