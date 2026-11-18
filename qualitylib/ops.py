"""Operations runner: replay ``operations.jsonl`` against a fresh store.

Each line is a JSON operation:

* ``evaluate``          -- evaluate a catalog file, write a report
* ``apply_revision``    -- apply a revision batch (``expect`` may assert a
                            rejection and that nothing changed)
* ``read_old_report``   -- read a written report through the downstream
                            ``report_reader.py`` contract

Usage::

    python -m qualitylib.ops --db var/quality.sqlite3 --workdir . \
        materials/708/operations.jsonl --reader materials/708/report_reader.py
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .errors import QualityError, RevisionError
from .service import QualityService


def run_operations(
    operations_path: str | Path,
    *,
    db_path: str | Path,
    workdir: str | Path,
    base_dir: str | Path | None = None,
    reader: str | Path | None = None,
    reset: bool = True,
    stream=None,
) -> dict:
    operations_path = Path(operations_path)
    workdir = Path(workdir)
    base_dir = Path(base_dir) if base_dir is not None else operations_path.parent
    out = stream or sys.stdout

    def say(message: str) -> None:
        print(message, file=out)

    if reset:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(db_path) + suffix)
            if candidate.exists():
                candidate.unlink()
    workdir.mkdir(parents=True, exist_ok=True)

    operations = [
        json.loads(line)
        for line in operations_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    # When the script does not bootstrap explicitly, an adjacent directory.json
    # is the base revision (as in materials/708).
    if not any(op.get("op") == "bootstrap_directory" for op in operations):
        seed = base_dir / "directory.json"
        if seed.exists():
            operations.insert(
                0, {"op": "bootstrap_directory", "file": seed.name}
            )

    results = []
    with QualityService.open(db_path) as service:
        # The very first directory file establishes the base revision.
        for index, operation in enumerate(operations):
            op = operation.get("op")
            if op == "bootstrap_directory":
                doc = _load_json(base_dir / operation["file"])
                directory = service.bootstrap_directory(doc)
                say(f"[{index}] bootstrap_directory {operation['file']} -> "
                    f"{directory.revision} ({directory.fingerprint[:12]})")
                results.append({"op": op, "revision": directory.revision})
            elif op == "evaluate":
                catalog = base_dir / operation["file"]
                report = workdir / operation["report"]
                evaluations, report_doc = service.evaluate(catalog, report)
                say(
                    f"[{index}] evaluate {operation['file']} @ "
                    f"{report_doc['directory_revision']} -> {operation['report']} "
                    f"{report_doc['summary']}"
                )
                results.append(
                    {
                        "op": op,
                        "report": str(report),
                        "revision": report_doc["directory_revision"],
                        "summary": report_doc["summary"],
                    }
                )
            elif op == "apply_revision":
                doc = _load_json(base_dir / operation["file"])
                expect = operation.get("expect")
                before = service.current_directory()
                try:
                    directory = service.apply_revision(doc)
                except RevisionError as exc:
                    if expect != "reject_without_change":
                        say(f"[{index}] apply_revision FAILED: {exc}")
                        raise
                    after = service.current_directory()
                    if after.revision != before.revision or after.fingerprint != before.fingerprint:
                        raise RuntimeError("rejected batch changed the current revision") from exc
                    say(
                        f"[{index}] apply_revision {operation['file']} rejected as "
                        f"expected ({exc}); current still {after.revision}"
                    )
                    results.append(
                        {
                            "op": op,
                            "rejected": True,
                            "reason": str(exc),
                            "revision": after.revision,
                        }
                    )
                else:
                    say(
                        f"[{index}] apply_revision {operation['file']} -> "
                        f"{directory.revision} ({directory.fingerprint[:12]})"
                    )
                    results.append({"op": op, "revision": directory.revision})
            elif op == "read_old_report":
                report = workdir / operation["report"]
                if reader is None:
                    raise QualityError("read_old_report requires a reader script")
                completed = subprocess.run(
                    [sys.executable, str(reader), str(report)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                say(f"[{index}] read_old_report {operation['report']} via reader:")
                for line in completed.stdout.splitlines():
                    say(f"    {line}")
                if completed.returncode != 0:
                    say(completed.stderr)
                    raise QualityError(
                        f"reader failed on {operation['report']}: "
                        f"{completed.stderr.strip()}"
                    )
                results.append(
                    {
                        "op": op,
                        "report": str(report),
                        "reader_output": completed.stdout.splitlines(),
                    }
                )
            else:
                raise QualityError(f"unsupported operation {op!r}")
    return {"operations": results}


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operations", help="operations.jsonl path")
    parser.add_argument("--db", default="var/quality.sqlite3")
    parser.add_argument("--workdir", default="reports")
    parser.add_argument("--base-dir", default=None,
                        help="directory containing catalog/revision files "
                             "(defaults to workdir)")
    parser.add_argument("--reader", default=None,
                        help="report_reader.py for read_old_report steps")
    parser.add_argument("--keep-db", action="store_true",
                        help="do not delete an existing database first")
    args = parser.parse_args(argv)

    run_operations(
        args.operations,
        db_path=args.db,
        workdir=args.workdir,
        base_dir=args.base_dir if args.base_dir is not None else None,
        reader=args.reader,
        reset=not args.keep_db,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
