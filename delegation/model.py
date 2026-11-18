"""Parsing / normalisation of grants and receipt originals (delegation side).

Nothing here looks at shipments or parts.  Names are opaque identifiers; the
service never tries to infer an identity from a name.
"""

from __future__ import annotations

from typing import Any

from .errors import ValidationError
from .util import content_hash, is_int

_REQUIRED_GRANT = (
    "grant_id",
    "revision",
    "issuer",
    "agent",
    "regions",
    "document_types",
    "start",
    "end",
    "can_redelegate",
    "parent_grant_id",
    "revoked_from",
)
_REQUIRED_RECEIPT = (
    "receipt_id",
    "consignee",
    "signer",
    "signed_day",
    "received_day",
    "region",
    "document_type",
    "claimed_grant_ids",
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


def _str_list(value: Any, name: str, *, allow_empty: bool) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ValidationError(f"{name} must be a list of non-empty strings")
    if not allow_empty and not value:
        raise ValidationError(f"{name} must not be empty")
    return sorted(set(value))


def _check_dupes(values: list[str], name: str) -> list[str]:
    # Duplicates are silently collapsed (set semantics) but empty entries are not.
    return sorted(set(values))


def parse_grant(obj: Any) -> dict:
    obj = _require(obj, _REQUIRED_GRANT, "grant")
    grant_id = _nonempty_str(obj["grant_id"], "grant_id")
    revision = obj["revision"]
    if not is_int(revision) or revision < 1:
        raise ValidationError(f"{grant_id}.revision must be an integer >= 1")
    issuer = _nonempty_str(obj["issuer"], "issuer")
    agent = _nonempty_str(obj["agent"], "agent")
    regions = _str_list(obj["regions"], "regions", allow_empty=False)
    document_types = _str_list(obj["document_types"], "document_types", allow_empty=False)
    start, end = obj["start"], obj["end"]
    if not is_int(start) or not is_int(end):
        raise ValidationError(f"{grant_id}.start/end must be integers")
    if start >= end:
        raise ValidationError(f"{grant_id}: nominal interval must satisfy start < end")
    if not isinstance(obj["can_redelegate"], bool):
        raise ValidationError(f"{grant_id}.can_redelegate must be boolean")
    parent = obj["parent_grant_id"]
    if parent is not None:
        parent = _nonempty_str(parent, "parent_grant_id")
    revoked = obj["revoked_from"]
    if revoked is not None:
        if not is_int(revoked):
            raise ValidationError(f"{grant_id}.revoked_from must be integer or null")
        if not (start <= revoked < end):
            raise ValidationError(
                f"{grant_id}: revoked_from {revoked} must lie inside nominal "
                f"[{start},{end})"
            )

    normalized = {
        "grant_id": grant_id,
        "revision": revision,
        "issuer": issuer,
        "agent": agent,
        "regions": regions,
        "document_types": document_types,
        "start": start,
        "end": end,
        "can_redelegate": obj["can_redelegate"],
        "parent_grant_id": parent,
        "revoked_from": revoked,
    }
    return {
        **normalized,
        "payload": obj,
        "content_hash": content_hash(normalized),
    }


def parse_receipt(obj: Any) -> dict:
    obj = _require(obj, _REQUIRED_RECEIPT, "receipt")
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

    normalized = {
        "receipt_id": receipt_id,
        "consignee": consignee,
        "signer": signer,
        "signed_day": signed_day,
        "received_day": received_day,
        "region": region,
        "document_type": document_type,
        "claimed_chain": list(chain),
    }
    return {
        **normalized,
        "payload": obj,
        "content_hash": content_hash(normalized),
    }
