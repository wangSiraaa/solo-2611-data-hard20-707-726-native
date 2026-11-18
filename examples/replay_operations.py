"""Walk materials/707 through the full revision flow using the public API.

Run: python3 examples/replay_operations.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from address_delivery import AddressService  # noqa: E402

MATERIALS = ROOT / "materials" / "707"


def show(label: str, value) -> None:
    print(f"\n== {label} ==")
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main() -> int:
    svc = AddressService.from_materials(MATERIALS)
    try:
        batch = svc.import_csv(MATERIALS / "addresses.csv")
        show("r1 import", [
            {"parcel": r.parcel_id, "outcome": r.outcome, "station": r.station_id}
            for r in batch.rows
        ])

        version = svc.apply_revision(MATERIALS / "revision.json")
        show("revision r2", {"catalog_version": version})

        show("retry P1 P3 P4", [
            {"parcel": r.parcel_id, "outcome": r.outcome, "station": r.station_id}
            for r in svc.retry(["P1", "P3", "P4"])
        ])

        replay = svc.replay_submitted("P2")
        show("replay P2", {
            "outcome": replay.outcome,
            "street": replay.job["street_id"],
            "station": replay.job["station_id"],
            "catalog_revision": replay.job["catalog_revision"],
        })

        new = svc.submit_new("P7", "C1", "西", "东路", 2)
        show("new P7", {"outcome": new.outcome, "message": new.message})

        show("final jobs", [
            {"parcel": j["parcel_id"], "street": j["street_id"],
             "station": j["station_id"], "version": j["catalog_version"]}
            for j in svc.delivery.list_jobs()
        ])
        return 0
    finally:
        svc.close()


if __name__ == "__main__":
    raise SystemExit(main())
