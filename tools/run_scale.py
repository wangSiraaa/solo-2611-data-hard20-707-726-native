#!/usr/bin/env python3
"""Load the scale dataset through both services and report integrity metrics.

Checks the acceptance-criteria scale (30 people, 100 grants, 200 originals,
50 shipments x 10 parts) and exercises the confirmation/review machinery at
that scale.  All data is synthetic.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from delegation import DelegationService  # noqa: E402
from delivery import DeliveryService  # noqa: E402
from tools._cli import read_json_or_jsonl  # noqa: E402
from tools.authority_adapter import DelegationAuthority  # noqa: E402

DATA = ROOT / "data" / "scale"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-dir", default=str(ROOT / "db" / "scale"))
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args(argv)

    db = Path(args.db_dir)
    if args.reset or not db.exists():
        shutil.rmtree(db, ignore_errors=True)
    db.mkdir(parents=True, exist_ok=True)

    deleg = DelegationService(db / "delegation.db")
    authority = DelegationAuthority(str(db / "delegation.db"))
    depot = DeliveryService(db / "delivery.db", authority)
    try:
        grants = read_json_or_jsonl(str(DATA / "grants.json"))
        receipts = read_json_or_jsonl(str(DATA / "receipts.jsonl"))
        shipments = read_json_or_jsonl(str(DATA / "shipments.jsonl"))
        supplements = read_json_or_jsonl(str(DATA / "receipt_supplements.jsonl"))

        deleg.ingest_grants(grants)
        deleg.ingest_receipts({"receipts": receipts})
        depot.ingest_shipments({"shipments": shipments})
        depot.ingest_supplements({"receipts": supplements})

        people = {g["issuer"] for g in grants["grants"]} | {g["agent"] for g in grants["grants"]}
        assessment = deleg.assess_all()
        valid = assessment["valid_count"]

        complete = 0
        partial = 0
        confirmed = 0
        for s in shipments:
            cov = depot.coverage(s["shipment_id"])
            if cov["complete"]:
                complete += 1
                prep = depot.prepare(s["shipment_id"])
                depot.confirm(s["shipment_id"],
                              expected_evidence_hash=prep["evidence_hash"])
                confirmed += 1
            else:
                partial += 1

        # A synthetic authority correction: revoke every root grant at day 0
        # then re-evaluate the depot; confirmed shipments should fall to review.
        roots = [g for g in grants["grants"] if g["parent_grant_id"] is None]
        corrections = []
        for g in roots:
            if g["revoked_from"] is None:
                corrections.append({**g, "revision": 2, "revoked_from": g["start"]})
        if corrections:
            deleg.ingest_grants({"grants": corrections})
        rec = depot.reconcile()
        flagged = rec["flagged_count"]
        after_valid = deleg.assess_all()["valid_count"]

        summary = {
            "people": len(people),
            "grants": len({g["grant_id"] for g in deleg.list_grants()}),
            "grant_revisions_stored": deleg.count_grants(),
            "receipt_originals": deleg.count_receipts(),
            "shipments": len(shipments),
            "parts_per_shipment": sorted({len(s["required_parts"]) for s in shipments}),
            "supplements": len(supplements),
            "initial_valid_receipts": valid,
            "shipments_complete_initial": complete,
            "shipments_partial_initial": partial,
            "shipments_confirmed_initial": confirmed,
            "authority_corrections": len(corrections),
            "valid_receipts_after_correction": after_valid,
            "shipments_flagged_needs_review": flagged,
        }
        print(json.dumps(summary, indent=2, ensure_ascii=False))

        # Assertions on the required scale.
        assert len(people) <= 30
        assert len({g["grant_id"] for g in deleg.list_grants()}) == 100
        assert deleg.count_receipts() == 200
        assert len(shipments) == 50
        assert summary["parts_per_shipment"] == [10]
        assert complete + partial == 50
        return 0
    finally:
        depot.close()
        authority.close()
        deleg.close()


if __name__ == "__main__":
    sys.exit(main())
