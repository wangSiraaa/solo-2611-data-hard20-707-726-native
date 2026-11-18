#!/usr/bin/env python3
"""Delegation service CLI.

Commands
    ingest-grants   --file grants.json
    ingest-receipts --file receipts.jsonl
    assess          [--receipt R1] [--as-of 19]
    publish         [--label REPORT1]
    read-report     REPORT1 [--verify]
    reports
    grants
    receipts
"""

from __future__ import annotations

import argparse
import sys

sys.path.insert(0, __import__("pathlib").Path(__file__).resolve().parent.parent.as_posix())

from delegation import DelegationService
from delegation.errors import DelegationError
from tools._cli import emit, emit_error, read_json_or_jsonl


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Delegation verification service")
    p.add_argument("--db", required=True, help="SQLite path for the delegation store")
    sub = p.add_subparsers(dest="command", required=True)

    g = sub.add_parser("ingest-grants")
    g.add_argument("--file", required=True)

    r = sub.add_parser("ingest-receipts")
    r.add_argument("--file", required=True)

    a = sub.add_parser("assess")
    a.add_argument("--receipt")
    a.add_argument("--as-of", type=int)

    pub = sub.add_parser("publish")
    pub.add_argument("--label")
    pub.add_argument("--save-as", dest="save_as")

    rd = sub.add_parser("read-report")
    rd.add_argument("report_id")
    rd.add_argument("--verify", action="store_true")

    sub.add_parser("reports")
    sub.add_parser("grants")
    sub.add_parser("receipts")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        with DelegationService(args.db) as svc:
            if args.command == "ingest-grants":
                emit(svc.ingest_grants(read_json_or_jsonl(args.file)))
            elif args.command == "ingest-receipts":
                emit(svc.ingest_receipts({"receipts": read_json_or_jsonl(args.file)}))
            elif args.command == "assess":
                if args.receipt:
                    emit(svc.assess_receipt(args.receipt, as_of=args.as_of))
                else:
                    emit(svc.assess_all(as_of=args.as_of))
            elif args.command == "publish":
                report = svc.publish(label=args.save_as or args.label)
                emit(report)
            elif args.command == "read-report":
                emit(svc.verify_report(args.report_id) if args.verify
                     else svc.get_report(args.report_id))
            elif args.command == "reports":
                emit(svc.list_reports())
            elif args.command == "grants":
                emit(svc.list_grants())
            elif args.command == "receipts":
                emit(svc.list_receipts())
    except DelegationError as exc:
        emit_error(str(exc), type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
