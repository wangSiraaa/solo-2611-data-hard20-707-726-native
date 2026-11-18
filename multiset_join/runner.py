"""Operation-file runner: replay the supplied document over durable stores.

Every operation opens the catalog / derived store it needs from disk, performs
one action, and closes it again.  This mirrors "consume an operation file,
close and reopen" and proves that no in-memory state is required across steps.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .catalog import SourceCatalog
from .derive import DerivedStore
from .errors import MultisetError, ValidationError
from .model import SOURCES, is_int, normalize_tuple


def load_document(path: str | Path) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        document = json.load(handle)
    if not isinstance(document, dict):
        raise ValidationError("operation document must be a JSON object")
    return document


def _batch_definitions(document: dict) -> dict[str, list]:
    batches = document.get("batches", [])
    if not isinstance(batches, list):
        raise ValidationError("'batches' must be a list")
    definitions: dict[str, list] = {}
    for index, batch in enumerate(batches):
        if not isinstance(batch, dict) or "batch_id" not in batch or "changes" not in batch:
            raise ValidationError(f"batches[{index}] needs 'batch_id' and 'changes'")
        batch_id = batch["batch_id"]
        if not isinstance(batch_id, str) or not batch_id:
            raise ValidationError(f"batches[{index}].batch_id must be a non-empty string")
        if batch_id in definitions:
            raise ValidationError(f"duplicate batch_id {batch_id!r} in document")
        definitions[batch_id] = batch["changes"]
    return definitions


def _changes_for(
    op: dict, definitions: dict[str, list], source: str
) -> list:
    if "changes" in op:
        return op["changes"]
    batch_id = op.get("batch_id")
    if not isinstance(batch_id, str) or not batch_id:
        raise ValidationError(f"{source}: batch_id must be a non-empty string")
    if batch_id not in definitions:
        raise ValidationError(
            f"{source}: batch_id {batch_id!r} is not defined in the document"
        )
    return definitions[batch_id]


def run_operations(
    document: dict,
    source_db: str | Path,
    derived_db: str | Path,
    stop_on_error: bool = True,
) -> dict:
    """Execute an operation document against the two independent stores."""
    definitions = _batch_definitions(document)
    operations = document.get("operations", [])
    if not isinstance(operations, list):
        raise ValidationError("'operations' must be a list")

    initial = document.get("initial")
    if initial is not None and not isinstance(initial, dict):
        raise ValidationError("'initial' must be an object")

    step_results: list[dict] = []
    error: str | None = None

    if initial is not None:
        with SourceCatalog(source_db) as catalog:
            import_report = catalog.import_initial(
                initial.get("R", []), initial.get("S", [])
            )
        step_results.append({"step": 0, "op": "import_initial", **import_report})

    for index, op in enumerate(operations, start=1):
        if not isinstance(op, dict):
            raise ValidationError(f"operations[{index - 1}] must be an object")
        kind = op.get("op")
        try:
            result = _run_one(kind, op, definitions, source_db, derived_db)
        except MultisetError as exc:
            error = f"{type(exc).__name__}: {exc}"
            step_results.append(
                {"step": index, "op": kind, "status": "error", "error": error}
            )
            if stop_on_error:
                raise
            continue
        step_results.append({"step": index, **result})

    return {
        "source_db": str(source_db),
        "derived_db": str(derived_db),
        "steps": step_results,
        "error": error,
    }


def _run_one(
    kind: Any,
    op: dict,
    definitions: dict[str, list],
    source_db: str | Path,
    derived_db: str | Path,
) -> dict:
    if kind == "apply":
        batch_id = op.get("batch_id")
        changes = _changes_for(op, definitions, "apply")
        with SourceCatalog(source_db) as catalog:
            applied = catalog.apply_batch(batch_id, changes)
            return {
                "op": "apply",
                "status": applied.status,
                "batch_id": applied.batch_id,
                "source_version": applied.source_version,
                "digest": applied.digest,
                "changes": [c.as_dict() for c in applied.changes],
                "change_count": len(applied.changes),
                "current_source_version": catalog.current_version(),
            }

    if kind == "replay":
        batch_id = op.get("batch_id")
        if not isinstance(batch_id, str) or not batch_id:
            raise ValidationError("replay: batch_id must be a non-empty string")
        expected = definitions.get(batch_id)
        with SourceCatalog(source_db) as catalog:
            record = catalog.replay_batch(batch_id, expected)
            slice_ = catalog.read_slice(record.accepted_version)
            return {
                "op": "replay",
                "status": "replayed",
                "batch_id": record.batch_id,
                "digest": record.digest,
                "source_version": record.accepted_version,
                "accepted_at": record.accepted_at,
                "changes": [c.as_dict() for c in record.changes],
                "change_count": len(record.changes),
                "current_source_version": catalog.current_version(),
                # The original slice this batch produced, not the current one.
                "slice": slice_,
            }

    if kind == "derive":
        result_id = op.get("result_id")
        if not isinstance(result_id, str) or not result_id:
            raise ValidationError("derive: result_id must be a non-empty string")
        version = _resolve_op_version(op, source_db)
        with DerivedStore(source_db, derived_db) as store:
            result = store.build_result(
                result_id, source_version=version, replace=bool(op.get("replace", False))
            )
        return {"op": "derive", **result}

    if kind == "read_result":
        result_id = op.get("result_id")
        if not isinstance(result_id, str) or not result_id:
            raise ValidationError("read_result: result_id must be a non-empty string")
        with DerivedStore(source_db, derived_db) as store:
            result = store.get_result(result_id)
            if op.get("include_rows", False):
                result = {
                    **result,
                    "rows": store.read_result_rows(
                        result_id,
                        region=op.get("region"),
                        sku=op.get("sku"),
                        quoted=op.get("quoted"),
                        limit=op.get("limit"),
                        offset=int(op.get("offset", 0)),
                    ),
                }
            if op.get("include_region_summary", True):
                result["by_region"] = store.region_summary(result_id)
        return {"op": "read_result", **result}

    if kind == "read_slice":
        version = _resolve_op_version(op, source_db)
        with SourceCatalog(source_db) as catalog:
            return {"op": "read_slice", **catalog.read_slice(version)}

    if kind == "read_source":
        source = op.get("source")
        if source not in SOURCES:
            raise ValidationError("read_source: source must be 'R' or 'S'")
        version = _resolve_op_version(op, source_db)
        with SourceCatalog(source_db) as catalog:
            resolved = catalog.resolve_version(version)
            rows = catalog.read_source_rows(source, resolved)
            return {
                "op": "read_source",
                "source": source,
                "source_version": resolved,
                "rows": [
                    {"tuple": list(row.tuple), "multiplicity": row.multiplicity}
                    for row in rows
                ],
                "distinct_tuple_count": len(rows),
                "total_multiplicity": sum(row.multiplicity for row in rows),
            }

    if kind == "tuple_count":
        source = op.get("source")
        raw_tuple = op.get("tuple")
        if source not in SOURCES:
            raise ValidationError("tuple_count: source must be 'R' or 'S'")
        version = _resolve_op_version(op, source_db)
        with SourceCatalog(source_db) as catalog:
            resolved = catalog.resolve_version(version)
            count = catalog.tuple_count(source, raw_tuple, resolved)
            return {
                "op": "tuple_count",
                "source": source,
                "tuple": list(normalize_tuple(source, raw_tuple)),
                "source_version": resolved,
                "multiplicity": count,
            }

    if kind == "explain":
        result_id = op.get("result_id")
        if not isinstance(result_id, str) or not result_id:
            raise ValidationError("explain: result_id must be a non-empty string")
        filters = {
            "region": op.get("region"),
            "sku": op.get("sku"),
            "quantity": op.get("quantity"),
            "quoted": op.get("quoted"),
            "supplier": op.get("supplier"),
            "price": op.get("price"),
        }
        with DerivedStore(source_db, derived_db) as store:
            return {"op": "explain", **store.explain(result_id, **filters)}

    raise ValidationError(f"unknown operation {kind!r}")


def _resolve_op_version(op: dict, source_db: str | Path) -> int | None:
    at_batch = op.get("at_batch")
    version = op.get("version")
    if at_batch is not None and version is not None:
        raise ValidationError("specify at most one of 'version' and 'at_batch'")
    if at_batch is not None:
        if not isinstance(at_batch, str) or not at_batch:
            raise ValidationError("at_batch must be a non-empty batch_id")
        with SourceCatalog(source_db) as catalog:
            return catalog.get_batch(at_batch).accepted_version
    if version is not None and not is_int(version):
        raise ValidationError("version must be an integer")
    return version


def run_document_path(
    path: str | Path,
    source_db: str | Path,
    derived_db: str | Path,
    stop_on_error: bool = True,
) -> dict:
    return run_operations(load_document(path), source_db, derived_db, stop_on_error)
