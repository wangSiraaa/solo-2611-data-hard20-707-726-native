#!/usr/bin/env python3
"""Advance a working copy of the frozen material history.

Usage:
    python3 run_history.py                 # copy -> data/history.sqlite, run ops
    python3 run_history.py --in-place      # run ops on data/history.sqlite again

The material database is never modified.  Each run copies it fresh unless
``--in-place`` is given, so the documented operations always start from the
frozen envelope.  After running, the supplied read-only client is invoked to
show historical orders, current remaining counts and cancellations.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from coupon_service.operations import run_operations

ROOT = Path(__file__).resolve().parent
MATERIAL = ROOT / "materials" / "709"
DEFAULT_DB = ROOT / "data" / "history.sqlite"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="advance the existing database instead of re-copying the material",
    )
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not args.in_place:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(MATERIAL / "history.sqlite", db_path)

    out_path = args.out or str(db_path.with_suffix(".operations-out.jsonl"))
    outcomes = run_operations(
        db_path, MATERIAL / "operations.jsonl", output_path=out_path
    )
    for outcome in outcomes:
        print(json.dumps(outcome.as_dict(), ensure_ascii=False, sort_keys=True))

    print(f"\n# read-only client: {db_path}", file=sys.stderr)
    subprocess.run(
        [sys.executable, str(ROOT / "ledger_reader.py"), str(db_path)],
        check=True,
    )
    print(f"# outcomes: {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
