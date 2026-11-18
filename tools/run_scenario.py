#!/usr/bin/env python3
"""End-to-end scenario driver.

Replays materials/756 (grants, receipts, operations) through the delegation
service, then the published shipment S1 + receipt supplements through the
delivery depot.  Every input is read from files under inputs/; nothing is
invented in code.

Run:
    python3 tools/run_scenario.py --reset

The script prints a compact transcript and writes JSON artefacts under
artifacts/.  Read them back with:
    python3 tools/delegation_cli.py --db db/delegation.db read-report REPORT1
    python3 tools/delivery_cli.py --db db/delivery.db --delegation-db db/delegation.db \
        confirmation DEL1
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

INPUTS = ROOT / "inputs"
DB = ROOT / "db"
ART = ROOT / "artifacts"


def dump(name: str, obj) -> None:
    path = ART / name
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_lines(path: Path) -> list[dict]:
    return read_json_or_jsonl(str(path))


def summarize_report(report: dict) -> None:
    rid = report.get("report_id", "assessment")
    print(f"  {rid}: valid={report['valid_count']} "
          f"invalid={report['invalid_count']}")
    for r in report["receipts"]:
        status = "VALID" if r["valid"] else "invalid"
        print(f"    {r['receipt_id']}: {status}"
              + ("" if r["valid"] else f"  <- {r['reason']}"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="wipe db/ and artifacts/ first")
    args = ap.parse_args()

    if args.reset:
        shutil.rmtree(DB, ignore_errors=True)
        shutil.rmtree(ART, ignore_errors=True)
    DB.mkdir(parents=True, exist_ok=True)
    ART.mkdir(parents=True, exist_ok=True)

    delegation_db = DB / "delegation.db"
    delivery_db = DB / "delivery.db"

    transcript: list[dict] = []

    def record(step: dict) -> None:
        transcript.append(step)

    # Both services stay open together so the scenario can interleave the
    # documented operation sequence:
    #   delegation initial + REPORT1
    #   delivery initial coverage + DEL1            (G1 revoked_from = 12)
    #   delegation revise G1 (12 -> 9) + REPORT2
    #   delivery reconcile -> NEEDS_REVIEW          (R1 no longer proof)
    #   deliver new original R4 + DEL2
    delegation = DelegationService(delegation_db)
    authority = DelegationAuthority(str(delegation_db))
    depot = DeliveryService(delivery_db, authority)
    try:
        # ============================================ delegation initial ===
        print("[delegation] ingest grants")
        grants_doc = read_json_or_jsonl(str(INPUTS / "grants.json"))
        res = delegation.ingest_grants(grants_doc)
        record({"step": "ingest_grants_initial", "result": res})

        print("[delegation] ingest receipts")
        receipts = read_lines(INPUTS / "receipts.jsonl")
        res = delegation.ingest_receipts({"receipts": receipts})
        record({"step": "ingest_receipts_initial", "result": res})

        print("[delegation] initial assessment")
        before = delegation.assess_all()
        summarize_report(before)
        record({"step": "assess_initial", "result": before})
        dump("delegation_initial.json", before)

        report1 = delegation.publish(label="REPORT1")
        print(f"  published {report1['report_id']} (valid={report1['valid_count']})")
        record({"step": "publish_report1", "result": report1["report_id"]})
        dump("REPORT1.json", report1)

        # =============================================== delivery initial ===
        print("[delivery] ingest shipment S1")
        res = depot.ingest_shipments(
            {"shipments": read_lines(INPUTS / "shipments.jsonl")}
        )
        record({"step": "ingest_shipment", "result": res})

        print("[delivery] ingest supplements (R1, R2)")
        sup = read_lines(INPUTS / "receipt_supplements.jsonl")
        res = depot.ingest_supplements({"receipts": sup})
        record({"step": "ingest_supplements_initial", "result": res})

        cov = depot.coverage("S1")
        print(f"  coverage complete={cov['complete']} covered={cov['covered_parts']} "
              f"missing={cov['missing_parts']}")
        dump("coverage_initial.json", cov)
        record({"step": "coverage_initial", "result": {
            "complete": cov["complete"], "covered": cov["covered_parts"],
            "missing": cov["missing_parts"], "covered_by": cov["covered_by"],
        }})

        # Duplicate import of R1: still only two parts.
        dup = read_lines(INPUTS / "receipt_supplements.jsonl")[:1]
        res = depot.ingest_supplements({"receipts": dup})
        cov_dup = depot.coverage("S1")
        print(f"[delivery] replay R1: replayed={res['replayed']}, "
              f"covered={cov_dup['covered_parts']}")
        assert cov_dup["covered_parts"] == cov["covered_parts"]
        record({"step": "replay_R1", "result": res, "covered": cov_dup["covered_parts"]})

        print("[delivery] prepare + confirm DEL1")
        prep = depot.prepare("S1")
        del1 = depot.confirm("S1", expected_evidence_hash=prep["evidence_hash"])
        print(f"  {del1['confirmation_id']} confirmed parts={del1['parts']}")
        dump("DEL1.json", del1)
        record({"step": "confirm_DEL1", "result": del1["confirmation_id"]})
        print(f"  status: {depot.confirmation_status('S1')['status']}")

        # ==================================== delegation correction + REPORT2 =
        print("[delegation] revise G1 revocation 12 -> 9")
        operations = read_lines(INPUTS / "operations_756.jsonl")
        for op in operations:
            if op.get("operation") == "revise_grant":
                delegation.ingest_grants({"grants": [op["grant"]]})
                record({"step": "revise_G1", "grant": op["grant"]})

        after = delegation.assess_all()
        summarize_report(after)
        report2 = delegation.publish(label="REPORT2")
        print(f"  published {report2['report_id']} (valid={report2['valid_count']})")
        record({"step": "publish_report2", "result": report2["report_id"]})
        dump("REPORT2.json", report2)

        hist = delegation.get_report("REPORT1")
        verify1 = delegation.verify_report("REPORT1")
        verify2 = delegation.verify_report("REPORT2")
        print(f"[delegation] reopen REPORT1: valid={hist['valid_count']}, "
              f"intact={verify1['intact']}, current={verify1['is_current_grants']}")
        record({"step": "reopen_historical", "report1": verify1, "report2": verify2})
        dump("delegation_reopen.json", {"REPORT1": verify1, "REPORT2": verify2})

        # ================================= delivery reconcile after correction =
        print("[delivery] reconcile after G1 correction")
        rec = depot.reconcile(["S1"])
        st = depot.confirmation_status("S1")
        del1_again = depot.get_confirmation("DEL1")
        print(f"  reconcile flagged={rec['flagged_count']}, new status={st['status']}")
        print(f"  DEL1 unchanged: {del1_again == del1}")
        record({"step": "reconcile_after_revocation",
                "flagged": rec["flagged_count"], "status": st["status"],
                "del1_unchanged": del1_again == del1})
        dump("status_needs_review.json", st)

        cov_nr = depot.coverage("S1")
        print(f"  coverage now complete={cov_nr['complete']} "
              f"missing={cov_nr['missing_parts']}")
        record({"step": "coverage_after_revocation", "result": {
            "complete": cov_nr["complete"], "missing": cov_nr["missing_parts"]}})

        # ======================================= new self-signed original R4 =
        print("[delegation] ingest extra original R4")
        r4 = read_lines(INPUTS / "receipts_extra.jsonl")
        res4 = delegation.ingest_receipts({"receipts": r4})
        print(f"  new={res4['new']}")
        record({"step": "ingest_R4_original", "result": res4})

        print("[delivery] ingest supplement R4 (self-signed, BX1+BX2)")
        r4_sup = read_lines(INPUTS / "receipt_supplements_extra.jsonl")
        res = depot.ingest_supplements({"receipts": r4_sup})
        cov_r4 = depot.coverage("S1")
        print(f"  coverage complete={cov_r4['complete']} covered={cov_r4['covered_parts']}")
        record({"step": "ingest_R4_supplement", "result": res,
                "coverage_complete": cov_r4["complete"]})

        print("[delivery] prepare + confirm DEL2")
        prep2 = depot.prepare("S1")
        del2 = depot.confirm("S1", expected_evidence_hash=prep2["evidence_hash"])
        print(f"  {del2['confirmation_id']} confirmed; previous="
              f"{del2['previous_confirmation_id']}")
        st2 = depot.confirmation_status("S1")
        print(f"  status={st2['status']}")
        record({"step": "confirm_DEL2", "result": {
            "confirmation_id": del2["confirmation_id"],
            "previous": del2["previous_confirmation_id"],
            "status": st2["status"],
        }})
        dump("DEL2.json", del2)
        dump("status_final.json", st2)

        assert depot.get_confirmation("DEL1") == del1
        hist_conf = depot.confirmations_for("S1")
        print(f"[delivery] confirmation lineage: "
              f"{[c['confirmation_id'] for c in hist_conf]}")
        dump("confirmations_S1.json", hist_conf)
        record({"step": "lineage", "ids": [c["confirmation_id"] for c in hist_conf]})
    finally:
        depot.close()
        authority.close()
        delegation.close()

    dump("transcript.json", transcript)
    print(f"\nartefacts written to {ART}; transcript has {len(transcript)} steps")
    return 0


if __name__ == "__main__":
    sys.exit(main())
