#!/usr/bin/env python3
"""Command line entry points for the catalog and scoring repository.

Commands keep the two stores behind independent surfaces:

  catalog / import-legacy-catalog / show-scale / export-catalog
  submit / score / publish / report / lookup / import-history / operations
  snapshot / list-reports
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import scoring_client
from catalog import Catalog
from common import (ContractError, atomic_write_json, canonical_json, content_hash,
                    load_json_file, transaction, utc_now)
from scoring_repo import NEW_PRODUCT_FORMAT, ScoringRepository

DEFAULT_CATALOG_DB = "catalog.sqlite3"
DEFAULT_REPO_DB = "scoring.sqlite3"


def _open_catalog(path: str) -> Catalog:
    return Catalog.open(path)


def _open_repo(path: str, catalog: Catalog) -> ScoringRepository:
    return ScoringRepository.open(path, catalog)


def _emit(payload: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_import_legacy_catalog(args: argparse.Namespace) -> dict[str, Any]:
    legacy = load_json_file(args.catalog)
    catalog = _open_catalog(args.catalog_db)
    try:
        with transaction(catalog.conn):
            result = catalog.import_legacy_catalog(legacy)
            catalog._set_meta("catalog_version", f"catalog-{utc_now()}")
    finally:
        catalog.close()
    return result


def cmd_show_scale(args: argparse.Namespace) -> dict[str, Any]:
    catalog = _open_catalog(args.catalog_db)
    try:
        versions = catalog.list_scale_versions(args.scale)
        scales = []
        for summary in versions:
            scale = catalog.require_scale(summary["scale_id"], summary["version"])
            scales.append({
                **summary,
                "entries": [e.__dict__ | {"min": e.min_value, "max": e.max_value}
                            for e in scale.entries],
                "total_min": scale.total_min,
                "total_max": scale.total_max,
                "total_weight": scale.total_weight,
            })
    finally:
        catalog.close()
    return {"scale_id": args.scale, "versions": scales}


def cmd_export_catalog(args: argparse.Namespace) -> dict[str, Any]:
    catalog = _open_catalog(args.catalog_db)
    try:
        document = catalog.exported_document()
    finally:
        catalog.close()
    atomic_write_json(args.output, document)
    return {"output": args.output, "scales": len(document["scales"]),
            "questions": len(document["questions"]),
            "cache_conflicts": len(document["cache_conflicts"])}


def cmd_submit(args: argparse.Namespace) -> dict[str, Any]:
    answers = load_json_file(args.answers_file) if args.answers_file \
        else json.loads(args.answers)
    catalog = _open_catalog(args.catalog_db)
    repo = _open_repo(args.repo_db, catalog)
    try:
        return repo.submit_answers(args.respondent, args.revision, answers)
    finally:
        repo.close()
        catalog.close()


def cmd_score(args: argparse.Namespace) -> dict[str, Any]:
    catalog = _open_catalog(args.catalog_db)
    repo = _open_repo(args.repo_db, catalog)
    try:
        return repo.score_answers(args.respondent, args.revision, args.scale,
                                  args.version)
    finally:
        repo.close()
        catalog.close()


def _parse_selection(text: str) -> dict[str, Any]:
    parts = text.split(":")
    if len(parts) != 4:
        raise ContractError("invalid_selection",
                            "selection must be respondent:revision:scale:version",
                            value=text)
    respondent, revision, scale, version = parts
    return {"respondent_id": respondent, "answer_revision": int(revision),
            "scale_id": scale, "scale_version": int(version)}


def cmd_publish(args: argparse.Namespace) -> dict[str, Any]:
    if args.selections_file:
        selections = load_json_file(args.selections_file)
    else:
        selections = [_parse_selection(text) for text in args.select]
    catalog = _open_catalog(args.catalog_db)
    repo = _open_repo(args.repo_db, catalog)
    try:
        return repo.publish_scores(selections, catalog_version=args.catalog_version,
                                   report_id=args.report_id)
    finally:
        repo.close()
        catalog.close()


def cmd_report(args: argparse.Namespace) -> dict[str, Any]:
    catalog = _open_catalog(args.catalog_db)
    repo = _open_repo(args.repo_db, catalog)
    try:
        document = repo.get_report(args.report_id)
        if document is None:
            raise ContractError("unknown_report", "no such report",
                                report_id=args.report_id)
        return scoring_client.read_document(document)
    finally:
        repo.close()
        catalog.close()


def cmd_export_report(args: argparse.Namespace) -> dict[str, Any]:
    catalog = _open_catalog(args.catalog_db)
    repo = _open_repo(args.repo_db, catalog)
    try:
        document = repo.get_report(args.report_id)
        if document is None:
            raise ContractError("unknown_report", "no such report",
                                report_id=args.report_id)
    finally:
        repo.close()
        catalog.close()
    atomic_write_json(args.output, document)
    return {"output": args.output, "report_id": args.report_id,
            "format": document["format"]}


def cmd_lookup(args: argparse.Namespace) -> dict[str, Any]:
    catalog = _open_catalog(args.catalog_db)
    repo = _open_repo(args.repo_db, catalog)
    try:
        score = repo.lookup_score(args.report_id, args.respondent, args.scale,
                                  args.version, args.revision)
    finally:
        repo.close()
        catalog.close()
    if score is None:
        raise ContractError("unknown_score", "no matching stored score")
    return score


def cmd_import_history(args: argparse.Namespace) -> dict[str, Any]:
    payload = load_json_file(args.history)
    catalog = _open_catalog(args.catalog_db)
    repo = _open_repo(args.repo_db, catalog)
    try:
        return repo.import_history_document(payload, catalog_version=args.catalog_version)
    finally:
        repo.close()
        catalog.close()


def cmd_operations(args: argparse.Namespace) -> dict[str, Any]:
    """Apply an operations journal.  The whole file is one atomic unit."""
    operations = [json.loads(line) for line in open(args.file, encoding="utf-8")
                  if line.strip()]
    catalog = _open_catalog(args.catalog_db)
    repo = _open_repo(args.repo_db, catalog)
    applied: list[dict[str, Any]] = []
    try:
        with transaction(catalog.conn):
            with transaction(repo.conn):
                for index, operation in enumerate(operations, start=1):
                    result = _apply_operation(catalog, repo, operation)
                    applied.append({"index": index,
                                    "operation": operation.get("operation"),
                                    "result": result})
    finally:
        repo.close()
        catalog.close()
    return {"applied": applied}


def _apply_operation(catalog: Catalog, repo: ScoringRepository,
                     operation: dict[str, Any]) -> dict[str, Any]:
    kind = operation.get("operation")
    if kind == "publish_scale":
        return catalog.publish_scale(
            operation.get("scale_id"), operation.get("version"),
            operation.get("entries"), operation.get("missing_weight_limit"),
            origin="operations")
    if kind == "revise_answers":
        return repo.submit_answers(operation.get("respondent_id"),
                                   operation.get("revision"),
                                   operation.get("answers"))
    if kind == "publish_score":
        selection = {
            "respondent_id": operation.get("respondent_id"),
            "answer_revision": operation.get("answer_revision"),
            "scale_id": operation.get("scale_id"),
            "scale_version": operation.get("scale_version"),
        }
        # Two independent SQLite files cannot share a single ACID transaction, so
        # a retry after a crash may replay already-committed steps.  A
        # deterministic report id makes the replay an ``already_published`` no-op
        # instead of creating a duplicate product.
        report_id = operation.get("report_id")
        if report_id is None:
            digest = content_hash(selection)[:16]
            report_id = f"SCORELED_{digest}"
        return repo.publish_scores([selection], report_id=report_id)
    if kind == "import_history":
        raise ContractError("invalid_operation",
                            "import_history takes a file, use the import-history command")
    raise ContractError("unknown_operation", "unsupported operation", operation=kind)


def cmd_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    catalog = _open_catalog(args.catalog_db)
    repo = _open_repo(args.repo_db, catalog)
    try:
        return repo.write_snapshot(args.output, include_history=not args.exclude_history)
    finally:
        repo.close()
        catalog.close()


def cmd_list_reports(args: argparse.Namespace) -> dict[str, Any]:
    catalog = _open_catalog(args.catalog_db)
    repo = _open_repo(args.repo_db, catalog)
    try:
        return {"reports": repo.list_reports()}
    finally:
        repo.close()
        catalog.close()


def cmd_list_questions(args: argparse.Namespace) -> dict[str, Any]:
    catalog = _open_catalog(args.catalog_db)
    try:
        return {"questions": catalog.list_questions()}
    finally:
        catalog.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--catalog-db", default=DEFAULT_CATALOG_DB)
    parser.add_argument("--repo-db", default=DEFAULT_REPO_DB)
    parser.add_argument("--json", action="store_true", help="emit JSON (always on)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("import-legacy-catalog")
    p.add_argument("--catalog", required=True)
    p.set_defaults(func=cmd_import_legacy_catalog)

    p = sub.add_parser("show-scale")
    p.add_argument("--scale", required=True)
    p.set_defaults(func=cmd_show_scale)

    p = sub.add_parser("list-questions")
    p.set_defaults(func=cmd_list_questions)

    p = sub.add_parser("export-catalog")
    p.add_argument("--output", required=True)
    p.set_defaults(func=cmd_export_catalog)

    p = sub.add_parser("submit")
    p.add_argument("--respondent", required=True)
    p.add_argument("--revision", type=int, required=True)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--answers")
    source.add_argument("--answers-file")
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("score")
    p.add_argument("--respondent", required=True)
    p.add_argument("--revision", type=int, required=True)
    p.add_argument("--scale", required=True)
    p.add_argument("--version", type=int, required=True)
    p.set_defaults(func=cmd_score)

    p = sub.add_parser("publish")
    p.add_argument("--select", action="append", default=[],
                   help="respondent:revision:scale:version")
    p.add_argument("--selections-file")
    p.add_argument("--report-id")
    p.add_argument("--catalog-version")
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("report")
    p.add_argument("--report-id", required=True)
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("export-report")
    p.add_argument("--report-id", required=True)
    p.add_argument("--output", required=True)
    p.set_defaults(func=cmd_export_report)

    p = sub.add_parser("lookup")
    p.add_argument("--report-id", required=True)
    p.add_argument("--respondent", required=True)
    p.add_argument("--scale", required=True)
    p.add_argument("--version", type=int, required=True)
    p.add_argument("--revision", type=int, required=True)
    p.set_defaults(func=cmd_lookup)

    p = sub.add_parser("import-history")
    p.add_argument("--history", required=True)
    p.add_argument("--catalog-version")
    p.set_defaults(func=cmd_import_history)

    p = sub.add_parser("operations")
    p.add_argument("--file", required=True)
    p.set_defaults(func=cmd_operations)

    p = sub.add_parser("snapshot")
    p.add_argument("--output", required=True)
    p.add_argument("--exclude-history", action="store_true")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("list-reports")
    p.set_defaults(func=cmd_list_reports)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = args.func(args)
    except ContractError as exc:
        print(json.dumps({"error": exc.to_dict()}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
