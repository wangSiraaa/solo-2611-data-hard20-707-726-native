"""Command line interface for gradaudit.

Two groups of commands:

Stateless (operate on JSON files directly)
    verify     -- independent verify-plan entry point (never calls the solver)
    solve      -- exact feasibility solver, prints a course-allocation/1 plan
    candidates -- list a transfer course's candidate local courses + categories

Persistent (SQLite-backed catalog / record / audit library)
    import-catalog / import-records / import-requirements
    prepare / confirm / status / reopen / history / list-*
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Optional

from .errors import GradAuditError
from .library import Store
from .model import parse_catalog, parse_records, parse_requirements, parse_plan
from .solver import solve
from .verify import verify_plan, verify_plan_document


def _load(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _dump(obj: Any) -> None:
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2, sort_keys=False)
    sys.stdout.write("\n")


def _inputs(args) -> tuple:
    catalog = parse_catalog(_load(args.catalog))
    records = parse_records(_load(args.records))
    requirements = parse_requirements(_load(args.requirements))
    return catalog, records, requirements


# --------------------------------------------------------------------------- #
# stateless commands
# --------------------------------------------------------------------------- #
def cmd_verify(args) -> int:
    catalog, records, requirements = _inputs(args)
    plan_doc = _load(args.plan)
    result = verify_plan_document(plan_doc, catalog, records, requirements)
    _dump(result.as_dict())
    return 0 if result.accepted else 1


def cmd_solve(args) -> int:
    catalog, records, requirements = _inputs(args)
    result = solve(catalog, records, requirements)
    _dump(result.as_dict())
    if args.emit_plan and result.feasible:
        # write the plan next to the summary when requested
        with open(args.emit_plan, "w", encoding="utf-8") as fh:
            json.dump(result.as_plan_document(), fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    return 0 if result.feasible else 1


def cmd_candidates(args) -> int:
    catalog = parse_catalog(_load(args.catalog))
    _dump({
        "catalog_revision": catalog.revision,
        "external_course": args.external_course,
        "candidates": catalog.candidates_with_categories(args.external_course),
    })
    return 0


# --------------------------------------------------------------------------- #
# persistent commands
# --------------------------------------------------------------------------- #
def _store(args) -> Store:
    return Store(args.db)


def cmd_import_catalog(args) -> int:
    with _store(args) as store:
        catalog = store.import_catalog(_load(args.file))
        _dump({"imported": "catalog", "revision": catalog.revision, "content_hash": catalog.content_hash()})
    return 0


def cmd_import_records(args) -> int:
    with _store(args) as store:
        records = store.import_records(_load(args.file))
        _dump({"imported": "records", "revision": records.revision, "student_id": records.student_id,
               "content_hash": records.content_hash()})
    return 0


def cmd_import_requirements(args) -> int:
    with _store(args) as store:
        req = store.import_requirements(_load(args.file))
        _dump({"imported": "requirements", "revision": req.revision, "request_id": req.request_id,
               "content_hash": req.content_hash()})
    return 0


def cmd_list_candidates(args) -> int:
    with _store(args) as store:
        _dump({"catalog_revision": args.catalog_revision,
               "external_course": args.external_course,
               "candidates": store.list_candidates(args.catalog_revision, args.external_course)})
    return 0


def cmd_prepare(args) -> int:
    with _store(args) as store:
        plan_doc = _load(args.plan)
        result = store.prepare_audit(
            plan_doc,
            request_id=args.request,
            catalog_revision=args.catalog_revision,
            records_revision=args.records_revision,
            requirements_revision=args.requirements_revision,
        )
        _dump(result)
        return 0 if result["accepted"] else 1


def cmd_confirm(args) -> int:
    with _store(args) as store:
        _dump(store.confirm_audit(request_id=args.request, preparation_id=args.preparation))
    return 0


def cmd_status(args) -> int:
    with _store(args) as store:
        _dump(store.confirmation_status(args.request))
    return 0


def cmd_reopen(args) -> int:
    with _store(args) as store:
        _dump(store.reopen_audit(request_id=args.request, reason=args.reason))
    return 0


def cmd_history(args) -> int:
    with _store(args) as store:
        _dump({
            "request_id": args.request,
            "history": store.history(args.request),
            "preparations": store.list_preparations(args.request),
            "confirmations": store.list_confirmations(args.request),
        })
    return 0


def cmd_points(args) -> int:
    with _store(args) as store:
        _dump({"preparation_id": args.preparation,
               "points": store.preparation_points(args.preparation)})
    return 0


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gradaudit", description="Course-equivalence catalog and graduation audit library")
    sub = p.add_subparsers(dest="command", required=True)

    def add_inputs(sp):
        sp.add_argument("--catalog", required=True, help="course-catalog/1 JSON file")
        sp.add_argument("--records", required=True, help="student records JSON file")
        sp.add_argument("--requirements", required=True, help="requirements JSON file")

    # verify
    sp = sub.add_parser("verify", help="independently verify a course-allocation/1 plan")
    sp.add_argument("--plan", required=True)
    add_inputs(sp)
    sp.set_defaults(func=cmd_verify)

    # solve
    sp = sub.add_parser("solve", help="exactly solve feasibility and emit an allocation")
    add_inputs(sp)
    sp.add_argument("--emit-plan", help="optional path to write a course-allocation/1 plan")
    sp.set_defaults(func=cmd_solve)

    # candidates (stateless)
    sp = sub.add_parser("candidates", help="list candidate local courses for an external course")
    sp.add_argument("external_course")
    sp.add_argument("--catalog", required=True)
    sp.set_defaults(func=cmd_candidates)

    # imports
    sp = sub.add_parser("import-catalog", help="import a catalog revision into the library")
    sp.add_argument("file")
    sp.add_argument("--db", default="gradaudit.sqlite3")
    sp.set_defaults(func=cmd_import_catalog)

    sp = sub.add_parser("import-records", help="import a student-records revision")
    sp.add_argument("file")
    sp.add_argument("--db", default="gradaudit.sqlite3")
    sp.set_defaults(func=cmd_import_records)

    sp = sub.add_parser("import-requirements", help="import a requirements revision")
    sp.add_argument("file")
    sp.add_argument("--db", default="gradaudit.sqlite3")
    sp.set_defaults(func=cmd_import_requirements)

    sp = sub.add_parser("list-candidates", help="list candidates from the library catalog")
    sp.add_argument("catalog_revision", type=int)
    sp.add_argument("external_course")
    sp.add_argument("--db", default="gradaudit.sqlite3")
    sp.set_defaults(func=cmd_list_candidates)

    # audit lifecycle
    sp = sub.add_parser("prepare", help="independently verify a plan and persist a preparation")
    sp.add_argument("--plan", required=True)
    sp.add_argument("--request", required=True)
    sp.add_argument("--catalog-revision", type=int, required=True)
    sp.add_argument("--records-revision", type=int, required=True)
    sp.add_argument("--requirements-revision", type=int, required=True)
    sp.add_argument("--db", default="gradaudit.sqlite3")
    sp.set_defaults(func=cmd_prepare)

    sp = sub.add_parser("confirm", help="confirm the latest (or a given) accepted preparation")
    sp.add_argument("--request", required=True)
    sp.add_argument("--preparation", default=None)
    sp.add_argument("--db", default="gradaudit.sqlite3")
    sp.set_defaults(func=cmd_confirm)

    sp = sub.add_parser("status", help="show confirmation status (including staleness)")
    sp.add_argument("--request", required=True)
    sp.add_argument("--db", default="gradaudit.sqlite3")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("reopen", help="explicitly reopen a confirmed audit")
    sp.add_argument("--request", required=True)
    sp.add_argument("--reason", required=True)
    sp.add_argument("--db", default="gradaudit.sqlite3")
    sp.set_defaults(func=cmd_reopen)

    sp = sub.add_parser("history", help="show full audit history for a request")
    sp.add_argument("--request", required=True)
    sp.add_argument("--db", default="gradaudit.sqlite3")
    sp.set_defaults(func=cmd_history)

    sp = sub.add_parser("points", help="show the point destinations stored for a preparation")
    sp.add_argument("preparation_id")
    sp.add_argument("--db", default="gradaudit.sqlite3")
    sp.set_defaults(func=cmd_points)

    return p


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except GradAuditError as exc:
        _dump({"error": exc.as_reason()})
        return 2
    except FileNotFoundError as exc:
        _dump({"error": {"code": "file_not_found", "message": str(exc)}})
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
