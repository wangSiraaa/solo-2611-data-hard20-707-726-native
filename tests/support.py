"""Shared fixtures for the test-suite."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from clocklab import ClockCatalogue, Ledger

MATERIALS = Path(__file__).resolve().parents[1] / "materials" / "727"


def load_material(name: str) -> Any:
    return json.loads((MATERIALS / name).read_text(encoding="utf-8"))


def fresh_ledger(revision_document: dict[str, Any] | None = None) -> Ledger:
    ledger = Ledger.memory()
    document = revision_document or load_material("clock.json")
    ledger.install_catalogue(ClockCatalogue.from_document(document))
    return ledger


# A zone where three segments' local ranges overlap.  Used to prove that every
# candidate is enumerated, not just the first two.
MULTI_FALLBACK = {
    "revision": "multi",
    "zones": [
        {
            "zone_id": "M",
            "utc_domain": [0, 600],
            "segments": [
                {"utc_start": 0, "utc_end": 300, "offset": 300},
                {"utc_start": 300, "utc_end": 500, "offset": 150},
                {"utc_start": 500, "utc_end": 600, "offset": 0},
            ],
        }
    ],
}


def session(
    session_id: str,
    local_start: int,
    local_end: int,
    rate: int = 2,
    start_occurrence: str | None = None,
    end_occurrence: str | None = None,
    zone_id: str = "Z",
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "zone_id": zone_id,
        "local_start": local_start,
        "start_occurrence": start_occurrence,
        "local_end": local_end,
        "end_occurrence": end_occurrence,
        "rate_per_utc_minute": rate,
    }
