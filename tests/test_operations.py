"""Play materials/707/operations.jsonl against a fresh service.

Each operation JSON line is interpreted with the semantics documented in the
task: import/attempt a CSV, apply a revision, retry named failed parcels,
replay a committed parcel, and submit a new parcel.  The expectations assert the
hand-worked outcomes from the materials README.
"""
from __future__ import annotations

import json
import unittest

from support import MATERIALS, MaterialServiceCase

from address_delivery.delivery import (
    FAILED_AMBIGUOUS_ADDRESS,
    FAILED_STATION_AMBIGUOUS,
    FAILED_STREET_INACTIVE,
    REPLAYED,
    SUBMITTED,
)


def run_operations(service) -> dict:
    """Execute operations.jsonl, returning the per-step results."""
    log: dict[str, list] = {}
    lines = (MATERIALS / "operations.jsonl").read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines, 1):
        if not line.strip():
            continue
        op = json.loads(line)
        kind = op["op"]
        if kind == "import_and_attempt":
            batch = service.import_csv(MATERIALS / op["file"])
            log[f"{index}:import"] = batch.as_dict()
        elif kind == "apply_revision":
            log[f"{index}:revision"] = service.apply_revision(MATERIALS / op["file"])
        elif kind == "retry":
            log[f"{index}:retry"] = [r.as_dict() for r in service.retry(op["parcel_ids"])]
        elif kind == "replay_submitted":
            result = service.replay_submitted(op["parcel_id"])
            log[f"{index}:replay"] = result.as_dict()
        elif kind == "new_parcel":
            result = service.submit_new(
                op["parcel_id"],
                op["city_id"],
                op["district_text"],
                op["street_text"],
                op["house_no"],
            )
            log[f"{index}:new"] = result.as_dict()
        else:
            raise AssertionError(f"unknown operation {kind}")
    return log


class OperationsPlaybackTests(MaterialServiceCase):
    def test_full_operations_flow(self):
        log = run_operations(self.svc)

        initial = {r["parcel_id"]: r["outcome"] for r in log["1:import"]["rows"]}
        self.assertEqual(initial["P1"], FAILED_AMBIGUOUS_ADDRESS)
        self.assertEqual(initial["P2"], SUBMITTED)
        self.assertEqual(initial["P3"], FAILED_STATION_AMBIGUOUS)
        self.assertEqual(initial["P5"], SUBMITTED)
        self.assertEqual(initial["P6"], SUBMITTED)

        self.assertEqual(log["2:revision"], 2)

        retry = {r["parcel_id"]: r for r in log["3:retry"]}
        self.assertEqual(retry["P1"]["outcome"], FAILED_AMBIGUOUS_ADDRESS)
        self.assertEqual(retry["P3"]["outcome"], SUBMITTED)
        self.assertEqual(retry["P3"]["station_id"], "A")
        self.assertEqual(retry["P4"]["outcome"], SUBMITTED)
        self.assertEqual(retry["P4"]["job"]["street_id"], "S1")

        self.assertEqual(log["4:replay"]["outcome"], REPLAYED)
        self.assertEqual(log["4:replay"]["job"]["street_id"], "S3")
        self.assertEqual(log["4:replay"]["job"]["catalog_revision"], "r1")

        self.assertEqual(log["5:new"]["outcome"], FAILED_STREET_INACTIVE)

        # Final committed set: P2,P5,P6 at r1 and P3,P4 at r2. P1/P7 never commit.
        jobs = {j["parcel_id"]: j for j in self.svc.delivery.list_jobs()}
        self.assertEqual(set(jobs), {"P2", "P3", "P4", "P5", "P6"})
        self.assertEqual(jobs["P2"]["catalog_version"], 1)
        self.assertEqual(jobs["P3"]["catalog_version"], 2)
        self.assertEqual(jobs["P4"]["catalog_version"], 2)

    def test_explain_reports_original_candidates_and_destination(self):
        run_operations(self.svc)
        detail = self.svc.explain("P3")
        # Original text is the CSV text, not the resolved street.
        self.assertEqual(detail["original"]["street_text"], "东路")
        self.assertEqual(detail["original"]["source_row"], 4)
        # Current resolution is unique after K3 removal.
        self.assertEqual(detail["current_resolution"]["status"], "unique")
        self.assertEqual(detail["final_destination"]["street_id"], "S1")
        self.assertEqual(detail["final_destination"]["station_id"], "A")
        self.assertEqual(len(detail["attempts"]), 2)

    def test_explain_p1_still_ambiguous_with_both_candidates(self):
        run_operations(self.svc)
        detail = self.svc.explain("P1")
        self.assertIsNone(detail["final_destination"])
        self.assertEqual(detail["current_resolution"]["status"], "ambiguous")
        self.assertEqual(
            {c["street_id"] for c in detail["current_resolution"]["candidates"]},
            {"S1", "S2"},
        )


if __name__ == "__main__":
    unittest.main()
