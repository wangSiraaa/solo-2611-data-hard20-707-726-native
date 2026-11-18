"""Synthetic industrial dataset: 8 quantities, 30 units, 500 measurements.

Everything is generated from exact ``(scale, offset)`` pairs relative to a
canonical base of each quantity::

    value_base = scale * value_unit + offset_unit

so every affine cycle is consistent by construction.  The dataset includes:

* temperature absolute/difference (and a disconnected pseudo-unit ``U``);
* quantities that only allow ``absolute`` (``TIME``);
* multiple equivalent paths (a hub plus an extra chord per quantity);
* boundary values exactly on ``lo`` / ``hi`` and clearly out-of-range values;
* deliberately invalid measurements: a disconnected unit, a category
  mismatch and a disallowed category.

It is *data*, not a conversion table: the quality library still derives every
converted value from the directory graph.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .rational import ratio_json

F = Fraction

# (scale, offset) mapping unit -> base for each quantity.
_SCALES: dict[str, dict[str, tuple[Fraction, Fraction]]] = {
    "TEMP": {
        "K": (F(1), F(0)),
        "C": (F(1), F(5463, 20)),          # K = C + 273.15
        "F": (F(5, 9), F(45967, 180)),      # K = (5/9) F + 45967/180
        "R": (F(5, 9), F(0)),               # K = (5/9) R
        "U": (F(1), F(0)),                  # isolated pseudo-unit
    },
    "LENGTH": {
        "m": (F(1), F(0)),
        "cm": (F(1, 100), F(0)),
        "mm": (F(1, 1000), F(0)),
        "in": (F(127, 5000), F(0)),
        "ft": (F(381, 1250), F(0)),
    },
    "MASS": {
        "kg": (F(1), F(0)),
        "g": (F(1, 1000), F(0)),
        "mg": (F(1, 1000000), F(0)),
        "lb": (F(45359237, 100000000), F(0)),
    },
    "PRESSURE": {
        "Pa": (F(1), F(0)),
        "kPa": (F(1000), F(0)),
        "bar": (F(100000), F(0)),
        "psi": (F(6894757, 1000), F(0)),
    },
    "FLOW": {
        "m3s": (F(1), F(0)),
        "Lmin": (F(1, 60000), F(0)),
        "gpm": (F(1, 15850), F(0)),
    },
    "VOLTAGE": {
        "V": (F(1), F(0)),
        "mV": (F(1, 1000), F(0)),
        "kV": (F(1000), F(0)),
    },
    "TIME": {
        "s": (F(1), F(0)),
        "min": (F(60), F(0)),
        "h": (F(3600), F(0)),
    },
    "CONCENTRATION": {
        "molL": (F(1), F(0)),
        "mmolL": (F(1, 1000), F(0)),
        "ppm": (F(1, 1000000), F(0)),
    },
}

_BASE = {
    "TEMP": "K", "LENGTH": "m", "MASS": "kg", "PRESSURE": "Pa",
    "FLOW": "m3s", "VOLTAGE": "V", "TIME": "s", "CONCENTRATION": "molL",
}

_ALLOWED = {
    "TEMP": ["absolute", "difference"],
    "LENGTH": ["absolute", "difference"],
    "MASS": ["absolute", "difference"],
    "PRESSURE": ["absolute", "difference"],
    "FLOW": ["absolute", "difference"],
    "VOLTAGE": ["absolute", "difference"],
    "TIME": ["absolute"],
    "CONCENTRATION": ["absolute", "difference"],
}

# Isolated units: no edges at all (different connected component).
_ISOLATED = {"TEMP": {"U"}}

# Absolute / difference specification intervals in the spec's unit.
_SPECS = {
    # quantity: (abs_unit, abs_lo, abs_hi, diff_unit, diff_lo, diff_hi)
    "TEMP": ("F", F(121), F(123), "F", F(18), F(18)),
    "LENGTH": ("m", F(0), F(10), "m", F(0), F(1)),
    "MASS": ("kg", F(0), F(100), "kg", F(0), F(10)),
    "PRESSURE": ("Pa", F(0), F(1000000), "Pa", F(0), F(100000)),
    "FLOW": ("m3s", F(0), F(1), "m3s", F(0), F(1, 10)),
    "VOLTAGE": ("V", F(0), F(500), "V", F(0), F(50)),
    "TIME": ("s", F(0), F(3600), None, None, None),
    "CONCENTRATION": ("molL", F(0), F(10), "molL", F(0), F(1)),
}


@dataclass(frozen=True)
class SyntheticDataset:
    directory: dict
    catalog: dict


def _edge(from_unit, to_unit, scales):
    s_from, o_from = scales[from_unit]
    s_to, o_to = scales[to_unit]
    return {
        "edge_id": f"E_{from_unit}_{to_unit}",
        "from": from_unit,
        "to": to_unit,
        "a": ratio_json(s_from / s_to),
        "b": ratio_json((o_from - o_to) / s_to),
    }


def _convert(value: Fraction, from_unit: str, to_unit: str, kind: str, scales) -> Fraction:
    s_from, o_from = scales[from_unit]
    s_to, o_to = scales[to_unit]
    a = s_from / s_to
    if kind == "difference":
        return a * value
    b = (o_from - o_to) / s_to
    return a * value + b


def _invert(target: Fraction, from_unit: str, to_unit: str, kind: str, scales) -> Fraction:
    """Return a value in ``from_unit`` whose conversion to ``to_unit`` is target."""
    s_from, _ = scales[from_unit]
    s_to, o_to = scales[to_unit]
    a = s_from / s_to
    if kind == "difference":
        return target / a
    _, o_from = scales[from_unit]
    b = (o_from - o_to) / s_to
    return (target - b) / a


def build_dataset(measurement_count: int = 500, seed: int = 708) -> SyntheticDataset:
    quantities = [
        {"quantity_id": qid, "allowed_kinds": _ALLOWED[qid]} for qid in _SCALES
    ]
    units = []
    edges = []
    for qid, scales in _SCALES.items():
        base = _BASE[qid]
        # Every declared unit belongs to the quantity; isolated units simply
        # have no edges and therefore form their own component.
        for unit in scales:
            units.append({"unit_id": unit, "quantity_id": qid})
        connected = [u for u in scales if u not in _ISOLATED.get(qid, set())]
        for unit in connected:
            if unit == base:
                continue
            edges.append(_edge(base, unit, scales))
        # One extra chord -> two equivalent paths between its endpoints.
        if len(connected) >= 3:
            a, c = connected[1], connected[2]
            edges.append(_edge(a, c, scales))

    specs = []
    spec_for: dict[tuple[str, str], dict] = {}
    for qid, (au, alo, ahi, du, dlo, dhi) in _SPECS.items():
        abs_spec = {
            "spec_id": f"S_{qid}_ABS",
            "quantity_id": qid,
            "kind": "absolute",
            "unit_id": au,
            "lo": ratio_json(alo),
            "hi": ratio_json(ahi),
        }
        specs.append(abs_spec)
        spec_for[(qid, "absolute")] = abs_spec
        if du is not None:
            diff_spec = {
                "spec_id": f"S_{qid}_DIFF",
                "quantity_id": qid,
                "kind": "difference",
                "unit_id": du,
                "lo": ratio_json(dlo),
                "hi": ratio_json(dhi),
            }
            specs.append(diff_spec)
            spec_for[(qid, "difference")] = diff_spec

    rng = random.Random(seed)
    measurements = []
    qids = list(_SCALES)
    for index in range(measurement_count):
        measurement_id = f"SYN-{index + 1:04d}"
        qid = qids[index % len(qids)]
        allowed = _ALLOWED[qid]
        kind = rng.choice(allowed)

        # --- deliberate invalid cases -------------------------------------
        bucket = index % 50  # every 50 measurements, inject a batch of oddities
        if qid == "TEMP" and bucket in (0, 1):
            # disconnected unit U -> must be rejected, never passed.
            unit = "U"
            value = F(50)
            spec = spec_for[(qid, "absolute")]
            measurements.append(_measurement(measurement_id, index, qid, "absolute",
                                             unit, value, spec))
            continue
        if qid == "TIME" and bucket == 2:
            # difference is not an allowed category for TIME -> rejected.
            spec = spec_for[(qid, "absolute")]
            measurements.append(_measurement(measurement_id, index, qid, "difference",
                                             "min", F(5), spec))
            continue
        if bucket == 3:
            # measurement kind disagrees with the spec kind -> rejected.
            spec_kind = "absolute" if kind == "difference" else "difference"
            if (qid, spec_kind) not in spec_for:
                spec_kind = "absolute"
            spec = spec_for[(qid, spec_kind)]
            measurements.append(_measurement(measurement_id, index, qid, kind,
                                             _BASE[qid], F(1), spec))
            continue

        # --- valid measurement, sometimes boundary / out-of-range ---------
        spec = spec_for[(qid, kind)]
        scales = _SCALES[qid]
        connected = [u for u in scales if u not in _ISOLATED.get(qid, set())]
        unit = rng.choice(connected)
        lo = _frac(spec["lo"])
        hi = _frac(spec["hi"])
        roll = rng.random()
        if roll < 0.10:
            target = lo  # exact lower boundary -> pass
        elif roll < 0.20:
            target = hi  # exact upper boundary -> pass
        elif roll < 0.35:
            target = hi + (abs(hi) + 1)  # clearly out of range -> fail
        else:
            target = (lo + hi) / 2
        value = _invert(target, unit, spec["unit_id"], kind, scales)
        measurements.append(_measurement(measurement_id, index, qid, kind,
                                         unit, value, spec))

    catalog = {"specifications": specs, "measurements": measurements}
    directory = {
        "revision": "syn-r1",
        "quantities": quantities,
        "units": units,
        "edges": edges,
    }
    return SyntheticDataset(directory=directory, catalog=catalog)


def _measurement(measurement_id, index, qid, kind, unit, value, spec):
    return {
        "measurement_id": measurement_id,
        "sample_id": f"SAMPLE-{index % 40:03d}",
        "spec_id": spec["spec_id"],
        "quantity_id": qid,
        "kind": kind,
        "unit_id": unit,
        "value": ratio_json(value),
    }


def _frac(obj) -> Fraction:
    return Fraction(obj["n"], obj["d"])


def write_dataset(directory: str | Path, *, measurement_count: int = 500) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    dataset = build_dataset(measurement_count=measurement_count)
    (directory / "directory.json").write_text(
        json.dumps(dataset.directory, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (directory / "catalog.json").write_text(
        json.dumps(dataset.catalog, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return directory


if __name__ == "__main__":  # pragma: no cover - convenience
    import argparse

    parser = argparse.ArgumentParser(description="Generate the synthetic dataset")
    parser.add_argument("out", nargs="?", default="synthetic")
    parser.add_argument("--measurements", type=int, default=500)
    args = parser.parse_args()
    write_dataset(args.out, measurement_count=args.measurements)
    print(f"wrote synthetic dataset to {args.out}")
