"""Independent normalizer for the existing global-offset catalogue."""

import argparse
import json
from pathlib import Path


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_catalog(path):
    document = read_json(path)
    if document["schema"] != "bench-calibration/v1":
        raise ValueError("unsupported calibration schema")
    if not isinstance(document["revision"], str) or not document["revision"]:
        raise ValueError("invalid catalogue revision")
    rules = {}
    used_ids = set()
    for entry in document["defaults"]:
        station, calibration_id, offset = (
            entry["station"], entry["calibration_id"], entry["offset"]
        )
        if station not in {"left", "right"} or station in rules:
            raise ValueError("invalid or repeated station")
        if not isinstance(calibration_id, str) or not calibration_id or calibration_id in used_ids:
            raise ValueError("invalid or repeated calibration id")
        if type(offset) is not int:
            raise ValueError("invalid offset")
        used_ids.add(calibration_id)
        rules[station] = {"calibration_id": calibration_id, "offset": offset}
    if set(rules) != {"left", "right"}:
        raise ValueError("both stations require defaults")
    return {"revision": document["revision"], "defaults": rules}


def index_samples(path):
    rows = read_json(path)["samples"]
    by_id = {}
    for row in rows:
        if (
            row["id"] in by_id
            or row["station"] not in {"left", "right"}
            or type(row["minute"]) is not int
            or not 0 <= row["minute"] < 1440
            or type(row["raw"]) is not int
        ):
            raise ValueError("invalid or repeated sample")
        by_id[row["id"]] = row
    return by_id


def normalize(catalog, sample):
    rule = catalog["defaults"][sample["station"]]
    return {
        **sample,
        "value": sample["raw"] + rule["offset"],
        "calibration_id": rule["calibration_id"],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Classify one synthetic sample")
    parser.add_argument("catalog")
    parser.add_argument("samples")
    parser.add_argument("sample_id")
    args = parser.parse_args()
    catalog = load_catalog(args.catalog)
    sample = index_samples(args.samples)[args.sample_id]
    print(json.dumps(normalize(catalog, sample), sort_keys=True))
