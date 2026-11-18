"""Load one revision of the exact rail-position catalogue."""

import json
from pathlib import Path


def snapshot(file):
    value = json.loads(Path(file).read_text(encoding="utf-8"))
    revision = value["revision"]
    if type(revision) is not int or revision < 1:
        raise ValueError("invalid catalogue revision")
    positions = {}
    for row in value["positions"]:
        sample = row["sample"]
        mm = row["mm"]
        if not isinstance(sample, str) or not sample or sample in positions or type(mm) is not int:
            raise ValueError("invalid or duplicate exact position")
        positions[sample] = mm
    return {"revision": revision, "positions": positions}
