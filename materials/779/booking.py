"""Reserve export time against the v1 route catalogue and confirmed journal."""

import argparse
import json
from pathlib import Path
import tempfile

from route_catalog import load_catalog


def atomic_json(path, payload):
    path = Path(path)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as file:
        json.dump(payload, file, indent=2)
        file.write("\n")
        staged = Path(file.name)
    staged.replace(path)


def check_reservation(catalog, rows, request):
    start, end, rate = (request[key] for key in ("start", "end", "rate"))
    route = request["route"]
    if (
        type(start) is not int
        or type(end) is not int
        or type(rate) is not int
        or not 0 <= start < end <= 1440
        or rate <= 0
        or route not in catalog
    ):
        raise ValueError("invalid export reservation")

    overlaps = [
        row
        for row in rows
        if row["route"] == route and row["start"] < end and start < row["end"]
    ]
    boundaries = {start, end}
    for row in overlaps:
        boundaries.update((max(start, row["start"]), min(end, row["end"])))
    ordered = sorted(boundaries)
    for left, right in zip(ordered, ordered[1:]):
        combined = rate + sum(
            row["rate"] for row in overlaps if row["start"] <= left < row["end"]
        )
        if combined > catalog[route]:
            return {
                "reason": "route_limit",
                "interval": [left, right],
                "rate": combined,
                "limit": catalog[route],
            }
    return None


def confirm(catalog, journal_path, request):
    path = Path(journal_path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc["schema"] != "export-booking/v1":
        raise ValueError("unsupported booking journal")
    if set(request) != {"id", "route", "start", "end", "rate"}:
        raise ValueError("invalid reservation fields")
    if not isinstance(request["id"], str) or not request["id"]:
        raise ValueError("reservation id must be nonempty")

    rows = doc["reservations"]
    existing = next((row for row in rows if row["id"] == request["id"]), None)
    if existing is not None:
        if existing != request:
            return {"accepted": False, "id": request["id"], "reason": "id_conflict"}
        return {"accepted": True, "id": request["id"], "replayed": True}

    refusal = check_reservation(catalog, rows, request)
    if refusal is not None:
        return {"accepted": False, "id": request["id"], **refusal}

    atomic_json(path, {**doc, "reservations": [*rows, request]})
    return {"accepted": True, "id": request["id"], "replayed": False}


def main():
    parser = argparse.ArgumentParser(description="Confirm an export reservation")
    parser.add_argument("catalog")
    parser.add_argument("journal")
    parser.add_argument("request")
    args = parser.parse_args()
    request = json.loads(Path(args.request).read_text(encoding="utf-8"))
    result = confirm(load_catalog(args.catalog), args.journal, request)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["accepted"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
