"""Load the supplied synthetic material set into a fresh SQLite database.

The material directory contains permanent car lengths (``vehicles.json``), the
initial ordered compositions (``state.json``) and a JSONL operation log.  Cargo
identity is the permanent ``(cargo_id, car_id, local_position)`` binding.
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

from .composition import CompositionService
from .model import ValidationError, frac_to_json
from .unload import add_cargo


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_material(conn, material_dir: str | Path) -> dict:
    """Populate ``conn`` from a material directory; fails if it is not empty."""
    material_dir = Path(material_dir)
    vehicles = _read_json(material_dir / "vehicles.json")
    state = _read_json(material_dir / "state.json")
    operations_path = material_dir / "operations.jsonl"

    if conn.execute("SELECT COUNT(*) FROM cars").fetchone()[0]:
        raise ValidationError("database already contains cars; refusing to reload")

    comp = CompositionService(conn)
    for car in vehicles["cars"]:
        comp.add_car(car["car_id"], car["length"])
    for train in state["trains"]:
        comp.create_train(train["train_id"], train["cars"])
    for cargo in state.get("cargo", []):
        add_cargo(conn, cargo["cargo_id"], cargo["car_id"], cargo["local_position"])

    operations = []
    if operations_path.exists():
        for line in operations_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                operations.append(json.loads(line))

    return {
        "cars": [c["car_id"] for c in vehicles["cars"]],
        "trains": [t["train_id"] for t in state["trains"]],
        "cargo": [c["cargo_id"] for c in state["cargo"]],
        "platform_window": [frac_to_json(v) for v in state.get("platform_window", [])],
        "operations": operations,
    }


def _summary_window(summary: dict):
    raw = summary.get("platform_window") or []
    if not raw:
        return None
    return [Fraction(item["n"], item["d"]) for item in raw]


def replay_operations(store, summary: dict, window=None) -> list[dict]:
    """Apply a material operation log through the real services.

    ``couple``/``split``/``reverse`` go through the composition service;
    ``scan_platform`` produces a ``platform-scan/v1`` report using the
    material platform window unless an explicit one is supplied.
    ``read_report`` only reads back an already stored report.
    """
    if window is None:
        window = _summary_window(summary)
    results = []
    for op in summary.get("operations", []):
        kind = op.get("op")
        if kind in ("couple", "split", "reverse"):
            results.append({"op": kind, "result": store.apply_event(op)})
        elif kind == "scan_platform":
            if window is None:
                raise ValidationError("scan_platform needs a platform window")
            report = store.platform.scan_platform(
                op["train_id"],
                op["report_id"],
                list(window),
                op.get("composition_version"),
            )
            results.append({"op": "scan_platform", "result": report})
        elif kind == "read_report":
            results.append(
                {"op": "read_report", "result": store.platform.get_report(op["report_id"])}
            )
        else:
            raise ValidationError(f"unsupported material operation: {kind!r}")
    return results


def material_summary(material_dir: str | Path) -> dict:
    """Read a material directory without touching a database."""
    material_dir = Path(material_dir)
    state = _read_json(material_dir / "state.json")
    ops_path = material_dir / "operations.jsonl"
    operations = []
    if ops_path.exists():
        operations = [
            json.loads(line)
            for line in ops_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    return {
        "platform_window": [frac_to_json(v) for v in state.get("platform_window", [])],
        "operations": operations,
    }
