"""Adjudication: turn a measurement + spec into a versioned evaluation.

A measurement is only *eligible* to pass when:

* its quantity and category match the specification exactly;
* both units really belong to that quantity and category in the directory;
* a conversion path exists inside one connected component.

Missing conversions and category errors never yield a qualifying record.
Converted values, paths and the spec/measurement snapshots are captured at
evaluation time, so a later revision cannot rewrite history.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from fractions import Fraction

from .catalog import Measurement, Specification
from .directory import Conversion, Directory
from .errors import ConversionError, KindMismatch, UnknownUnit
from .rational import ratio_json


STATUS_EVALUATED = "evaluated"
STATUS_REJECTED = "rejected"


@dataclass(frozen=True)
class Evaluation:
    evaluation_id: str
    measurement_id: str
    sample_id: str | None
    spec_id: str
    directory_revision: str
    directory_fingerprint: str
    quantity_id: str
    kind: str
    status: str
    passed: bool
    converted_unit: str | None
    converted_value: Fraction | None
    reason: str | None
    conversion: Conversion | None
    specification: dict
    measurement: dict
    created_at: str

    @property
    def convertible(self) -> bool:
        return self.status == STATUS_EVALUATED

    def to_row(self) -> dict:
        return {
            "evaluation_id": self.evaluation_id,
            "measurement_id": self.measurement_id,
            "sample_id": self.sample_id,
            "spec_id": self.spec_id,
            "directory_revision": self.directory_revision,
            "directory_fingerprint": self.directory_fingerprint,
            "quantity_id": self.quantity_id,
            "kind": self.kind,
            "status": self.status,
            "converted_unit": self.converted_unit,
            "converted_value": (
                ratio_json(self.converted_value)
                if self.converted_value is not None
                else None
            ),
            "passed": self.passed,
            "reason": self.reason,
            "path": self.conversion.path_json() if self.conversion else [],
            "conversion": self.conversion.affine.to_json() if self.conversion else None,
            "specification": self.specification,
            "measurement": self.measurement,
            "created_at": self.created_at,
        }

    def to_report_entry(self) -> dict:
        """Report row preserving the report_reader.py contract.

        ``converted_value`` is present for every entry in ``evaluations``; a
        rejected measurement is reported through the separate ``rejections``
        channel of the report so the reader contract stays intact.
        """
        entry = {
            "evaluation_id": self.evaluation_id,
            "measurement_id": self.measurement_id,
            "directory_revision": self.directory_revision,
            "converted_value": ratio_json(self.converted_value),
            "passed": self.passed,
            # --- extensible evidence -------------------------------------
            "sample_id": self.sample_id,
            "spec_id": self.spec_id,
            "quantity_id": self.quantity_id,
            "kind": self.kind,
            "converted_unit": self.converted_unit,
            "status": self.status,
            "directory_fingerprint": self.directory_fingerprint,
            "path": self.conversion.path_json() if self.conversion else [],
            "conversion": self.conversion.affine.to_json() if self.conversion else None,
            "specification": self.specification,
            "measurement": self.measurement,
        }
        return entry

    def to_rejection_entry(self) -> dict:
        return {
            "evaluation_id": self.evaluation_id,
            "measurement_id": self.measurement_id,
            "directory_revision": self.directory_revision,
            "status": self.status,
            "reason": self.reason,
            "sample_id": self.sample_id,
            "spec_id": self.spec_id,
            "quantity_id": self.quantity_id,
            "kind": self.kind,
            "passed": False,
            "specification": self.specification,
            "measurement": self.measurement,
        }


def new_evaluation_id(measurement_id: str, revision: str) -> str:
    token = uuid.uuid4().hex[:12]
    return f"EV-{measurement_id}-{revision}-{token}"


def evaluate(
    directory: Directory,
    spec: Specification,
    measurement: Measurement,
    *,
    created_at: str,
) -> Evaluation:
    """Evaluate one measurement against one specification.

    Never raises for a bad measurement: the failure is captured as a rejected
    evaluation.  Programming errors (wrong types) still propagate.
    """
    base = dict(
        evaluation_id=new_evaluation_id(measurement.measurement_id, directory.revision),
        measurement_id=measurement.measurement_id,
        sample_id=measurement.sample_id,
        spec_id=spec.spec_id,
        directory_revision=directory.revision,
        directory_fingerprint=directory.fingerprint,
        quantity_id=measurement.quantity_id,
        kind=measurement.kind,
        specification=spec.to_json(),
        measurement=measurement.to_json(),
        created_at=created_at,
    )

    def reject(reason: str) -> Evaluation:
        return Evaluation(
            status=STATUS_REJECTED,
            passed=False,
            converted_unit=None,
            converted_value=None,
            reason=reason,
            conversion=None,
            **base,
        )

    # 1. quantity / category must line up across spec and measurement.
    if measurement.quantity_id != spec.quantity_id:
        return reject(
            f"measurement quantity {measurement.quantity_id!r} != "
            f"spec quantity {spec.quantity_id!r}"
        )
    if measurement.kind != spec.kind:
        return reject(
            f"measurement kind {measurement.kind!r} != spec kind {spec.kind!r}"
        )
    try:
        directory.require_kind(spec.quantity_id, spec.kind)
        if directory.quantity_of(spec.unit_id) != spec.quantity_id:
            return reject(
                f"spec unit {spec.unit_id!r} is not a {spec.quantity_id!r} unit"
            )
        if directory.quantity_of(measurement.unit_id) != measurement.quantity_id:
            return reject(
                f"measurement unit {measurement.unit_id!r} is not a "
                f"{measurement.quantity_id!r} unit"
            )
        if measurement.kind not in directory.allowed_kinds(measurement.quantity_id):
            return reject(
                f"quantity {measurement.quantity_id!r} does not allow "
                f"{measurement.kind!r}"
            )
    except (KindMismatch, UnknownUnit) as exc:
        return reject(str(exc))

    # 2. real conversion through the directory.
    try:
        conversion = directory.conversion(
            measurement.unit_id, spec.unit_id, measurement.kind
        )
    except ConversionError as exc:
        return reject(str(exc))

    converted = conversion.apply(measurement.value)
    passed = spec.contains(converted)
    return Evaluation(
        status=STATUS_EVALUATED,
        passed=passed,
        converted_unit=spec.unit_id,
        converted_value=converted,
        reason=None if passed else "converted value outside specification interval",
        conversion=conversion,
        **base,
    )


def evaluate_catalog(
    directory: Directory,
    specs: list[Specification],
    measurements: list[Measurement],
    *,
    created_at: str,
) -> list[Evaluation]:
    specs_by_id = {spec.spec_id: spec for spec in specs}
    results: list[Evaluation] = []
    for measurement in measurements:
        spec = specs_by_id.get(measurement.spec_id)
        if spec is None:
            # Unreachable after parse_catalog, but keep the engine defensive.
            raise KeyError(measurement.spec_id)
        results.append(evaluate(directory, spec, measurement, created_at=created_at))
    return results
