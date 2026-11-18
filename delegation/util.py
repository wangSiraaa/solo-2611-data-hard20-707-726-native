"""Small helpers shared inside the delegation package only."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any


def canonical(obj: Any) -> str:
    """Deterministic JSON used for hashing and conflict comparison."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(obj: Any) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def is_int(value: Any) -> bool:
    """bool is a subclass of int; delegation days/revisions must be real ints."""
    return isinstance(value, int) and not isinstance(value, bool)
