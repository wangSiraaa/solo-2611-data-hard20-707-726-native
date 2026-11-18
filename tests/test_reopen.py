"""Persistence across close/reopen: revisions, catalog and evaluations survive."""
from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

import pytest

from qualitylib.db import list_evaluations
from qualitylib.errors import RevisionError
from qualitylib.service import QualityService

F = Fraction
MATERIALS = Path("materials/708")


def _json(name):
    return json.loads((MATERIALS / name).read_text(encoding="utf-8"))


def test_reopen_keeps_revision_report_and_history(tmp_path):
    db_path = tmp_path / "quality.sqlite3"

    with QualityService.open(db_path) as service:
        service.bootstrap_directory(_json("directory.json"))
        _, report_r1 = service.evaluate(MATERIALS / "measurements.json",
                                        tmp_path / "r1-report.json")
        service.apply_revision(_json("revision.json"))
        _, report_r2 = service.evaluate(MATERIALS / "measurements.json",
                                        tmp_path / "r2-report.json")

    # Reopen the same database: current revision and both histories are intact.
    with QualityService.open(db_path) as reopened:
        current = reopened.current_directory()
        assert current.revision == "r2"
        assert set(current.edges) == {"E_CF", "E_KC", "E_KF2"}

        revisions = {row["revision"] for row in reopened.revisions()}
        assert {"r1", "r2"}.issubset(revisions)

        history = list_evaluations(reopened.conn, "M_ABS")
        assert [h["directory_revision"] for h in history] == ["r1", "r2"]
        assert history[0]["converted_value"] == {"n": 122, "d": 1}
        assert history[0]["passed"] is True
        assert history[1]["converted_value"] == {"n": 120, "d": 1}
        assert history[1]["passed"] is False

        # Rebuild the r1 report from stored rows only (no conversion involved).
        rebuilt = reopened.write_report_for_revision("r1", tmp_path / "r1-rebuilt.json")
        assert rebuilt["evaluations"][0]["converted_value"] == {"n": 122, "d": 1}
        assert rebuilt["evaluations"][0]["directory_revision"] == "r1"


def test_reopen_preserves_rejected_revision_state(tmp_path):
    db_path = tmp_path / "quality.sqlite3"
    with QualityService.open(db_path) as service:
        service.bootstrap_directory(_json("directory.json"))
        service.apply_revision(_json("revision.json"))
        with pytest.raises(RevisionError):
            service.apply_revision(_json("invalid_revision.json"))

    with QualityService.open(db_path) as reopened:
        directory = reopened.current_directory()
        assert directory.revision == "r2"
        assert set(directory.edges) == {"E_CF", "E_KC", "E_KF2"}
