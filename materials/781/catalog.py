"""Read the current chapter catalogue for the legacy player."""
import json
from pathlib import Path


def chapter(path, chapter_id):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    for item in data["chapters"]:
        if item["chapter_id"] == chapter_id:
            return item["start"], item["end"]
    raise ValueError("unknown chapter")
