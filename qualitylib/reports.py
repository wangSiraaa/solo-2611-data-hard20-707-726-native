"""Report serialisation: ``measurement-report/v1``.

The public contract consumed by ``report_reader.py`` is preserved:
``evaluation_id``, ``measurement_id``, ``directory_revision``,
``converted_value`` (``{"n", "d"}``) and ``passed``.

Extra evidence (spec snapshots, conversion path, revision fingerprint, ...)
is additive and ignored by the reader.  Measurements that could not be
converted are *not* smuggled into ``evaluations`` with a fake value; they are
reported in a separate ``rejections`` list so the reader contract stays sound
while the failure remains visible.
"""
from __future__ import annotations

import json
from pathlib import Path


REPORT_SCHEMA = "measurement-report/v1"


def build_report(*, revision: str, fingerprint: str, evaluations: list) -> dict:
    """Build a v1 report from evaluation objects (see ``evaluate.Evaluation``)."""
    entries = []
    rejections = []
    for evaluation in evaluations:
        if evaluation.convertible:
            entries.append(evaluation.to_report_entry())
        else:
            rejections.append(evaluation.to_rejection_entry())
    return {
        "schema": REPORT_SCHEMA,
        "directory_revision": revision,
        "directory_fingerprint": fingerprint,
        "evaluations": entries,
        "rejections": rejections,
        "summary": {
            "evaluated": len(entries),
            "passed": sum(1 for e in entries if e["passed"]),
            "failed": sum(1 for e in entries if not e["passed"]),
            "rejected": len(rejections),
        },
    }


def build_report_from_rows(*, revision: str, fingerprint: str, rows: list[dict]) -> dict:
    """Build a v1 report from stored evaluation rows (DB-shaped dicts)."""
    entries = []
    rejections = []
    for row in rows:
        if row["status"] == "evaluated":
            entries.append(
                {
                    "evaluation_id": row["evaluation_id"],
                    "measurement_id": row["measurement_id"],
                    "directory_revision": row["directory_revision"],
                    "converted_value": row["converted_value"],
                    "passed": row["passed"],
                    "sample_id": row.get("sample_id"),
                    "spec_id": row["spec_id"],
                    "quantity_id": row["quantity_id"],
                    "kind": row["kind"],
                    "converted_unit": row["converted_unit"],
                    "status": row["status"],
                    "directory_fingerprint": row["directory_fingerprint"],
                    "path": row["path"],
                    "conversion": row.get("conversion"),
                    "specification": row["specification"],
                    "measurement": row["measurement"],
                }
            )
        else:
            rejections.append(
                {
                    "evaluation_id": row["evaluation_id"],
                    "measurement_id": row["measurement_id"],
                    "directory_revision": row["directory_revision"],
                    "status": row["status"],
                    "reason": row.get("reason"),
                    "sample_id": row.get("sample_id"),
                    "spec_id": row["spec_id"],
                    "quantity_id": row["quantity_id"],
                    "kind": row["kind"],
                    "passed": False,
                    "specification": row["specification"],
                    "measurement": row["measurement"],
                }
            )
    return {
        "schema": REPORT_SCHEMA,
        "directory_revision": revision,
        "directory_fingerprint": fingerprint,
        "evaluations": entries,
        "rejections": rejections,
        "summary": {
            "evaluated": len(entries),
            "passed": sum(1 for e in entries if e["passed"]),
            "failed": sum(1 for e in entries if not e["passed"]),
            "rejected": len(rejections),
        },
    }


def write_report(path: str | Path, report: dict) -> None:
    path = Path(path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def read_report(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema") != REPORT_SCHEMA:
        raise ValueError("unsupported report schema")
    return data
