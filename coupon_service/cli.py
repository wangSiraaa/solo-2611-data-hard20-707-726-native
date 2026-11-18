"""Command-line entry point.

    python -m coupon_service.cli snapshot   DB
    python -m coupon_service.cli qualify    DB REQUEST.json
    python -m coupon_service.cli redeem     DB REQUEST.json
    python -m coupon_service.cli cancel     DB REQUEST.json
    python -m coupon_service.cli run        DB OPERATIONS.jsonl [--out OUT.jsonl]

``snapshot`` and ``qualify`` open the database read-only; ``run``/``redeem``/
``cancel`` require a writable working copy (never the material file).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import db, ledger
from .models import OrderLine
from .operations import run_operations
from .snapshot import snapshot


def _load(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _emit(value: Any) -> None:
    json.dump(value, sys.stdout, ensure_ascii=False, sort_keys=True, indent=2)
    sys.stdout.write("\n")


def _cmd_snapshot(args: argparse.Namespace) -> int:
    _emit(snapshot(args.db))
    return 0


def _cmd_qualify(args: argparse.Namespace) -> int:
    request = _load(args.request)
    con = db.connect(args.db, readonly=True)
    try:
        lines = tuple(
            OrderLine(
                sku=line["sku"], qty=line["qty"], unit_price=line["unit_price"]
            )
            for line in request.get("lines", [])
        )
        evaluation = ledger.preflight_redeem(
            con, request.get("user_id"), lines, tuple(request.get("coupon_ids", []))
        )
    finally:
        con.close()
    if evaluation.ok and evaluation.plan is not None:
        _emit(
            {
                "entry": "qualification",
                "eligible": True,
                "plan": evaluation.plan.as_dict(),
            }
        )
    else:
        _emit(
            {
                "entry": "qualification",
                "eligible": False,
                "code": evaluation.primary,
                "issues": evaluation.reason_dicts(),
            }
        )
    return 0


def _cmd_redeem(args: argparse.Namespace) -> int:
    request = _load(args.request)
    request_id = request.get("request_id")
    con = db.connect(args.db)
    try:
        db.ensure_schema(con)
        response = ledger.redeem(con, request_id, request).as_dict()
    finally:
        con.close()
    _emit(response)
    return 0


def _cmd_cancel(args: argparse.Namespace) -> int:
    request = _load(args.request)
    request_id = request.get("request_id")
    con = db.connect(args.db)
    try:
        db.ensure_schema(con)
        response = ledger.cancel(con, request_id, request).as_dict()
    finally:
        con.close()
    _emit(response)
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    outcomes = run_operations(args.db, args.operations, output_path=args.out)
    for outcome in outcomes:
        _emit(outcome.as_dict())
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coupon_service")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("snapshot", help="read-only snapshot")
    p.add_argument("db")
    p.set_defaults(func=_cmd_snapshot)

    p = sub.add_parser("qualify", help="read-only qualification preflight")
    p.add_argument("db")
    p.add_argument("request")
    p.set_defaults(func=_cmd_qualify)

    p = sub.add_parser("redeem", help="apply a redeem request")
    p.add_argument("db")
    p.add_argument("request")
    p.set_defaults(func=_cmd_redeem)

    p = sub.add_parser("cancel", help="apply a cancel request")
    p.add_argument("db")
    p.add_argument("request")
    p.set_defaults(func=_cmd_cancel)

    p = sub.add_parser("run", help="run an operations.jsonl file")
    p.add_argument("db")
    p.add_argument("operations")
    p.add_argument("--out", default=None)
    p.set_defaults(func=_cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
