"""Demo driver: produce materials/708 reports and the synthetic-scale report.

Run::

    python3 -m qualitylib.demo

Artifacts:
* ``reports/r1-report.json`` / ``reports/r2-report.json`` for materials/708
  (replayed in the order of ``materials/708/operations.jsonl``).
* ``synthetic/`` -- generated directory + 500-measurement catalog.
* ``reports/synthetic-report.json`` -- 8 quantities / 30 units / 500 rows,
  read back through ``materials/708/report_reader.py``.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from .ops import run_operations
from .service import QualityService
from .synthetic import write_dataset


ROOT = Path(__file__).resolve().parent.parent
MATERIALS = ROOT / "materials" / "708"
REPORTS = ROOT / "reports"
SYNTHETIC = ROOT / "synthetic"
VAR = ROOT / "var"


def main() -> int:
    REPORTS.mkdir(exist_ok=True)
    VAR.mkdir(exist_ok=True)

    print("== materials/708 operations ==")
    run_operations(
        MATERIALS / "operations.jsonl",
        db_path=VAR / "708.sqlite3",
        workdir=REPORTS,
        reader=MATERIALS / "report_reader.py",
        reset=True,
    )

    print("\n== synthetic dataset (8 quantities / 30 units / 500 measurements) ==")
    write_dataset(SYNTHETIC, measurement_count=500)
    with QualityService.open(VAR / "synthetic.sqlite3") as service:
        service.bootstrap_directory(
            json.loads((SYNTHETIC / "directory.json").read_text(encoding="utf-8"))
        )
        evaluations, report = service.evaluate(
            SYNTHETIC / "catalog.json", REPORTS / "synthetic-report.json"
        )
    print("summary", report["summary"])

    print("\n== synthetic report read through the downstream reader ==")
    completed = subprocess.run(
        [sys.executable, str(MATERIALS / "report_reader.py"),
         str(REPORTS / "synthetic-report.json")],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        print(completed.stderr, file=sys.stderr)
        return completed.returncode
    lines = completed.stdout.splitlines()
    print("\n".join(lines[:6]))
    print(f"... ({len(lines)} evaluation rows read)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
