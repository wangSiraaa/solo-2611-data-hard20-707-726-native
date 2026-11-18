"""Parsing / normalisation for delivery-side input (shipments, supplements).

A "receipt supplement" is the delivery-side record of an original receipt: it
repeats the receipt content verbatim (so its hash can be matched against the
delegation original) and adds an explicit shipment/part binding.  The binding
is a supplement to the original content; it is fixed once accepted and can
never be re-pointed at another shipment.
"""

from __future__ import annotations

from typing import Any

from .errors import ValidationError
from .util import content_hash, is_int

_REQUIRED_SHIPMENT = (
    "shipment_id",
    "consignee",
    "region",
    "document_type",
    "required_parts",
)
_REQUIRED_SUPPLEMENT = (
    "receipt_id",
    "consignee",
    "signer",
    "signed_day",
    "received_day",
    "region",
    "document_type",
    "claimed_grant_ids",
    "shipment_id",
    "part_ids",
)


def _require(obj: Any, fields: tuple[str, ...], kind: str) -> dict:
    if not isinstance(obj, dict):
        raise ValidationError(f"{kind} must be a JSON object")
    missing = [f for f in fields if f not in obj]
    if missing:
        raise ValidationError(f"{kind} missing fields: {missing}")
    return obj


def _nonempty_str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{name} must be a non-empty string")
    return value


def parse_shipment(obj: Any) -> dict:
    obj = _require(obj, _REQUIRED_SHIPMENT, "shipment")
    shipment_id = _nonempty_str(obj["shipment_id"], "shipment_id")
    consignee = _nonempty_str(obj["consignee"], "consignee")
    region = _nonempty_str(obj["region"], "region")
    document_type = _nonempty_str(obj["document_type"], "document_type")
    parts = obj["required_parts"]
    if not isinstance(parts, list) or not all(isinstance(p, str) and p for p in parts):
        raise ValidationError(f"{shipment_id}.required_parts must be a list of non-empty strings")
    if not parts:
        raise ValidationError(f"{shipment_id}.required_parts must not be empty")
    if len(set(parts)) != len(parts):
        raise ValidationError(f"{shipment_id}.required_parts contains a duplicate part id")
    normalized = {
        "shipment_id": shipment_id,
        "consignee": consignee,
        "region": region,
        "document_type": document_type,
        "required_parts": sorted(parts),
    }
    return {**normalized, "payload": obj, "content_hash": content_hash(normalized)}


def parse_supplement(obj: Any) -> dict:
    obj = _require(obj, _REQUIRED_SUPPLEMENT, "receipt supplement")
    receipt_id = _nonempty_str(obj["receipt_id"], "receipt_id")
    consignee = _nonempty_str(obj["consignee"], "consignee")
    signer = obj["signer"]
    if signer is not None:
        signer = _nonempty_str(signer, "signer")
    signed_day, received_day = obj["signed_day"], obj["received_day"]
    if not is_int(signed_day):
        raise ValidationError(f"{receipt_id}.signed_day must be an integer")
    if not is_int(received_day):
        raise ValidationError(f"{receipt_id}.received_day must be an integer")
    region = _nonempty_str(obj["region"], "region")
    document_type = _nonempty_str(obj["document_type"], "document_type")
    chain = obj["claimed_grant_ids"]
    if not isinstance(chain, list) or not all(isinstance(c, str) and c for c in chain):
        raise ValidationError(f"{receipt_id}.claimed_grant_ids must be a list of non-empty strings")
    if len(set(chain)) != len(chain):
        raise ValidationError(f"{receipt_id}.claimed_grant_ids contains a repeated grant id")
    shipment_id = _nonempty_str(obj["shipment_id"], "shipment_id")
    part_ids = obj["part_ids"]
    if not isinstance(part_ids, list) or not all(isinstance(p, str) and p for p in part_ids):
        raise ValidationError(f"{receipt_id}.part_ids must be a list of non-empty strings")
    if not part_ids:
        raise ValidationError(f"{receipt_id}.part_ids must not be empty")

    core = {
        "receipt_id": receipt_id,
        "consignee": consignee,
        "signer": signer,
        "signed_day": signed_day,
        "received_day": received_day,
        "region": region,
        "document_type": document_type,
        "claimed_chain": list(chain),
    }
    binding = {
        "shipment_id": shipment_id,
        "part_ids": sorted(set(part_ids)),
    }
    return {
        **core,
        "shipment_id": shipment_id,
        "part_ids": sorted(set(part_ids)),
        "core_hash": content_hash(core),
        "payload": obj,
    }
