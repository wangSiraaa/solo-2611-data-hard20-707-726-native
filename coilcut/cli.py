"""Command-line interface for the coil cut ledger."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .errors import CoilCutError
from .operations import initialize_from_stock, load_stock, run_operations_file
from .storage import Ledger


def _dump(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def cmd_init(args: argparse.Namespace) -> int:
    stock = load_stock(args.stock)
    with Ledger(args.db) as ledger:
        initialize_from_stock(ledger, stock)
    print(f"initialized {args.db} from {args.stock}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    stock = load_stock(args.stock)
    db_path = Path(args.db)
    if args.reset:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(db_path) + suffix)
            if candidate.exists():
                candidate.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with Ledger(args.db) as ledger:
        initialize_from_stock(ledger, stock)
        results = run_operations_file(ledger, args.operations)
        for entry in results:
            _dump(entry)
        if args.delivery_dir:
            out = Path(args.delivery_dir)
            out.mkdir(parents=True, exist_ok=True)
            for delivery in ledger.deliveries():
                manifest = delivery["manifest"]
                (out / f"{manifest['delivery_id']}.json").write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
                )
                print(f"wrote {out / (manifest['delivery_id'] + '.json')}")
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    with Ledger(args.db) as ledger:
        if args.piece:
            _dump(ledger.lineage(args.piece))
        elif args.root:
            _dump(ledger.root_report(args.root))
        elif args.cut:
            _dump(ledger.replay_cut(args.cut))
        elif args.delivery:
            _dump(ledger.replay_delivery(args.delivery))
        else:
            _dump({"deliveries": ledger.deliveries()})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coilcut", description="One-dimensional coil cutting geometry + ledger"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="create a ledger from a stock.json")
    p_init.add_argument("--db", required=True)
    p_init.add_argument("--stock", required=True)
    p_init.set_defaults(func=cmd_init)

    p_run = sub.add_parser("run", help="replay operations.jsonl into a fresh ledger")
    p_run.add_argument("--db", required=True)
    p_run.add_argument("--stock", required=True)
    p_run.add_argument("--operations", required=True)
    p_run.add_argument("--delivery-dir", default=None)
    p_run.add_argument("--reset", action="store_true", default=True)
    p_run.set_defaults(func=cmd_run)

    p_query = sub.add_parser("query", help="inspect lineage, root coverage or a delivery")
    p_query.add_argument("--db", required=True)
    group = p_query.add_mutually_exclusive_group()
    group.add_argument("--piece")
    group.add_argument("--root")
    group.add_argument("--cut")
    group.add_argument("--delivery")
    p_query.set_defaults(func=cmd_query)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except CoilCutError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
