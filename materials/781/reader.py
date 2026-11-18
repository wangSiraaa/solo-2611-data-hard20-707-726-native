"""Print a resume position using the original one-prefix format."""
import json
from pathlib import Path
import sys

from catalog import chapter


def progress(catalog_path, history_path, chapter_id):
    start, end = chapter(catalog_path, chapter_id)
    history = json.loads(Path(history_path).read_text(encoding="utf-8"))
    end_at = max((row["end"] for row in history["events"] if row["start"] == start), default=start)
    next_at = min(end_at, end)
    return {"chapter_id": chapter_id, "next": next_at, "completed": next_at == end}


if __name__ == "__main__":
    print(json.dumps(progress(*sys.argv[1:])))
