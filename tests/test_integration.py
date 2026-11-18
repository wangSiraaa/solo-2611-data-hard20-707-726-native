"""End-to-end: operations replay, synthetic scale, report reader."""
from __future__ import annotations

import json
import subprocess
import sys
from fractions import Fraction
from pathlib import Path


from qualitylib.catalog import parse_catalog
from qualitylib.db import now_iso
from qualitylib.directory import parse_directory
from qualitylib.evaluate import evaluate_catalog
from qualitylib.ops import run_operations
from qualitylib.reports import read_report
from qualitylib.synthetic import write_dataset

F = Fraction
MATERIALS = Path("materials/708")


def test_operations_replay_matches_hand_calculation(tmp_path):
    result = run_operations(
        MATERIALS / "operations.jsonl",
        db_path=tmp_path / "quality.sqlite3",
        workdir=tmp_path / "reports",
        reader=MATERIALS / "report_reader.py",
        reset=True,
        stream=open(tmp_path / "ops.log", "w", encoding="utf-8"),
    )
    reports = tmp_path / "reports"
    assert (reports / "r1-report.json").exists()
    assert (reports / "r2-report.json").exists()

    r1 = read_report(reports / "r1-report.json")
    r2 = read_report(reports / "r2-report.json")
    r1_by_id = {e["measurement_id"]: e for e in r1["evaluations"]}
    r2_by_id = {e["measurement_id"]: e for e in r2["evaluations"]}

    assert r1_by_id["M_ABS"]["converted_value"] == {"n": 122, "d": 1}
    assert r1_by_id["M_ABS"]["passed"] is True
    assert r1_by_id["M_DIFF"]["converted_value"] == {"n": 18, "d": 1}
    assert r1_by_id["M_DIFF"]["passed"] is True

    assert r2_by_id["M_ABS"]["converted_value"] == {"n": 120, "d": 1}
    assert r2_by_id["M_ABS"]["passed"] is False
    assert r2_by_id["M_DIFF"]["converted_value"] == {"n": 18, "d": 1}
    assert r2_by_id["M_DIFF"]["passed"] is True

    # The rejected bad batch is recorded and the current revision stayed r2.
    rejected = [r for r in result["operations"] if r.get("rejected")]
    assert len(rejected) == 1
    assert rejected[0]["revision"] == "r2"
    # The old report was read through the downstream reader.
    old = [r for r in result["operations"] if r["op"] == "read_old_report"]
    assert old and any("M_ABS" in line for line in old[0]["reader_output"])


def test_operations_reader_uses_stored_reports_not_memory(tmp_path):
    run_operations(
        MATERIALS / "operations.jsonl",
        db_path=tmp_path / "q.sqlite3",
        workdir=tmp_path / "reports",
        reader=MATERIALS / "report_reader.py",
        reset=True,
        stream=open(tmp_path / "ops.log", "w", encoding="utf-8"),
    )
    completed = subprocess.run(
        [sys.executable, str(MATERIALS / "report_reader.py"),
         str(tmp_path / "reports" / "r2-report.json")],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "M_ABS r2 120 FAIL" in completed.stdout
    assert "M_DIFF r2 18 PASS" in completed.stdout


def test_synthetic_scale_and_exactness(tmp_path):
    out = write_dataset(tmp_path / "synthetic", measurement_count=500)
    directory_doc = json.loads((out / "directory.json").read_text())
    catalog_doc = json.loads((out / "catalog.json").read_text())

    directory = parse_directory(directory_doc)
    assert len(directory.quantities) == 8
    assert len(directory.units) == 30
    assert len(catalog_doc["measurements"]) == 500
    # TEMP has a disconnected pseudo-unit.
    assert len(directory.components("TEMP")) == 2

    specs, measurements = parse_catalog(catalog_doc)
    evaluations = evaluate_catalog(directory, specs, measurements, created_at=now_iso())
    assert len(evaluations) == 500

    statuses = [e.status for e in evaluations]
    assert statuses.count("rejected") > 0
    # No rejected measurement is ever marked as passing.
    assert all(not e.passed for e in evaluations if e.status == "rejected")
    # Every evaluated conversion used a real path (or identity).
    for e in evaluations:
        if e.status == "evaluated":
            assert e.converted_value is not None
            assert e.conversion is not None
    # Boundary cases should exist and pass.
    boundary_pass = 0
    for e in evaluations:
        if e.status == "evaluated" and e.passed:
            spec = e.specification
            lo = F(spec["lo"]["n"], spec["lo"]["d"])
            hi = F(spec["hi"]["n"], spec["hi"]["d"])
            if e.converted_value in (lo, hi):
                boundary_pass += 1
    assert boundary_pass > 0
    # Out-of-range cases should exist and fail.
    assert any(e.status == "evaluated" and not e.passed for e in evaluations)


def test_synthetic_multi_path_agreement(tmp_path):
    out = write_dataset(tmp_path / "synthetic", measurement_count=20)
    directory = parse_directory(json.loads((out / "directory.json").read_text()))
    # LENGTH has an extra chord m-cm/mm; both routes must agree exactly.
    direct = directory.conversion("cm", "mm", "absolute")
    via = directory.conversion("cm", "m", "absolute")
    composed = directory.conversion("m", "mm", "absolute").affine.compose_into(via.affine)
    assert direct.affine == composed
