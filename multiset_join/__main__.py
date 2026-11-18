"""Command-line entry points for the source catalog and derived store.

    python -m multiset_join run  INPUT.json --source-db src.db --derived-db d.db
    python -m multiset_join import INPUT.json --source-db src.db
    python -m multiset_join apply  INPUT.json --source-db src.db --batch-id b2
    python -m multiset_join replay INPUT.json --source-db src.db --batch-id b2
    python -m multiset_join read-source --source-db src.db R [--version V]
    python -m multiset_join tuple-count --source-db src.db S '["A","v",3]' [--version V]
    python -m multiset_join derive INPUT.json --source-db src.db --derived-db d.db \
        --result-id after-b3 [--at-batch b3]
    python -m multiset_join read-result --derived-db d.db --result-id initial \
        [--rows] [--region east]
    python -m multiset_join explain --derived-db d.db --result-id after-b2 --sku A
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .catalog import SourceCatalog
from .derive import DerivedStore
from .errors import MultisetError, ValidationError
from .runner import _batch_definitions, load_document, run_document_path
from .model import SOURCES


def _emit(payload: Any) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, sort_keys=False)
    sys.stdout.write("\n")


def _initial(document: dict, required: bool = True) -> tuple[list, list]:
    initial = document.get("initial")
    if initial is None:
        if required:
            raise ValidationError("document has no 'initial' section")
        return [], []
    if not isinstance(initial, dict):
        raise ValidationError("'initial' must be an object")
    return initial.get("R", []), initial.get("S", [])


def _import(document: dict, source_db: str) -> dict:
    r_rows, s_rows = _initial(document)
    with SourceCatalog(source_db) as catalog:
        return catalog.import_initial(r_rows, s_rows)


def cmd_run(args: argparse.Namespace) -> dict:
    return run_document_path(args.input, args.source_db, args.derived_db)


def cmd_import(args: argparse.Namespace) -> dict:
    document = load_document(args.input)
    return {"op": "import_initial", **_import(document, args.source_db)}


def cmd_apply(args: argparse.Namespace) -> dict:
    document = load_document(args.input)
    definitions = _batch_definitions(document)
    if args.batch_id not in definitions:
        raise ValidationError(f"batch_id {args.batch_id!r} is not in the document")
    with SourceCatalog(args.source_db) as catalog:
        if not catalog.is_initialized():
            r_rows, s_rows = _initial(document)
            catalog.import_initial(r_rows, s_rows)
        result = catalog.apply_batch(args.batch_id, definitions[args.batch_id])
        return {
            "op": "apply",
            **result.as_dict(),
            "current_source_version": catalog.current_version(),
        }


def cmd_replay(args: argparse.Namespace) -> dict:
    document = load_document(args.input)
    definitions = _batch_definitions(document)
    expected = definitions.get(args.batch_id)
    with SourceCatalog(args.source_db) as catalog:
        record = catalog.replay_batch(args.batch_id, expected)
        return {
            "op": "replay",
            "batch_id": record.batch_id,
            "digest": record.digest,
            "source_version": record.accepted_version,
            "changes": [c.as_dict() for c in record.changes],
            "current_source_version": catalog.current_version(),
            "slice": catalog.read_slice(record.accepted_version),
        }


def _version_arg(args: argparse.Namespace) -> int | None:
    at_batch = getattr(args, "at_batch", None)
    if at_batch is not None and args.version is not None:
        raise ValidationError("specify at most one of --version and --at-batch")
    if at_batch is not None:
        with SourceCatalog(args.source_db) as catalog:
            return catalog.get_batch(at_batch).accepted_version
    return args.version


def cmd_read_source(args: argparse.Namespace) -> dict:
    version = _version_arg(args)
    with SourceCatalog(args.source_db) as catalog:
        resolved = catalog.resolve_version(version)
        rows = catalog.read_source_rows(args.source, resolved)
        return {
            "op": "read_source",
            "source": args.source,
            "source_version": resolved,
            "rows": [
                {"tuple": list(row.tuple), "multiplicity": row.multiplicity}
                for row in rows
            ],
            "distinct_tuple_count": len(rows),
            "total_multiplicity": sum(row.multiplicity for row in rows),
        }


def cmd_tuple_count(args: argparse.Namespace) -> dict:
    try:
        raw_tuple = json.loads(args.tuple)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"--tuple is not valid JSON: {exc}") from exc
    version = _version_arg(args)
    with SourceCatalog(args.source_db) as catalog:
        resolved = catalog.resolve_version(version)
        count = catalog.tuple_count(args.source, raw_tuple, resolved)
        return {
            "op": "tuple_count",
            "source": args.source,
            "tuple": raw_tuple,
            "source_version": resolved,
            "multiplicity": count,
        }


def cmd_derive(args: argparse.Namespace) -> dict:
    version = _version_arg(args)
    with DerivedStore(args.source_db, args.derived_db) as store:
        result = store.build_result(
            args.result_id, source_version=version, replace=args.replace
        )
    return {"op": "derive", **result}


def cmd_read_result(args: argparse.Namespace) -> dict:
    with DerivedStore(args.source_db, args.derived_db) as store:
        result = store.get_result(args.result_id)
        if args.rows:
            result["rows"] = store.read_result_rows(
                args.result_id,
                region=args.region,
                sku=args.sku,
                quoted=args.quoted,
                limit=args.limit,
                offset=args.offset,
            )
    return {"op": "read_result", **result}


def cmd_explain(args: argparse.Namespace) -> dict:
    with DerivedStore(args.source_db, args.derived_db) as store:
        result = store.explain(
            args.result_id,
            region=args.region,
            sku=args.sku,
            quantity=args.quantity,
            quoted=args.quoted,
            supplier=args.supplier,
            price=args.price,
        )
    return {"op": "explain", **result}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m multiset_join",
        description="Signed multiset source catalog and replayable LEFT JOIN store.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_source_db(p: argparse.ArgumentParser) -> None:
        p.add_argument("--source-db", required=True, help="source catalog SQLite path")

    def add_version(p: argparse.ArgumentParser) -> None:
        p.add_argument("--version", type=int, default=None, help="source version")
        p.add_argument("--at-batch", default=None, help="resolve version of a batch")

    p = sub.add_parser("run", help="execute an operation document end to end")
    p.add_argument("input")
    add_source_db(p)
    p.add_argument("--derived-db", required=True)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("import", help="import the initial R/S multisets")
    p.add_argument("input")
    add_source_db(p)
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("apply", help="apply one batch by id")
    p.add_argument("input")
    add_source_db(p)
    p.add_argument("--batch-id", required=True)
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("replay", help="replay an accepted batch without re-applying")
    p.add_argument("input")
    add_source_db(p)
    p.add_argument("--batch-id", required=True)
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("read-source", help="read a source table at a version")
    add_source_db(p)
    p.add_argument("source", choices=list(SOURCES))
    add_version(p)
    p.set_defaults(func=cmd_read_source)

    p = sub.add_parser("tuple-count", help="read one complete tuple's multiplicity")
    add_source_db(p)
    p.add_argument("source", choices=list(SOURCES))
    p.add_argument("tuple", help="JSON array, e.g. '[\"A\",\"v\",3]'")
    add_version(p)
    p.set_defaults(func=cmd_tuple_count)

    p = sub.add_parser("derive", help="build a derived result from a pinned slice")
    p.add_argument("input", nargs="?", default=None, help="operation document (optional)")
    add_source_db(p)
    p.add_argument("--derived-db", required=True)
    p.add_argument("--result-id", required=True)
    p.add_argument("--replace", action="store_true", help="rebuild an existing result id")
    add_version(p)
    p.set_defaults(func=cmd_derive)

    p = sub.add_parser("read-result", help="read a stored derived result")
    add_source_db(p)
    p.add_argument("--derived-db", required=True)
    p.add_argument("--result-id", required=True)
    p.add_argument("--rows", action="store_true", help="include output rows")
    p.add_argument("--region", default=None)
    p.add_argument("--sku", default=None)
    p.add_argument("--quoted", action=argparse.BooleanOptionalAction, default=None)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--offset", type=int, default=0)
    p.set_defaults(func=cmd_read_result)

    p = sub.add_parser("explain", help="explain derived counts from source tuples")
    add_source_db(p)
    p.add_argument("--derived-db", required=True)
    p.add_argument("--result-id", required=True)
    p.add_argument("--region", default=None)
    p.add_argument("--sku", default=None)
    p.add_argument("--quantity", type=int, default=None)
    p.add_argument("--quoted", action=argparse.BooleanOptionalAction, default=None)
    p.add_argument("--supplier", default=None)
    p.add_argument("--price", type=int, default=None)
    p.set_defaults(func=cmd_explain)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        _emit(args.func(args))
    except MultisetError as exc:
        _emit({"error": type(exc).__name__, "message": str(exc)})
        return 2
    except FileNotFoundError as exc:
        _emit({"error": "FileNotFoundError", "message": str(exc)})
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
