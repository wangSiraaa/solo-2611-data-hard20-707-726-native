"""Catalog, evaluation, persistence and report-client tests."""
from __future__ import annotations

import json
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import pytest

from qualitylib.catalog import parse_catalog, parse_measurement, parse_specification
from qualitylib.db import now_iso
from qualitylib.directory import parse_directory
from qualitylib.errors import RevisionError, ValidationError
from qualitylib.evaluate import evaluate
from qualitylib.reports import REPORT_SCHEMA
from qualitylib.service import QualityService

F = Fraction
MATERIALS = Path("materials/708")


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


@pytest.fixture
def service(tmp_path):
    with QualityService.open(tmp_path / "quality.sqlite3") as svc:
        svc.bootstrap_directory(_json(MATERIALS / "directory.json"))
        yield svc


# ---------------------------------------------------------------------------
# Spec / measurement validation
# ---------------------------------------------------------------------------
def test_spec_lo_must_not_exceed_hi():
    with pytest.raises(ValidationError):
        parse_specification(
            {"spec_id": "S", "quantity_id": "T", "kind": "absolute", "unit_id": "C",
             "lo": {"n": 5, "d": 1}, "hi": {"n": 1, "d": 1}}
        )


def test_catalog_measurement_needs_known_spec():
    with pytest.raises(ValidationError):
        parse_catalog(
            {
                "specifications": [],
                "measurements": [
                    {"measurement_id": "M", "spec_id": "MISSING", "quantity_id": "T",
                     "kind": "absolute", "unit_id": "C", "value": {"n": 1, "d": 1}}
                ],
            }
        )


# ---------------------------------------------------------------------------
# Evaluation correctness (in-memory)
# ---------------------------------------------------------------------------
def _r1():
    return parse_directory(_json(MATERIALS / "directory.json"))


def test_absolute_evaluation_r1():
    specs, measurements = parse_catalog(_json(MATERIALS / "measurements.json"))
    directory = _r1()
    evaluation = evaluate(
        directory,
        specs[0],
        measurements[0],
        created_at=now_iso(),
    )
    assert evaluation.status == "evaluated"
    assert evaluation.converted_value == F(122)
    assert evaluation.passed is True
    assert evaluation.directory_revision == "r1"
    assert evaluation.conversion.path  # real edge path captured


def test_difference_evaluation_r1():
    specs, measurements = parse_catalog(_json(MATERIALS / "measurements.json"))
    evaluation = evaluate(
        _r1(), specs[1], measurements[1], created_at=now_iso()
    )
    assert evaluation.converted_value == F(18)
    assert evaluation.passed is True


def test_missing_conversion_never_passes():
    specs, measurements = parse_catalog(_json(MATERIALS / "measurements.json"))
    directory = _r1()
    # Retarget a measurement to the disconnected U unit.
    measurement = measurements[0]
    broken = measurement.__class__(
        measurement.measurement_id, measurement.sample_id, measurement.spec_id,
        measurement.quantity_id, measurement.kind, "U", F(50),
    )
    evaluation = evaluate(directory, specs[0], broken, created_at=now_iso())
    assert evaluation.status == "rejected"
    assert evaluation.passed is False
    assert evaluation.converted_value is None


def test_kind_mismatch_never_passes():
    specs, measurements = parse_catalog(_json(MATERIALS / "measurements.json"))
    measurement = measurements[1]  # difference
    evaluation = evaluate(_r1(), specs[0], measurement, created_at=now_iso())
    assert evaluation.status == "rejected"
    assert evaluation.passed is False


def test_boundary_values_inclusive():
    spec = parse_specification(
        {"spec_id": "S", "quantity_id": "TMP", "kind": "absolute", "unit_id": "F",
         "lo": {"n": 121, "d": 1}, "hi": {"n": 123, "d": 1}}
    )
    directory = parse_directory(
        {
            "revision": "x",
            "quantities": [{"quantity_id": "TMP", "allowed_kinds": ["absolute", "difference"]}],
            "units": [{"unit_id": "C", "quantity_id": "TMP"},
                      {"unit_id": "F", "quantity_id": "TMP"}],
            "edges": [{"edge_id": "e", "from": "C", "to": "F",
                       "a": {"n": 9, "d": 5}, "b": {"n": 32, "d": 1}}],
        }
    )

    def measure(measurement_id, n, d):
        return parse_measurement(
            {"measurement_id": measurement_id, "spec_id": "S", "quantity_id": "TMP",
             "kind": "absolute", "unit_id": "C", "value": {"n": n, "d": d}}
        )

    # (121-32)*5/9 = 445/9 C   and   (123-32)*5/9 = 455/9 C.
    m_lo = measure("ML", 445, 9)
    m_hi = measure("MH", 455, 9)
    m_out = measure("MO", 456, 9)  # -> 123.2 F, out of range
    assert evaluate(directory, spec, m_lo, created_at=now_iso()).passed is True
    assert evaluate(directory, spec, m_hi, created_at=now_iso()).passed is True
    assert evaluate(directory, spec, m_lo, created_at=now_iso()).converted_value == F(121)
    assert evaluate(directory, spec, m_hi, created_at=now_iso()).converted_value == F(123)
    assert evaluate(directory, spec, m_out, created_at=now_iso()).passed is False


# ---------------------------------------------------------------------------
# Persistence: atomic snapshots, old reports preserved
# ---------------------------------------------------------------------------
def test_evaluation_snapshots_and_revision_versions(service):
    _, report_r1 = service.evaluate(MATERIALS / "measurements.json")
    assert report_r1["directory_revision"] == "r1"

    service.apply_revision(_json(MATERIALS / "revision.json"))
    _, report_r2 = service.evaluate(MATERIALS / "measurements.json")
    assert report_r2["directory_revision"] == "r2"

    # r1 rows still exist, unchanged, and explainable.
    old = service.conn.execute(
        "SELECT * FROM evaluation_runs WHERE directory_revision='r1'"
    ).fetchall()
    new = service.conn.execute(
        "SELECT * FROM evaluation_runs WHERE directory_revision='r2'"
    ).fetchall()
    assert len(old) == 2 and len(new) == 2
    assert {row["converted_n"] for row in old} == {122, 18}
    assert {row["converted_n"] for row in new} == {120, 18}
    # Each row carries its own spec + measurement snapshot.
    for row in old + new:
        spec = json.loads(row["spec_snapshot"])
        measurement = json.loads(row["measurement_snapshot"])
        assert spec["spec_id"] == row["spec_id"]
        assert measurement["measurement_id"] == row["measurement_id"]
        assert json.loads(row["path_json"])


def test_invalid_revision_leaves_directory_untouched(service):
    service.apply_revision(_json(MATERIALS / "revision.json"))
    before = service.current_directory()
    with pytest.raises(RevisionError):
        service.apply_revision(_json(MATERIALS / "invalid_revision.json"))
    after = service.current_directory()
    assert after.revision == before.revision == "r2"
    assert after.fingerprint == before.fingerprint
    assert set(after.edges) == {"E_CF", "E_KC", "E_KF2"}


def test_reevaluate_uses_new_revision_and_keeps_old(service):
    service.evaluate(MATERIALS / "measurements.json")
    service.apply_revision(_json(MATERIALS / "revision.json"))
    new_eval = service.evaluate_measurement("M_ABS")
    assert new_eval.directory_revision == "r2"
    assert new_eval.converted_value == F(120)
    assert new_eval.passed is False

    from qualitylib.db import list_evaluations

    history = list_evaluations(service.conn, "M_ABS")
    assert [h["directory_revision"] for h in history] == ["r1", "r2"]
    assert history[0]["converted_value"] == {"n": 122, "d": 1}
    assert history[0]["passed"] is True
    assert history[1]["converted_value"] == {"n": 120, "d": 1}
    assert history[1]["passed"] is False


def test_report_v1_contract(service):
    _, doc = service.evaluate(MATERIALS / "measurements.json")
    assert doc["schema"] == REPORT_SCHEMA
    for entry in doc["evaluations"]:
        assert set(["evaluation_id", "measurement_id", "directory_revision",
                    "converted_value", "passed"]).issubset(entry)
        assert set(entry["converted_value"]) == {"n", "d"}
        assert isinstance(entry["passed"], bool)


def test_rejected_measurements_are_not_in_evaluations(service):
    # Rewrite the catalog so M_ABS uses the disconnected U unit.
    catalog = _json(MATERIALS / "measurements.json")
    catalog["measurements"][0]["unit_id"] = "U"
    path = Path(service.conn.execute("PRAGMA database_list").fetchone()["file"]).parent
    catalog_path = path / "catalog_bad.json"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    _, report = service.evaluate(catalog_path)
    assert report["summary"]["rejected"] == 1
    assert all(e["measurement_id"] != "M_ABS" for e in report["evaluations"])
    assert any(r["measurement_id"] == "M_ABS" for r in report["rejections"])


def test_report_reader_subprocess(tmp_path, service):
    service.evaluate(MATERIALS / "measurements.json", tmp_path / "report.json")
    completed = subprocess.run(
        [sys.executable, str(MATERIALS / "report_reader.py"), str(tmp_path / "report.json")],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    lines = completed.stdout.strip().splitlines()
    assert len(lines) == 2
    assert "M_ABS" in lines[0] and "122" in lines[0] and "PASS" in lines[0]
