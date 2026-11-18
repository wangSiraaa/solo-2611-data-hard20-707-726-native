"""Reader already used for the v1 release journal."""

import json
from pathlib import Path
import sys


def read_releases(path):
    history = json.loads(Path(path).read_text(encoding="utf-8"))
    if history["schema"] != "kit-release/v1":
        raise ValueError("unsupported release journal")
    return {
        row["id"]: {"lot": row["lot"], "sum": sum(row["values"])}
        for row in history["releases"]
    }


if __name__ == "__main__":
    print(json.dumps(read_releases(sys.argv[1]), sort_keys=True))
