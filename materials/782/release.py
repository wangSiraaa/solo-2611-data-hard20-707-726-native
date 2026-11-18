"""Read the current source whose legacy contract guarantees order."""
import json
import sys

from catalog import read


def release(path):
    source = read(path)
    current = source["start"]
    steps = []
    for row in source["records"]:
        if row["from"] != current:
            raise ValueError("disconnected ordered list")
        current = row["to"]
        steps.append(row["edge_id"])
    if current != source["end"]:
        raise ValueError("wrong endpoint")
    return {"start": source["start"], "end": current, "steps": steps}


if __name__ == "__main__":
    print(json.dumps(release(sys.argv[1])))
