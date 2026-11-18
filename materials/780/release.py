"""Existing two-bench kit preview and release journal consumer."""

import argparse
import json
from pathlib import Path
import tempfile

from calibration import index_samples, load_catalog, normalize, read_json


def atomic_json(path, payload):
    path = Path(path)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as file:
        json.dump(payload, file, indent=2)
        file.write("\n")
        staged = Path(file.name)
    staged.replace(path)


def prepare(catalog, samples, request):
    if set(request) != {"id", "members"} or not isinstance(request["id"], str) or not request["id"]:
        raise ValueError("invalid release request")
    if len(request["members"]) != 2 or len(set(request["members"])) != 2:
        raise ValueError("two different samples are required")

    normalized = [normalize(catalog, samples[key]) for key in request["members"]]
    left, right = sorted(normalized, key=lambda sample: sample["station"])
    if left["station"] != "left" or right["station"] != "right" or left["lot"] != right["lot"]:
        return {"accepted": False, "id": request["id"], "reason": "wrong_pair"}
    if abs(left["value"] - right["value"]) > 2:
        return {"accepted": False, "id": request["id"], "reason": "pair_gap"}
    return {
        "accepted": True,
        "schema": "kit-plan/v1",
        "request": request,
        "catalog_revision": catalog["revision"],
        "normalized": [left, right],
    }


def confirm(catalog, journal_path, plan):
    if plan.get("schema") != "kit-plan/v1" or not plan.get("accepted"):
        raise ValueError("an accepted preview is required")
    request = plan["request"]
    prior = read_json(journal_path)
    if prior["schema"] != "kit-release/v1":
        raise ValueError("unsupported release journal")
    rows = prior["releases"]
    existing = next((row for row in rows if row["id"] == request["id"]), None)
    if existing is not None:
        if existing["members"] != request["members"]:
            return {"accepted": False, "id": request["id"], "reason": "id_conflict"}
        return {"accepted": True, "replayed": True, "receipt": existing}

    if plan["catalog_revision"] != catalog["revision"]:
        return {"accepted": False, "id": request["id"], "reason": "stale_preview"}
    left, right = plan["normalized"]
    row = {
        "id": request["id"],
        "lot": left["lot"],
        "members": request["members"],
        "values": [left["value"], right["value"]],
        "catalog_revision": catalog["revision"],
    }
    atomic_json(journal_path, {**prior, "releases": [*rows, row]})
    return {"accepted": True, "replayed": False, "receipt": row}


def main():
    parser = argparse.ArgumentParser(description="Preview or confirm a kit release")
    parser.add_argument("action", choices=("prepare", "confirm"))
    parser.add_argument("catalog")
    parser.add_argument("samples")
    parser.add_argument("input")
    parser.add_argument("--output")
    args = parser.parse_args()
    if not args.output:
        parser.error("--output is required")
    catalog = load_catalog(args.catalog)
    document = read_json(args.input)
    if args.action == "prepare":
        result = prepare(catalog, index_samples(args.samples), document)
        if result["accepted"]:
            atomic_json(args.output, result)
    else:
        result = confirm(catalog, args.output, document)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["accepted"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
