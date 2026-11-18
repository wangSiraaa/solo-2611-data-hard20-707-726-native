"""Marginal-band contract and the cumulative cost function F(x).

A contract is a list of half-open bands ``[lower, upper)`` with integer cent
rates.  Bands must start at 0, be contiguous, non-overlapping, and the last
band must have no upper bound.  ``F(x)`` is the sum of the first ``x`` units
priced through their own marginal bands:

    F(x) = sum over bands of unit_cents * overlap([0, x), [lower, upper))

A period fee is ``F(period_end_cumulative) - F(period_start_cumulative)`` and
is never "quantity times one band" nor "cumulative total at the top rate".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .errors import ContractError


@dataclass(frozen=True)
class Band:
    lower: int
    upper: int | None  # None means unbounded (last band only)
    unit_cents: int


@dataclass(frozen=True)
class Contract:
    contract_id: str
    revision: int
    bands: tuple[Band, ...]

    # -- construction -----------------------------------------------------
    @classmethod
    def from_dict(cls, payload: dict) -> "Contract":
        try:
            contract_id = payload["contract_id"]
            revision = payload["revision"]
            raw_bands = payload["bands"]
        except KeyError as exc:
            raise ContractError(f"contract missing field: {exc.args[0]}") from exc

        if not isinstance(contract_id, str) or not contract_id:
            raise ContractError("contract_id must be a non-empty string")
        if not _is_int(revision) or revision < 0:
            raise ContractError("revision must be a non-negative integer")
        if not isinstance(raw_bands, list) or not raw_bands:
            raise ContractError("bands must be a non-empty list")

        bands: list[Band] = []
        for i, raw in enumerate(raw_bands):
            bands.append(_parse_band(raw, i))

        _validate_band_chain(bands)
        return cls(contract_id=contract_id, revision=revision, bands=tuple(bands))

    def to_dict(self) -> dict:
        return {
            "contract_id": self.contract_id,
            "revision": self.revision,
            "bands": [
                {"lower": b.lower, "upper": b.upper, "unit_cents": b.unit_cents}
                for b in self.bands
            ],
        }

    # -- pricing ----------------------------------------------------------
    def cost(self, cumulative_units: int) -> int:
        """F(x): total cents for the first ``cumulative_units`` units."""
        if not _is_int(cumulative_units) or cumulative_units < 0:
            raise ContractError("cumulative units must be a non-negative integer")
        total = 0
        remaining = cumulative_units
        for band in self.bands:
            if remaining <= 0:
                break
            span = remaining if band.upper is None else min(remaining, band.upper - band.lower)
            total += span * band.unit_cents
            remaining -= span
        return total

    def marginal_breakdown(self, start: int, end: int) -> list[dict]:
        """Per-band contribution for the interval ``[start, end)`` of cumulative units."""
        if not (0 <= start <= end):
            raise ContractError("expected 0 <= start <= end")
        out = []
        for band in self.bands:
            lo = max(start, band.lower)
            hi = end if band.upper is None else min(end, band.upper)
            if hi > lo:
                out.append(
                    {
                        "lower": band.lower,
                        "upper": band.upper,
                        "unit_cents": band.unit_cents,
                        "units": hi - lo,
                        "cents": (hi - lo) * band.unit_cents,
                    }
                )
        return out

    def period_fee(self, start: int, end: int) -> int:
        return self.cost(end) - self.cost(start)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_band(raw: object, index: int) -> Band:
    if not isinstance(raw, dict):
        raise ContractError(f"band {index} must be an object")
    for field in ("lower", "upper", "unit_cents"):
        if field not in raw:
            raise ContractError(f"band {index} missing field {field}")
    lower = raw["lower"]
    upper = raw["upper"]
    unit_cents = raw["unit_cents"]

    if not _is_int(lower) or lower < 0:
        raise ContractError(f"band {index} lower must be a non-negative integer")
    if upper is not None and (not _is_int(upper) or upper <= lower):
        raise ContractError(f"band {index} upper must be null or an integer > lower")
    if not _is_int(unit_cents) or unit_cents < 0:
        raise ContractError(f"band {index} unit_cents must be a non-negative integer")
    return Band(lower=lower, upper=upper, unit_cents=unit_cents)


def _validate_band_chain(bands: Iterable[Band]) -> None:
    bands = list(bands)
    if bands[0].lower != 0:
        raise ContractError("first band must start at 0")
    for i, band in enumerate(bands):
        is_last = i == len(bands) - 1
        if is_last:
            if band.upper is not None:
                raise ContractError("last band must have no upper bound")
        else:
            nxt = bands[i + 1]
            if band.upper is None:
                raise ContractError(f"band {i} is unbounded but is not last")
            if band.upper != nxt.lower:
                raise ContractError(
                    f"bands {i} and {i + 1} are not contiguous: "
                    f"upper {band.upper} != lower {nxt.lower}"
                )
