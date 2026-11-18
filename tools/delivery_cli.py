#!/usr/bin/env python3
"""Delivery depot CLI.

Commands
    ingest-shipments  --file shipments.jsonl
    ingest-supplements --file receipt_supplements.jsonl
    coverage          SHIPMENT
    prepare           SHIPMENT
    confirm           SHIPMENT [--expect-evidence-hash H]
    status            SHIPMENT
    reconcile         [--shipment S1 ...]
    confirmation      CONFIRMATION_ID
    confirmations     SHIPMENT
    review-history    SHIPMENT
"""

from __future__ import annotations

import argparse
import sys

sys.path.insert(0, __import__("pathlib").Path(__file__).resolve().parent.parent.as_posix())

from delivery import DeliveryService
from delivery.errors import DeliveryError
from tools._cli import emit, emit_error, read_json_or_jsonl
from tools.authority_adapter import DelegationAuthority


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Delivery confirmation depot")
    p.add_argument("--db", required=True, help="SQLite path for the delivery store")
    p.add_argument("--delegation-db", required=True, help="SQLite path for the delegation store")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("ingest-shipments")
    s.add_argument("--file", required=True)

    sup = sub.add_parser("ingest-supplements")
    sup.add_argument("--file", required=True)

    cov = sub.add_parser("coverage")
    cov.add_argument("shipment_id")

    prep = sub.add_parser("prepare")
    prep.add_argument("shipment_id")

    conf = sub.add_parser("confirm")
    conf.add_argument("shipment_id")
    conf.add_argument("--expect-evidence-hash")

    st = sub.add_parser("status")
    st.add_argument("shipment_id")

    rec = sub.add_parser("reconcile")
    rec.add_argument("--shipment", action="append", dest="shipments")

    get = sub.add_parser("confirmation")
    get.add_argument("confirmation_id")

    lst = sub.add_parser("confirmations")
    lst.add_argument("shipment_id")

    hist = sub.add_parser("review-history")
    hist.add_argument("shipment_id")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    authority = DelegationAuthority(args.delegation_db)
    try:
        with DeliveryService(args.db, authority) as svc:
            if args.command == "ingest-shipments":
                emit(svc.ingest_shipments({"shipments": read_json_or_jsonl(args.file)}))
            elif args.command == "ingest-supplements":
                emit(svc.ingest_supplements({"receipts": read_json_or_jsonl(args.file)}))
            elif args.command == "coverage":
                emit(svc.coverage(args.shipment_id))
            elif args.command == "prepare":
                emit(svc.prepare(args.shipment_id))
            elif args.command == "confirm":
                emit(svc.confirm(args.shipment_id, args.expect_evidence_hash))
            elif args.command == "status":
                emit(svc.confirmation_status(args.shipment_id))
            elif args.command == "reconcile":
                emit(svc.reconcile(args.shipments))
            elif args.command == "confirmation":
                emit(svc.get_confirmation(args.confirmation_id))
            elif args.command == "confirmations":
                emit(svc.confirmations_for(args.shipment_id))
            elif args.command == "review-history":
                emit(svc.review_history(args.shipment_id))
    except DeliveryError as exc:
        emit_error(str(exc), type(exc).__name__)
        return 1
    finally:
        authority.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
