"""Replay ``operations.jsonl`` against a working copy of the history database.

The runner is intentionally envelope-preserving: it never re-runs historical
redemptions and never re-initialises balances.  ``O_OLD`` already exists in
``redemptions`` and its uses are already reflected in ``coupon_instances``;
the only operation touching it is the cancel that returns the recorded uses.

Operation kinds
---------------
``revise_template``  publish a new immutable version and make it current.
``redeem``           apply :func:`coupon_service.ledger.redeem`.
``cancel``           apply :func:`coupon_service.ledger.cancel`.
``get``/``preflight_redeem``/``query``
                     read-only, no writes.
``balances``         read-only remaining counts.
``snapshot``         dump the tables the read-only client understands.

The input file is never modified.  Every operation's structured outcome is
written to an output JSONL file for inspection.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from . import catalog, db, errors, ledger
from .jsonutil import dumps as canonical_dumps
from .models import OrderLine, TemplateSpec


@dataclass(frozen=True)
class OperationOutcome:
    index: int
    op: str
    response: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"index": self.index, "op": self.op, "response": self.response}


def _read_jsonl(path: str | Path) -> Iterable[dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            try:
                value = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: operation must be an object")
            yield value


def _load_revision(base_dir: Path, name: str) -> dict[str, Any]:
    candidate = base_dir / name
    with open(candidate, "r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{candidate}: revision must be a JSON object")
    return value


def _apply_revision(con, base_dir: Path, name: str) -> dict[str, Any]:
    revision = _load_revision(base_dir, name)
    template_id = revision.get("template_id")
    version = revision.get("version")
    if not isinstance(template_id, str) or not template_id:
        raise ValueError("revision.template_id must be a non-empty string")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError("revision.version must be an integer")
    spec = TemplateSpec.parse(revision.get("spec"), "revision.spec")
    with ledger._Transaction(con):
        published = catalog.revise(con, template_id, version, spec)
    return {
        "entry": "catalog",
        "op": "revise_template",
        "template_id": published.template_id,
        "version": published.version,
        "spec": published.spec.as_dict(),
    }


def _render_preflight(evaluation) -> dict[str, Any]:
    if evaluation.ok and evaluation.plan is not None:
        return {
            "entry": "qualification",
            "eligible": True,
            "plan": evaluation.plan.as_dict(),
        }
    return {
        "entry": "qualification",
        "eligible": False,
        "code": evaluation.primary,
        "issues": evaluation.reason_dicts(),
    }


def run_operations(
    db_path: str | Path,
    operations_path: str | Path,
    *,
    output_path: str | Path | None = None,
) -> list[OperationOutcome]:
    """Execute every operation in order; return their structured outcomes."""

    operations_path = Path(operations_path)
    base_dir = operations_path.parent
    con = db.connect(db_path)
    outcomes: list[OperationOutcome] = []
    try:
        db.ensure_schema(con)
        for index, operation in enumerate(_read_jsonl(operations_path)):
            op = operation.get("op")
            if op == "revise_template":
                response = _apply_revision(con, base_dir, operation["file"])
            elif op == "redeem":
                request_id = operation.get("request_id")
                response = ledger.redeem(con, request_id, operation).as_dict()
            elif op == "cancel":
                request_id = operation.get("request_id")
                response = ledger.cancel(con, request_id, operation).as_dict()
            elif op in {"preflight_redeem", "query_qualification"}:
                order_id = operation.get("order_id")
                user_id = operation.get("user_id")
                lines = tuple(
                    OrderLine(
                        sku=line["sku"],
                        qty=line["qty"],
                        unit_price=line["unit_price"],
                    )
                    for line in operation.get("lines", [])
                )
                coupon_ids = tuple(operation.get("coupon_ids", []))
                evaluation = ledger.preflight_redeem(
                    con, user_id, lines, coupon_ids
                )
                response = _render_preflight(evaluation)
                if order_id is not None:
                    response["order_id"] = order_id
            elif op in {"balances", "get_balances"}:
                instance_ids = tuple(operation.get("instance_ids", []))
                response = {
                    "entry": "ledger",
                    "balances": ledger.current_balances(con, instance_ids),
                }
            elif op in {"get", "order_status"}:
                order_id = operation.get("order_id")
                response = {
                    "entry": "ledger",
                    "order_id": order_id,
                    "status": ledger.order_status(con, order_id),
                }
            elif op == "snapshot":
                from .snapshot import snapshot_connection

                read_only = db.connect(db_path, readonly=True)
                try:
                    response = {
                        "entry": "snapshot",
                        "data": snapshot_connection(read_only),
                    }
                finally:
                    read_only.close()
            else:
                response = {
                    "entry": "runner",
                    "status": "REJECTED",
                    "code": errors.INVALID_REQUEST,
                    "issues": [
                        errors.issue(
                            "invalid_request", message=f"unknown op {op!r}"
                        ).as_dict()
                    ],
                }
            outcomes.append(OperationOutcome(index, str(op), response))
    finally:
        con.close()

    if output_path is not None:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as handle:
            for outcome in outcomes:
                handle.write(canonical_dumps(outcome.as_dict()) + "\n")
    return outcomes


def copy_history(source: str | Path, destination: str | Path) -> Path:
    """Copy the material database to a working location without touching it."""

    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.resolve() == source.resolve():
        raise ValueError("destination must differ from the read-only material")
    shutil.copyfile(source, destination)
    return destination


__all__ = [
    "OperationOutcome",
    "run_operations",
    "copy_history",
]
