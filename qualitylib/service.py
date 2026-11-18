"""Service layer: one coherent API over the directory, catalog and reports.

Typical flow (mirrors ``materials/708/operations.jsonl``)::

    service = QualityService.open("var/quality.sqlite3")
    service.bootstrap_directory(directory_doc)   # r1
    service.store_catalog(catalog_doc)
    service.evaluate("measurements.json", "reports/r1-report.json")
    service.apply_revision(revision_doc)         # r2, atomic or unchanged
    service.evaluate("measurements.json", "reports/r2-report.json")
    service.apply_revision(bad_doc)              # raises RevisionError

Every evaluation run inserts fresh, immutable rows; re-evaluating with a new
revision never mutates the old rows.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import db
from .catalog import parse_catalog, Specification, Measurement
from .directory import Directory, parse_directory, revise
from .evaluate import evaluate_catalog, Evaluation
from .errors import ValidationError
from .reports import build_report, write_report


class QualityService:
    def __init__(self, conn):
        self.conn = conn

    @classmethod
    def open(cls, path: str | Path) -> "QualityService":
        conn = db.connect(path)
        db.init_schema(conn)
        return cls(conn)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "QualityService":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- directory ----------------------------------------------------------
    def bootstrap_directory(self, obj: dict) -> Directory:
        """Store an initial directory revision (validated)."""
        directory = parse_directory(obj)
        db.save_directory(self.conn, directory, parent_revision=None)
        return directory

    def current_directory(self) -> Directory:
        return db.load_directory(self.conn)

    def apply_revision(self, obj: dict) -> Directory:
        """Validate a batch against the *current final graph* and adopt it.

        On any final error the batch is rejected and the current revision is
        left byte-for-byte untouched.
        """
        current = self.current_directory()
        candidate = revise(current, obj)
        db.save_directory(self.conn, candidate, parent_revision=current.revision)
        return candidate

    def revisions(self) -> list[dict]:
        return [dict(row) for row in db.list_revisions(self.conn)]

    def directory_document(self, revision: str | None = None) -> dict:
        return db.load_directory(self.conn, revision).to_json()

    # -- catalog ------------------------------------------------------------
    def store_catalog(self, obj: dict) -> tuple[list[Specification], list[Measurement]]:
        specs, measurements = parse_catalog(obj)
        db.store_catalog(self.conn, obj)
        return specs, measurements

    # -- evaluation ---------------------------------------------------------
    def evaluate(
        self,
        catalog_path: str | Path,
        report_path: str | Path | None = None,
        *,
        revision: str | None = None,
    ) -> tuple[list[Evaluation], dict]:
        """Evaluate every measurement in a catalog file at a stored revision.

        Catalog definitions are (re)stored, then a fresh evaluation run is
        produced.  Returns the evaluations and the report document; optionally
        writes the report to ``report_path``.
        """
        catalog_obj = json.loads(Path(catalog_path).read_text(encoding="utf-8"))
        specs, measurements = parse_catalog(catalog_obj)
        db.store_catalog(self.conn, catalog_obj)

        directory = db.load_directory(self.conn, revision)
        created_at = db.now_iso()
        evaluations = evaluate_catalog(
            directory, specs, measurements, created_at=created_at
        )
        db.insert_evaluations(self.conn, [e.to_row() for e in evaluations])

        report = build_report(
            revision=directory.revision,
            fingerprint=directory.fingerprint,
            evaluations=evaluations,
        )
        if report_path is not None:
            write_report(report_path, report)
        return evaluations, report

    def evaluate_measurement(
        self, measurement_id: str, revision: str | None = None
    ) -> Evaluation:
        """Re-evaluate one measurement at the current (or given) revision."""
        row = self.conn.execute(
            "SELECT * FROM measurements WHERE measurement_id = ?", (measurement_id,)
        ).fetchone()
        if row is None:
            raise ValidationError(f"unknown measurement {measurement_id!r}")
        spec_row = self.conn.execute(
            "SELECT document FROM specifications WHERE spec_id = ?", (row["spec_id"],)
        ).fetchone()
        if spec_row is None:
            raise ValidationError(f"unknown spec {row['spec_id']!r}")
        spec = _spec_from_doc(json.loads(spec_row["document"]))
        measurement = _measurement_from_doc(json.loads(row["document"]))
        directory = db.load_directory(self.conn, revision)
        evaluation = evaluate_catalog(
            directory, [spec], [measurement], created_at=db.now_iso()
        )[0]
        db.insert_evaluations(self.conn, [evaluation.to_row()])
        return evaluation

    # -- reports ------------------------------------------------------------
    def write_report_for_revision(
        self, revision: str, report_path: str | Path
    ) -> dict:
        """Rebuild a report from stored rows for a revision (no re-conversion)."""
        rows = db.all_evaluations(self.conn, revision)
        directory = db.load_directory(self.conn, revision)
        report = _report_from_rows(directory, rows)
        write_report(report_path, report)
        return report


def _spec_from_doc(doc: dict) -> Specification:
    from .rational import parse_ratio

    return Specification(
        spec_id=doc["spec_id"],
        quantity_id=doc["quantity_id"],
        kind=doc["kind"],
        unit_id=doc["unit_id"],
        lo=parse_ratio(doc["lo"]),
        hi=parse_ratio(doc["hi"]),
    )


def _measurement_from_doc(doc: dict) -> Measurement:
    from .rational import parse_ratio

    return Measurement(
        measurement_id=doc["measurement_id"],
        sample_id=doc.get("sample_id"),
        spec_id=doc["spec_id"],
        quantity_id=doc["quantity_id"],
        kind=doc["kind"],
        unit_id=doc["unit_id"],
        value=parse_ratio(doc["value"]),
    )


def _report_from_rows(directory: Directory, rows: list[dict]) -> dict:
    from .reports import build_report_from_rows

    return build_report_from_rows(
        revision=directory.revision,
        fingerprint=directory.fingerprint,
        rows=rows,
    )
