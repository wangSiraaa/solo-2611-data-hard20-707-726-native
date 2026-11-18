"""period-bill/1 document model and validation.

The read-only client (``bill_reader.py``) understands exactly this shape.
Original bills keep their ID and amount; adjustments are separate documents
with ``document_type = "ADJUSTMENT"``, a signed ``amount_cents`` and a
``previous_statement_id`` naming the original bill they adjust.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from .errors import DocumentError

FORMAT = "period-bill/1"
BILL = "BILL"
ADJUSTMENT = "ADJUSTMENT"
DOCUMENT_TYPES = {BILL, ADJUSTMENT}

REQUIRED_FIELDS = (
    "format",
    "document_id",
    "document_type",
    "account_id",
    "year",
    "period",
    "amount_cents",
    "contract_revision",
    "usage_revisions",
    "previous_statement_id",
)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class BillDocument:
    document_id: str
    document_type: str
    account_id: str
    year: int
    period: int
    amount_cents: int
    contract_revision: int
    usage_revisions: dict[int, int]
    previous_statement_id: str | None
    source: str = "unknown"
    extra: dict | None = None

    # -- validation -------------------------------------------------------
    @classmethod
    def from_dict(cls, payload: dict, *, source: str = "unknown") -> "BillDocument":
        if not isinstance(payload, dict):
            raise DocumentError("document must be a JSON object")
        missing = [f for f in REQUIRED_FIELDS if f not in payload]
        if missing:
            raise DocumentError(f"document missing fields: {', '.join(missing)}")

        if payload["format"] != FORMAT:
            raise DocumentError(f"unsupported format: {payload['format']!r}")

        document_id = payload["document_id"]
        if not isinstance(document_id, str) or not document_id:
            raise DocumentError("document_id must be a non-empty string")

        document_type = payload["document_type"]
        if document_type not in DOCUMENT_TYPES:
            raise DocumentError(f"unsupported document_type: {document_type!r}")

        account_id = payload["account_id"]
        if not isinstance(account_id, str) or not account_id:
            raise DocumentError("account_id must be a non-empty string")

        year = payload["year"]
        if not _is_int(year) or not (1 <= year <= 9999):
            raise DocumentError("year must be an integer in 1..9999")

        period = payload["period"]
        if not _is_int(period) or not (1 <= period <= 12):
            raise DocumentError("period must be an integer in 1..12")

        amount = payload["amount_cents"]
        if not _is_int(amount):
            raise DocumentError("amount_cents must be an integer")
        if document_type == BILL and amount < 0:
            raise DocumentError("BILL amount_cents must be non-negative")

        contract_revision = payload["contract_revision"]
        if not _is_int(contract_revision) or contract_revision < 0:
            raise DocumentError("contract_revision must be a non-negative integer")

        raw_revisions = payload["usage_revisions"]
        if not isinstance(raw_revisions, dict):
            raise DocumentError("usage_revisions must be an object")
        usage_revisions: dict[int, int] = {}
        for key, value in raw_revisions.items():
            if not isinstance(key, str) or not key.isdigit():
                raise DocumentError(f"usage_revisions key must be a period: {key!r}")
            p = int(key)
            if not (1 <= p <= 12):
                raise DocumentError(f"usage_revisions period out of range: {p}")
            if not _is_int(value) or value < 1:
                raise DocumentError(f"usage_revisions[{key}] must be a positive revision")
            usage_revisions[p] = value

        previous = payload["previous_statement_id"]
        if document_type == BILL:
            if previous is not None:
                raise DocumentError("BILL previous_statement_id must be null")
        else:
            if not isinstance(previous, str) or not previous:
                raise DocumentError(
                    "ADJUSTMENT previous_statement_id must name the adjusted bill"
                )

        known = set(REQUIRED_FIELDS) | {"source", "recalc_run_id", "sequence",
                                        "target_amount_cents"}
        extra = {k: v for k, v in payload.items() if k not in known}

        return cls(
            document_id=document_id,
            document_type=document_type,
            account_id=account_id,
            year=year,
            period=period,
            amount_cents=amount,
            contract_revision=contract_revision,
            usage_revisions=usage_revisions,
            previous_statement_id=previous,
            source=payload.get("source", source),
            extra=extra or None,
        )

    # -- serialization ----------------------------------------------------
    def to_dict(self) -> dict:
        payload = {
            "format": FORMAT,
            "document_id": self.document_id,
            "document_type": self.document_type,
            "account_id": self.account_id,
            "year": self.year,
            "period": self.period,
            "amount_cents": self.amount_cents,
            "contract_revision": self.contract_revision,
            "usage_revisions": {str(p): r for p, r in sorted(self.usage_revisions.items())},
            "previous_statement_id": self.previous_statement_id,
            "source": self.source,
        }
        if self.extra:
            payload.update(self.extra)
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), ensure_ascii=False)


def load_jsonl(path: str) -> list[dict]:
    return [payload for _, payload in load_jsonl_raw(path)]


def load_jsonl_raw(path: str) -> list[tuple[str, dict]]:
    """Return ``(original_line_text, parsed_object)`` pairs.

    The raw text lets imported documents be re-exported byte-for-byte, so the
    original issued bills are never rewritten.
    """
    docs = []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                docs.append((stripped, json.loads(stripped)))
            except json.JSONDecodeError as exc:
                raise DocumentError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
    return docs
