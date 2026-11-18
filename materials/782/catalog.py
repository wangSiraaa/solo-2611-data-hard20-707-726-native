"""Read registered transition records from the legacy source."""
import json
from pathlib import Path


def read(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    names = set(data["states"])
    seen = set()
    for row in data["records"]:
        key = row["edge_id"]
        if key in seen or row["from"] not in names or row["to"] not in names:
            raise ValueError("invalid transition")
        seen.add(key)
    return data
