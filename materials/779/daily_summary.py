"""Existing turn report. Its v1 journal contract is already used downstream."""

import json
from pathlib import Path
import sys


def summarize(path):
    history = json.loads(Path(path).read_text(encoding="utf-8"))
    if history["schema"] != "export-booking/v1":
        raise ValueError("unsupported booking journal")
    rows = history["reservations"]
    return {
        "confirmed": len(rows),
        "rate_minutes": sum((row["end"] - row["start"]) * row["rate"] for row in rows),
        "ids": [row["id"] for row in rows],
    }


if __name__ == "__main__":
    print(json.dumps(summarize(sys.argv[1]), sort_keys=True))
