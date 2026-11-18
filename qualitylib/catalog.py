"""Specifications and measurements: parse, validate and persist.

A *specification* fixes the quantity, the category (absolute vs difference),
the target unit and an inclusive ``[lo, hi]`` interval.  A *measurement* gives
a sample, the specification it is judged against, the same quantity and
category, and an exact value in its own unit.

This module never contains a conversion table: unit arithmetic lives only in
:mod:`qualitylib.directory`.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .edge import KINDS
from .errors import ValidationError
from .rational import parse_ratio, ratio_json


@dataclass(frozen=True)
class Specification:
    spec_id: str
    quantity_id: str
    kind: str
    unit_id: str
    lo: Fraction
    hi: Fraction

    def contains(self, value: Fraction) -> bool:
        return self.lo <= value <= self.hi

    def to_json(self) -> dict:
        return {
            "spec_id": self.spec_id,
            "quantity_id": self.quantity_id,
            "kind": self.kind,
            "unit_id": self.unit_id,
            "lo": ratio_json(self.lo),
            "hi": ratio_json(self.hi),
        }


@dataclass(frozen=True)
class Measurement:
    measurement_id: str
    sample_id: str | None
    spec_id: str
    quantity_id: str
    kind: str
    unit_id: str
    value: Fraction

    def to_json(self) -> dict:
        return {
            "measurement_id": self.measurement_id,
            "sample_id": self.sample_id,
            "spec_id": self.spec_id,
            "quantity_id": self.quantity_id,
            "kind": self.kind,
            "unit_id": self.unit_id,
            "value": ratio_json(self.value),
        }


def _require_id(obj: dict, key: str, where: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{where}.{key} must be a non-empty string")
    return value


def parse_specification(obj: dict, where: str = "specification") -> Specification:
    if not isinstance(obj, dict):
        raise ValidationError(f"{where} must be an object")
    spec_id = _require_id(obj, "spec_id", where)
    quantity_id = _require_id(obj, "quantity_id", where)
    unit_id = _require_id(obj, "unit_id", where)
    kind = obj.get("kind")
    if kind not in KINDS:
        raise ValidationError(f"{where}.kind must be one of {KINDS}")
    try:
        lo = parse_ratio(obj.get("lo"), key=f"{where}.lo")
        hi = parse_ratio(obj.get("hi"), key=f"{where}.hi")
    except ValueError as exc:
        raise ValidationError(f"{where}: {exc}") from exc
    if lo > hi:
        raise ValidationError(f"{where}: lo must not exceed hi")
    return Specification(spec_id, quantity_id, kind, unit_id, lo, hi)


def parse_measurement(obj: dict, where: str = "measurement") -> Measurement:
    if not isinstance(obj, dict):
        raise ValidationError(f"{where} must be an object")
    measurement_id = _require_id(obj, "measurement_id", where)
    spec_id = _require_id(obj, "spec_id", where)
    quantity_id = _require_id(obj, "quantity_id", where)
    unit_id = _require_id(obj, "unit_id", where)
    kind = obj.get("kind")
    if kind not in KINDS:
        raise ValidationError(f"{where}.kind must be one of {KINDS}")
    sample_id = obj.get("sample_id")
    if sample_id is not None and (not isinstance(sample_id, str) or not sample_id):
        raise ValidationError(f"{where}.sample_id must be a non-empty string if given")
    try:
        value = parse_ratio(obj.get("value"), key=f"{where}.value")
    except ValueError as exc:
        raise ValidationError(f"{where}: {exc}") from exc
    return Measurement(
        measurement_id, sample_id, spec_id, quantity_id, kind, unit_id, value
    )


def parse_catalog(obj: dict) -> tuple[list[Specification], list[Measurement]]:
    if not isinstance(obj, dict):
        raise ValidationError("catalog must be an object")
    raw_specs = obj.get("specifications")
    raw_measurements = obj.get("measurements")
    if not isinstance(raw_specs, list):
        raise ValidationError("catalog.specifications must be a list")
    if not isinstance(raw_measurements, list):
        raise ValidationError("catalog.measurements must be a list")
    specs: list[Specification] = []
    seen_specs: set[str] = set()
    for index, raw in enumerate(raw_specs):
        spec = parse_specification(raw, where=f"specifications[{index}]")
        if spec.spec_id in seen_specs:
            raise ValidationError(f"duplicate spec_id {spec.spec_id!r}")
        seen_specs.add(spec.spec_id)
        specs.append(spec)
    measurements: list[Measurement] = []
    seen_measurements: set[str] = set()
    for index, raw in enumerate(raw_measurements):
        measurement = parse_measurement(raw, where=f"measurements[{index}]")
        if measurement.measurement_id in seen_measurements:
            raise ValidationError(
                f"duplicate measurement_id {measurement.measurement_id!r}"
            )
        seen_measurements.add(measurement.measurement_id)
        if measurement.spec_id not in seen_specs:
            raise ValidationError(
                f"measurement {measurement.measurement_id!r} references "
                f"unknown spec {measurement.spec_id!r}"
            )
        measurements.append(measurement)
    return specs, measurements
