"""Reading sender-side input: CSV files and the ``address-input/v1`` document.

Both paths perform *structural* validation only.  A malformed file (wrong
columns, wrong field count, bad house number) is rejected in full before any
row is received.  Once a file is structurally valid, each row is attempted
independently by the delivery service.
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Sequence

from .errors import InputFormatError
from .models import AddressInput, validate_address_input

CSV_FIELDS = ["parcel_id", "city_id", "district_text", "street_text", "house_no"]


def _parse_house_no(raw: str, where: str) -> int:
    # Same rule as the supplied csv_client: positive ASCII decimal integer.
    if not raw.isascii() or not raw.isdecimal():
        raise InputFormatError(f"{where}: house_no must be a positive ASCII integer")
    value = int(raw)
    if value <= 0:
        raise InputFormatError(f"{where}: house_no must be a positive ASCII integer")
    return value


def read_csv_addresses(path: str | Path, *, source_file: str | None = None) -> list[AddressInput]:
    """Parse a sender CSV, raising :class:`InputFormatError` on any defect.

    The whole file is validated before anything is returned; callers therefore
    reject it wholesale without creating delivery attempts.
    """
    path = Path(path)
    source_file = source_file or str(path)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InputFormatError(f"CSV is not valid UTF-8: {exc}") from exc
    return parse_csv_text(text, source_file=source_file)


def parse_csv_text(text: str, *, source_file: str | None = None) -> list[AddressInput]:
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames != CSV_FIELDS:
        raise InputFormatError("CSV columns must be " + ",".join(CSV_FIELDS))
    result: list[AddressInput] = []
    for source_row, row in enumerate(reader, 2):
        if None in row or any(v is None for v in row.values()):
            raise InputFormatError(f"row {source_row}: wrong field count")
        parcel_id = row["parcel_id"]
        if not parcel_id.strip():
            raise InputFormatError(f"row {source_row}: parcel_id must not be empty")
        house_no = _parse_house_no(row["house_no"], f"row {source_row}")
        item = AddressInput(
            parcel_id=parcel_id,
            city_id=row["city_id"],
            district_text=row["district_text"],
            street_text=row["street_text"],
            house_no=house_no,
            source_file=source_file,
            source_row=source_row,
        )
        validate_address_input(item)
        result.append(item)
    return result


def parse_address_input_document(document: dict, *, source_file: str | None = None) -> list[AddressInput]:
    """Parse the JSON produced by the supplied ``csv_client.py``.

    Accepts the ``address-input/v1`` schema.  Extra fields on a row or address
    are tolerated (the contract allows additions); the meaning of the existing
    fields is preserved.
    """
    if not isinstance(document, dict):
        raise InputFormatError("address-input document must be an object")
    if document.get("schema") != "address-input/v1":
        raise InputFormatError("schema must be address-input/v1")
    rows = document.get("rows")
    if not isinstance(rows, list):
        raise InputFormatError("rows must be a list")
    result: list[AddressInput] = []
    for index, entry in enumerate(rows):
        if not isinstance(entry, dict):
            raise InputFormatError(f"row entry {index} must be an object")
        address = entry.get("address")
        if not isinstance(address, dict):
            raise InputFormatError(f"row entry {index} missing address object")
        for field in CSV_FIELDS:
            if field not in address:
                raise InputFormatError(f"row entry {index} missing {field}")
        house_no = address["house_no"]
        if isinstance(house_no, bool) or not isinstance(house_no, int) or house_no <= 0:
            raise InputFormatError(f"row entry {index}: house_no must be a positive integer")
        source_row = entry.get("source_row")
        if source_row is not None and (
            isinstance(source_row, bool) or not isinstance(source_row, int) or source_row < 1
        ):
            raise InputFormatError(f"row entry {index}: source_row must be a positive integer")
        item = AddressInput(
            parcel_id=address["parcel_id"],
            city_id=address["city_id"],
            district_text=address["district_text"],
            street_text=address["street_text"],
            house_no=house_no,
            source_file=entry.get("source_file", source_file),
            source_row=source_row,
            kind=entry.get("kind", "address"),
        )
        validate_address_input(item)
        result.append(item)
    return result


def read_address_input_json(path: str | Path) -> list[AddressInput]:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    return parse_address_input_document(document, source_file=str(path))


def address_input_document(items: Sequence[AddressInput]) -> dict:
    """Emit an ``address-input/v1`` document (compatible with csv_client)."""
    return {
        "schema": "address-input/v1",
        "rows": [
            {
                "source_row": item.source_row,
                "address": {
                    "parcel_id": item.parcel_id,
                    "city_id": item.city_id,
                    "district_text": item.district_text,
                    "street_text": item.street_text,
                    "house_no": item.house_no,
                },
            }
            for item in items
        ],
    }
