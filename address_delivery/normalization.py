"""Text and house-number normalisation.

Only two text operations are permitted, exactly as specified:

* Unicode NFC normalisation
* stripping leading/trailing whitespace

Internal whitespace is *not* collapsed and no fuzzy matching of any kind is
performed.  This means ``"学  园"`` and ``"学 园"`` are different names, while
``"Cafe\\u0301路"`` and ``"Café路"`` are the same name.
"""
from __future__ import annotations

import json
import unicodedata


def normalize_text(value: str) -> str:
    """NFC-normalise *value* and strip leading/trailing whitespace."""
    if not isinstance(value, str):
        raise TypeError("text value must be a str")
    return unicodedata.normalize("NFC", value).strip()


def make_identity(city_id: str, district_text: str, street_text: str, house_no: int) -> str:
    """Canonical parcel identity for frozen-success comparisons.

    Uses the stable city id, the NFC/trimmed district and street text, and the
    integer house number.  Source file and source row are deliberately absent.
    """
    payload = [
        city_id,
        normalize_text(district_text),
        normalize_text(street_text),
        int(house_no),
    ]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def identity_parts(identity_key: str) -> list:
    """Inverse of :func:`make_identity` for diagnostics."""
    return json.loads(identity_key)
