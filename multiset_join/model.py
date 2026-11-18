"""Tuple validation, signed-delta merging and canonical batch encoding.

Everything that defines "the same batch content" lives here so that the
catalog, the runner and the tests agree on one canonical form.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .errors import ValidationError

SOURCE_R = "R"
SOURCE_S = "S"
SOURCES = (SOURCE_R, SOURCE_S)

# Every stored count, delta and derived contribution must fit SQLite's signed
# 64-bit INTEGER.
MAX_COUNT = 2**63 - 1

# A complete source tuple plus its multiplicity (or signed delta).
TUPLE_FIELDS = {
    SOURCE_R: ("region", "sku", "quantity"),
    SOURCE_S: ("sku", "supplier", "price"),
}


def is_int(value: Any) -> bool:
    """bool is an int subclass in Python, but never a valid count here."""
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True, order=True)
class MergedChange:
    source: str
    tuple: tuple
    delta: int

    def as_dict(self) -> dict:
        return {"source": self.source, "tuple": list(self.tuple), "delta": self.delta}


@dataclass(frozen=True, order=True)
class SourceRow:
    source: str
    tuple: tuple
    multiplicity: int


def normalize_tuple(source: str, raw: Any) -> tuple:
    """Validate one complete source tuple and return it in canonical order."""
    if source not in SOURCES:
        raise ValidationError(f"unknown source {source!r}; expected 'R' or 'S'")
    if not isinstance(raw, (list, tuple)) or len(raw) != 3:
        raise ValidationError(
            f"{source} tuple must have exactly three fields "
            f"{TUPLE_FIELDS[source]}, got {raw!r}"
        )

    if source == SOURCE_R:
        region, sku, quantity = raw
        if not isinstance(region, str) or not isinstance(sku, str):
            raise ValidationError(f"R tuple region/sku must be strings, got {raw!r}")
        if not is_int(quantity) or quantity <= 0:
            raise ValidationError(f"R quantity must be a positive integer, got {quantity!r}")
        if quantity > MAX_COUNT:
            raise ValidationError(f"R quantity exceeds {MAX_COUNT}, got {quantity!r}")
        return (region, sku, quantity)

    sku, supplier, price = raw
    if not isinstance(sku, str) or not isinstance(supplier, str):
        raise ValidationError(f"S tuple sku/supplier must be strings, got {raw!r}")
    if not is_int(price) or price < 0:
        raise ValidationError(f"S price must be a non-negative integer, got {price!r}")
    if price > MAX_COUNT:
        raise ValidationError(f"S price exceeds {MAX_COUNT}, got {price!r}")
    return (sku, supplier, price)


def normalize_initial_rows(source: str, rows: Any) -> list[SourceRow]:
    """Merge equal complete tuples, drop zero counts, reject negative counts.

    Duplicates are *added*, never deduplicated to one.  Zero-multiplicity
    entries are not retained.  The source tuple (all three fields) is the
    identity; a multiplicity is a count attached to that identity.
    """
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise ValidationError(f"initial {source} rows must be a list")
    merged: dict[tuple, int] = {}
    for index, item in enumerate(rows):
        if not isinstance(item, dict):
            raise ValidationError(f"initial {source}[{index}] must be an object")
        try:
            raw_tuple = item["tuple"]
            multiplicity = item["multiplicity"]
        except KeyError as exc:
            raise ValidationError(
                f"initial {source}[{index}] needs 'tuple' and 'multiplicity'"
            ) from exc
        key = normalize_tuple(source, raw_tuple)
        if not is_int(multiplicity) or multiplicity < 0:
            raise ValidationError(
                f"initial {source}[{index}] multiplicity must be a non-negative "
                f"integer, got {multiplicity!r}"
            )
        merged[key] = merged.get(key, 0) + multiplicity
    return [
        SourceRow(source, key, count)
        for key, count in sorted(merged.items())
        if count > 0
    ]


def merge_batch(changes: Any) -> list[MergedChange]:
    """Merge a batch per (source, complete tuple) and drop zero deltas.

    Merging happens *before* the non-negativity check, so +1/-1 in either
    input order describes the same batch and must be accepted/rejected
    identically.
    """
    if not isinstance(changes, list):
        raise ValidationError("batch changes must be a list")
    merged: dict[tuple[str, tuple], int] = {}
    for index, change in enumerate(changes):
        if not isinstance(change, dict):
            raise ValidationError(f"change[{index}] must be an object")
        try:
            source = change["source"]
            raw_tuple = change["tuple"]
            delta = change["delta"]
        except KeyError as exc:
            raise ValidationError(
                f"change[{index}] needs 'source', 'tuple' and 'delta'"
            ) from exc
        if source not in SOURCES:
            raise ValidationError(
                f"change[{index}] source must be 'R' or 'S', got {source!r}"
            )
        key = normalize_tuple(source, raw_tuple)
        if not is_int(delta):
            raise ValidationError(
                f"change[{index}] delta must be a signed integer, got {delta!r}"
            )
        merged[(source, key)] = merged.get((source, key), 0) + delta
    return [
        MergedChange(source, key, delta)
        for (source, key), delta in sorted(merged.items())
        if delta != 0
    ]


def canonical_batch_json(changes: Iterable[MergedChange]) -> str:
    payload = {
        "changes": [
            {"source": c.source, "tuple": list(c.tuple), "delta": c.delta}
            for c in sorted(changes)
        ]
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def batch_digest(changes: Iterable[MergedChange]) -> str:
    return hashlib.sha256(canonical_batch_json(changes).encode("utf-8")).hexdigest()


def initial_canonical_json(r_rows: Sequence[SourceRow], s_rows: Sequence[SourceRow]) -> str:
    payload = {
        "R": [[list(row.tuple), row.multiplicity] for row in sorted(r_rows, key=lambda r: r.tuple)],
        "S": [[list(row.tuple), row.multiplicity] for row in sorted(s_rows, key=lambda r: r.tuple)],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def initial_digest(r_rows: Sequence[SourceRow], s_rows: Sequence[SourceRow]) -> str:
    return hashlib.sha256(initial_canonical_json(r_rows, s_rows).encode("utf-8")).hexdigest()


def slice_fingerprint(r_rows: Iterable[SourceRow], s_rows: Iterable[SourceRow]) -> str:
    """Stable digest of one materialised source slice.

    Both the catalog and the derived store use this so that a result can
    prove it was built from exactly the slice it recorded.
    """
    payload = [
        [row.source, list(row.tuple), row.multiplicity]
        for row in sorted((*r_rows, *s_rows))
    ]
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
