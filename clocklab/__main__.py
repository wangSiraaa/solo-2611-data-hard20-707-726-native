"""Command-line entry point.

    python -m clocklab run --db ledger.sqlite --ops materials/727/operations.jsonl
    python -m clocklab point --db ledger.sqlite --local 330 --zone Z
    python -m clocklab summary --db ledger.sqlite --from 180 --to 240
    python -m clocklab session --db ledger.sqlite --session J1
    python -m clocklab evaluation --db ledger.sqlite --evaluation E1
    python -m clocklab revisions --db ledger.sqlite
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .clock import ClockCatalogue, ClockError
from .operations import OperationRunner, load_operations
from .storage import Ledger, LedgerError


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _open(args: argparse.Namespace) -> Ledger:
    return Ledger.open(args.db)


def cmd_load_clock(args: argparse.Namespace) -> int:
    ledger = _open(args)
    try:
        document = json.loads(Path(args.file).read_text(encoding="utf-8"))
        catalogue = ClockCatalogue.from_document(document)
        ledger.install_catalogue(catalogue)
        _print({"status": "ok", "active_revision": ledger.active_revision()})
        return 0
    finally:
        ledger.close()


def cmd_run(args: argparse.Namespace) -> int:
    ledger = _open(args)
    try:
        operations = load_operations(args.ops)
        runner = OperationRunner(ledger, args.base_dir or Path(args.ops).parent)
        records = runner.run(operations)
        _print({"status": "ok", "steps": records})
        return 0
    finally:
        ledger.close()


def cmd_point(args: argparse.Namespace) -> int:
    ledger = _open(args)
    try:
        _print(
            ledger.resolve_point(
                local=args.local,
                zone_id=args.zone,
                revision=args.revision,
                occurrence=args.occurrence,
            )
        )
        return 0
    finally:
        ledger.close()


def cmd_summary(args: argparse.Namespace) -> int:
    ledger = _open(args)
    try:
        _print(
            ledger.summarize(
                utc_from=args.start,
                utc_to=args.end,
                zone_id=args.zone,
                session_id=args.session_id,
                group_by=args.group_by,
                revision=args.revision,
                latest_per_session=not args.all_evaluations,
            )
        )
        return 0
    finally:
        ledger.close()


def cmd_session(args: argparse.Namespace) -> int:
    ledger = _open(args)
    try:
        _print(ledger.get_session(args.session_id))
        return 0
    finally:
        ledger.close()


def cmd_sessions(args: argparse.Namespace) -> int:
    ledger = _open(args)
    try:
        _print({"sessions": ledger.list_sessions()})
        return 0
    finally:
        ledger.close()


def cmd_evaluation(args: argparse.Namespace) -> int:
    ledger = _open(args)
    try:
        _print(ledger.read_evaluation(args.evaluation))
        return 0
    finally:
        ledger.close()


def cmd_revisions(args: argparse.Namespace) -> int:
    ledger = _open(args)
    try:
        _print({"revisions": ledger.list_revisions()})
        return 0
    finally:
        ledger.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="clocklab")
    sub = parser.add_subparsers(dest="command", required=True)

    load = sub.add_parser("load-clock", help="store an initial clock revision")
    load.add_argument("--db", required=True)
    load.add_argument("--file", required=True)
    load.set_defaults(func=cmd_load_clock)

    run = sub.add_parser("run", help="apply an operation file")
    run.add_argument("--db", required=True)
    run.add_argument("--ops", required=True)
    run.add_argument("--base-dir", default=None)
    run.set_defaults(func=cmd_run)

    point = sub.add_parser("point", help="local -> candidate UTC query")
    point.add_argument("--db", required=True)
    point.add_argument("--local", type=int, required=True)
    point.add_argument("--zone", default="Z")
    point.add_argument("--revision", default=None)
    point.add_argument("--occurrence", choices=("earlier", "later"), default=None)
    point.set_defaults(func=cmd_point)

    summary = sub.add_parser("summary", help="settle over a real UTC interval")
    summary.add_argument("--db", required=True)
    summary.add_argument("--from", dest="start", type=int, required=True)
    summary.add_argument("--to", dest="end", type=int, required=True)
    summary.add_argument("--zone", default=None)
    summary.add_argument("--session-id", default=None)
    summary.add_argument("--revision", default=None)
    summary.add_argument("--group-by", choices=("session", "zone", "revision"),
                         default="session")
    summary.add_argument(
        "--all-evaluations", action="store_true",
        help="count every stored successful fact instead of the newest per session",
    )
    summary.set_defaults(func=cmd_summary)

    session = sub.add_parser("session", help="one session and its evaluations")
    session.add_argument("--db", required=True)
    session.add_argument("--session-id", required=True)
    session.set_defaults(func=cmd_session)

    sessions = sub.add_parser("sessions", help="list sessions")
    sessions.add_argument("--db", required=True)
    sessions.set_defaults(func=cmd_sessions)

    evaluation = sub.add_parser("evaluation", help="read a stored evaluation")
    evaluation.add_argument("--db", required=True)
    evaluation.add_argument("--evaluation", required=True)
    evaluation.set_defaults(func=cmd_evaluation)

    revisions = sub.add_parser("revisions", help="list stored revisions")
    revisions.add_argument("--db", required=True)
    revisions.set_defaults(func=cmd_revisions)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ClockError, LedgerError, ValueError) as exc:
        _print({"status": "error", "error_type": type(exc).__name__,
                "message": str(exc)})
        return 2


if __name__ == "__main__":
    sys.exit(main())
