"""Command-line entry points: independent code lookup, calibration, history.

Usage examples (all state lives in one SQLite file):

    python -m scale_calibration --db cal.db load materials/759/input.json
    python -m scale_calibration --db cal.db interpret K 1 6
    python -m scale_calibration --db cal.db read A 1
    python -m scale_calibration --db cal.db read-history B
    python -m scale_calibration --db cal.db calibrate --request-id G2
    python -m scale_calibration --db cal.db product G2
    python -m scale_calibration --db cal.db samples G2
    python -m scale_calibration --db cal.db products
    python -m scale_calibration --db cal.db demo materials/759/input.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .catalog import Catalog, CatalogError, IllegalCodeError, NotFoundError
from .calibration import Calibrator, CalibrationInputError, InfeasibleError
from .store import Store


def _dump(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=False))


def _open(db: str) -> tuple[Store, Catalog, Calibrator]:
    store = Store(db)
    catalog = Catalog(store)
    calibrator = Calibrator(store, catalog)
    return store, catalog, calibrator


def _load_payload(catalog: Catalog, payload: dict) -> dict:
    created_contracts = 0
    created_reads = 0
    skipped = 0
    for c in payload.get("scale_contracts", []):
        result = catalog.add_contract(
            c["scale_id"], c["revision"], c["base"], c["p"], c["q"], c["lo"], c["hi"]
        )
        created_contracts += 1 if result["created"] else 0
    for r in payload.get("reads", []):
        result = catalog.add_read(
            r["read_id"], r["revision"], r["point"],
            r["scale_id"], r["scale_revision"], r["code"],
        )
        created_reads += 1 if result["created"] else 0
        skipped += 0 if result["created"] else 1
    return {
        "contracts_created": created_contracts,
        "reads_created": created_reads,
        "reads_already_present": skipped,
    }


def cmd_load(args) -> int:
    store, catalog, _ = _open(args.db)
    with store:
        payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
        _dump(_load_payload(catalog, payload))
    return 0


def cmd_interpret(args) -> int:
    store, catalog, _ = _open(args.db)
    with store:
        try:
            _dump(catalog.interpret_code(args.scale_id, args.revision, args.code))
        except (NotFoundError, IllegalCodeError, CatalogError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    return 0


def cmd_contracts(args) -> int:
    store, catalog, _ = _open(args.db)
    with store:
        _dump([c.__dict__ for c in catalog.list_contracts(args.scale_id)])
    return 0


def cmd_read(args) -> int:
    store, catalog, _ = _open(args.db)
    with store:
        try:
            _dump(catalog.interpret_read(args.read_id, args.revision))
        except NotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    return 0


def cmd_read_history(args) -> int:
    store, catalog, _ = _open(args.db)
    with store:
        try:
            _dump(catalog.list_read_revisions(args.read_id))
        except NotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    return 0


def _request_from_payload(payload: dict, request_id: str) -> dict:
    for r in payload.get("requests", []):
        if r.get("request_id") == request_id:
            return r
    raise KeyError(request_id)


def cmd_calibrate(args) -> int:
    store, catalog, calibrator = _open(args.db)
    with store:
        if args.request_file:
            request = json.loads(Path(args.request_file).read_text(encoding="utf-8"))
        elif args.file:
            payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
            request = _request_from_payload(payload, args.request_id)
        elif args.request_json:
            request = json.loads(args.request_json)
        else:
            print("error: supply --request-file, --request-json or --file", file=sys.stderr)
            return 2
        try:
            _dump(calibrator.submit(request))
        except InfeasibleError as exc:
            _dump({"status": "INFEASIBLE", "reasons": exc.reasons,
                   "detail": exc.detail.get("points", {})})
            return 3
        except CalibrationInputError as exc:
            print(f"input error: {exc}", file=sys.stderr)
            return 2
    return 0


def cmd_product(args) -> int:
    store, _, calibrator = _open(args.db)
    with store:
        try:
            _dump(calibrator.get_product(args.request_id))
        except NotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    return 0


def cmd_samples(args) -> int:
    store, _, calibrator = _open(args.db)
    with store:
        try:
            _dump(calibrator.get_samples(args.request_id))
        except NotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    return 0


def cmd_products(args) -> int:
    store, _, calibrator = _open(args.db)
    with store:
        _dump(calibrator.list_products())
    return 0


def cmd_demo(args) -> int:
    """End-to-end walkthrough of the bundled synthetic input, with commentary."""
    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    db = args.db if args.db != ":memory:" else ":memory:"
    store, catalog, calibrator = _open(db)
    with store:
        print("== load contracts and reads ==")
        _dump(_load_payload(catalog, payload))

        print("\n== independent code-value lookups ==")
        for scale_id, revision, code in [("K", 1, 6), ("K", 1, 14),
                                         ("K", 2, 14), ("K", 1, 4), ("K", 1, 16)]:
            _dump(catalog.interpret_code(scale_id, revision, code))

        print("\n== stored read interpretations and history ==")
        for read_id, revision in [("A", 1), ("B", 1), ("B", 2), ("AX", 1), ("BX", 1)]:
            _dump(catalog.interpret_read(read_id, revision))
        _dump(catalog.list_read_revisions("B"))

        print("\n== calibrations ==")
        for request in payload.get("requests", []):
            rid = request["request_id"]
            print(f"-- {rid} --")
            try:
                _dump(calibrator.submit(request))
            except InfeasibleError as exc:
                _dump({"request_id": rid, "status": "INFEASIBLE",
                       "reasons": exc.reasons})
            except CalibrationInputError as exc:
                _dump({"request_id": rid, "status": "INPUT_ERROR",
                       "error": str(exc)})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scale_calibration")
    parser.add_argument("--db", default="calibration.db", help="SQLite database path")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("load", help="load contracts + reads from a JSON payload")
    p.add_argument("file")
    p.set_defaults(func=cmd_load)

    p = sub.add_parser("interpret", help="independent code-value lookup")
    p.add_argument("scale_id")
    p.add_argument("revision", type=int)
    p.add_argument("code", type=int)
    p.set_defaults(func=cmd_interpret)

    p = sub.add_parser("contracts", help="list scale contracts")
    p.add_argument("scale_id", nargs="?")
    p.set_defaults(func=cmd_contracts)

    p = sub.add_parser("read", help="look up one stored read revision")
    p.add_argument("read_id")
    p.add_argument("revision", type=int)
    p.set_defaults(func=cmd_read)

    p = sub.add_parser("read-history", help="all revisions of one read id")
    p.add_argument("read_id")
    p.set_defaults(func=cmd_read_history)

    p = sub.add_parser("calibrate", help="run a calibration request")
    p.add_argument("--file", help="JSON payload containing requests[]")
    p.add_argument("--request-file", help="JSON file containing a single request")
    p.add_argument("--request-json", help="inline JSON of a single request")
    p.add_argument("--request-id", help="request_id to pick from --file")
    p.set_defaults(func=cmd_calibrate)

    p = sub.add_parser("product", help="read a stored calibration product")
    p.add_argument("request_id")
    p.set_defaults(func=cmd_product)

    p = sub.add_parser("samples", help="read stored corrected samples")
    p.add_argument("request_id")
    p.set_defaults(func=cmd_samples)

    p = sub.add_parser("products", help="list stored calibration products")
    p.set_defaults(func=cmd_products)

    p = sub.add_parser("demo", help="run the bundled synthetic example end to end")
    p.add_argument("file")
    p.set_defaults(func=cmd_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
