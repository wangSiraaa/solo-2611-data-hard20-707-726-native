"""Input value objects and structural validation."""
from __future__ import annotations

from dataclasses import dataclass

from .errors import InputFormatError


@dataclass(frozen=True)
class AddressInput:
    """One parcel's sender-side address, before any catalog interpretation.

    Field names and meanings mirror ``address-input/v1``:
    ``parcel_id``, ``city_id``, ``district_text``, ``street_text``,
    ``house_no``.  ``source_file``/``source_row`` are provenance only and are
    never part of the parcel identity.
    """

    parcel_id: str
    city_id: str
    district_text: str
    street_text: str
    house_no: int
    source_file: str | None = None
    source_row: int | None = None
    kind: str = "address"


def validate_address_input(item: AddressInput) -> None:
    """Raise :class:`InputFormatError` if *item* is structurally invalid."""
    if not isinstance(item, AddressInput):
        raise InputFormatError("expected AddressInput")
    if not isinstance(item.parcel_id, str) or not item.parcel_id.strip():
        raise InputFormatError("parcel_id must be a non-empty string")
    if not isinstance(item.city_id, str):
        raise InputFormatError("city_id must be a string")
    if not isinstance(item.district_text, str):
        raise InputFormatError("district_text must be a string")
    if not isinstance(item.street_text, str):
        raise InputFormatError("street_text must be a string")
    if isinstance(item.house_no, bool) or not isinstance(item.house_no, int):
        raise InputFormatError("house_no must be an integer")
    if item.house_no <= 0:
        raise InputFormatError("house_no must be a positive integer")
    if item.source_row is not None and (
        isinstance(item.source_row, bool)
        or not isinstance(item.source_row, int)
        or item.source_row < 1
    ):
        raise InputFormatError("source_row must be a positive integer when present")
    if not isinstance(item.kind, str) or not item.kind:
        raise InputFormatError("kind must be a non-empty string")
