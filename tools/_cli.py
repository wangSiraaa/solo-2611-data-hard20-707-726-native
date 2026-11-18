"""Shared CLI helpers (JSON IO) used by both service CLIs."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def read_json_or_jsonl(path: str) -> Any:
    p = Path(path)
    text = p.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if p.suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    return json.loads(text)


def emit(obj: Any) -> None:
    json.dump(obj, sys.stdout, indent=2, sort_keys=False, ensure_ascii=False)
    sys.stdout.write("\n")


def emit_error(message: str, code: str = "error") -> None:
    json.dump({"error": code, "message": message}, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
