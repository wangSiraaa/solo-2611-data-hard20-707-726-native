"""Command-line entry points for the cumulative billing service and top-up store.

Examples::

    python -m cumulative_billing.cli init --db billing.db --contract contract.json
    python -m cumulative_billing.cli import-usage --db billing.db --usage usage.jsonl
    python -m cumulative_billing.cli import-bills --db billing.db --bills issued_bills.jsonl
    python -m cumulative_billing.cli revise-usage --db billing.db \
        --account AC1 --year 2025 --period 1 --revision 2 --quantity 60
    python -m cumulative_billing.cli recalc --db billing.db \
        --account AC1 --year 2025 --through 3 --save-as ADJ1
    python -m cumulative_billing.cli reassess --db billing.db --account AC1 --year 2025
    python -m cumulative_billing.cli export --db billing.db --out out/bills.jsonl \
        --account AC1 --year 2025

The exported JSONL is period-bill/1, so the read-only client reads it unchanged::

    python3 materials/754/bill_reader.py out/bills.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys

from .contract import Contract
from .documents import load_jsonl
from .errors import BillingError
from .pricing import PricingService, UsageRevision
from .repository import BillRepository


def _repo(args) -> BillRepository:
    return BillRepository(args.db)


def cmd_init(args) -> int:
    contract = Contract.from_dict(json.loads(open(args.contract, encoding="utf-8").read()))
    repo = _repo(args)
    repo.store_contract(contract)
    repo.close()
    print(f"stored contract {contract.contract_id}@{contract.revision}")
    return 0


def cmd_import_usage(args) -> int:
    repo = _repo(args)
    count = 0
    for payload in load_jsonl(args.usage):
        repo.add_usage_revision(UsageRevision(**payload))
        count += 1
    repo.close()
    print(f"imported {count} usage revision(s)")
    return 0


def cmd_revise_usage(args) -> int:
    repo = _repo(args)
    repo.add_usage_revision(
        UsageRevision(
            account_id=args.account,
            year=args.year,
            period=args.period,
            revision=args.revision,
            quantity=args.quantity,
        )
    )
    repo.close()
    print(f"recorded usage revision {args.account}/{args.year}/{args.period}#{args.revision}")
    return 0


def cmd_import_bills(args) -> int:
    repo = _repo(args)
    docs = repo.import_documents(args.bills)
    repo.close()
    print(f"imported {len(docs)} document(s)")
    return 0


def cmd_recalc(args) -> int:
    repo = _repo(args)
    result = repo.recalculate_and_issue_adjustments(
        args.account,
        args.year,
        args.through,
        save_as=args.save_as,
        contract_id=args.contract_id,
        contract_revision=args.contract_revision,
    )
    repo.close()
    for ledger in result.periods:
        print(
            f"P{ledger.period} original={ledger.original_amount_cents} "
            f"adjust={ledger.adjustment_cents} net={ledger.net_issued_cents} "
            f"target={ledger.target_fee_cents} delta={ledger.delta_cents} "
            f"status={ledger.status}"
        )
    for doc in result.issued:
        print(f"ISSUED {doc.document_id} P{doc.period} {doc.amount_cents:+d}")
    print(
        f"run {result.run_id} issued {len(result.issued)} adjustment(s), "
        f"net adjustment {result.net_adjustment_cents:+d}"
    )
    return 0


def cmd_reassess(args) -> int:
    repo = _repo(args)
    contract = repo.pricing_for_year(args.account, args.year).contract
    statement = PricingService(contract).assess_year(
        args.account, args.year, repo.usage_for_year(args.account, args.year)
    )
    repo.close()
    print(json.dumps(statement.to_dict(), indent=2))
    return 0


def cmd_export(args) -> int:
    repo = _repo(args)
    count = repo.export_jsonl(args.out, account_id=args.account, year=args.year)
    repo.close()
    print(f"exported {count} document(s) to {args.out}")
    return 0


def cmd_replay(args) -> int:
    """Replay a synthetic operations.jsonl against a fresh database."""
    repo = _repo(args)
    if args.contract:
        repo.store_contract(
            Contract.from_dict(json.loads(open(args.contract, encoding="utf-8").read()))
        )
    if args.usage:
        for payload in load_jsonl(args.usage):
            repo.add_usage_revision(UsageRevision(**payload))
    for op in load_jsonl(args.operations):
        name = op.get("operation")
        if name == "import_issued_bills":
            docs = repo.import_documents(args.bills)
            print(f"imported {len(docs)} issued document(s)")
        elif name == "revise_usage":
            repo.add_usage_revision(UsageRevision(**op["record"]))
            rec = op["record"]
            print(
                f"revised usage {rec['account_id']}/{rec['year']}/"
                f"{rec['period']}#{rec['revision']} = {rec['quantity']}"
            )
        elif name == "recalculate_and_issue_adjustments":
            result = repo.recalculate_and_issue_adjustments(
                op["account_id"], op["year"], op["through_period"],
                save_as=op.get("save_as"),
            )
            if not result.issued:
                print(
                    f"recalc {result.account_id}/{result.year} through {result.through_period}: "
                    f"no new adjustments ({op.get('expect', 'no_duplicate_adjustments')})"
                )
            else:
                for doc in result.issued:
                    print(f"ISSUED {doc.document_id} P{doc.period} {doc.amount_cents:+d}")
                print(f"net adjustment {result.net_adjustment_cents:+d}")
        elif name == "read_original_bills_and_adjustments":
            for doc in repo.documents(op.get("account_id"), op.get("year")):
                print(
                    doc.document_id,
                    doc.document_type,
                    doc.period,
                    doc.amount_cents,
                    doc.previous_statement_id,
                )
        else:
            raise ValueError(f"unknown operation: {name!r}")
    repo.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="cumulative_billing")
    sub = ap.add_subparsers(dest="command", required=True)

    def add_db(p):
        p.add_argument("--db", required=True)

    p = sub.add_parser("init", help="store a marginal-band contract")
    add_db(p)
    p.add_argument("--contract", required=True)
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("import-usage", help="import usage revision JSONL")
    add_db(p)
    p.add_argument("--usage", required=True)
    p.set_defaults(func=cmd_import_usage)

    p = sub.add_parser("revise-usage", help="record one usage revision")
    add_db(p)
    p.add_argument("--account", required=True)
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--period", type=int, required=True)
    p.add_argument("--revision", type=int, required=True)
    p.add_argument("--quantity", type=int, required=True)
    p.set_defaults(func=cmd_revise_usage)

    p = sub.add_parser("import-bills", help="import issued period-bill/1 JSONL")
    add_db(p)
    p.add_argument("--bills", required=True)
    p.set_defaults(func=cmd_import_bills)

    p = sub.add_parser("recalc", help="recalculate and issue signed adjustments")
    add_db(p)
    p.add_argument("--account", required=True)
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--through", type=int, required=True)
    p.add_argument("--save-as", default=None)
    p.add_argument("--contract-id", default=None)
    p.add_argument("--contract-revision", type=int, default=None)
    p.set_defaults(func=cmd_recalc)

    p = sub.add_parser("reassess", help="print the current cumulative statement")
    add_db(p)
    p.add_argument("--account", required=True)
    p.add_argument("--year", type=int, required=True)
    p.set_defaults(func=cmd_reassess)

    p = sub.add_parser("export", help="export stored documents as period-bill/1 JSONL")
    add_db(p)
    p.add_argument("--out", required=True)
    p.add_argument("--account", default=None)
    p.add_argument("--year", type=int, default=None)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("replay", help="replay an operations.jsonl scenario")
    add_db(p)
    p.add_argument("--contract", default=None)
    p.add_argument("--usage", default=None)
    p.add_argument("--bills", required=True)
    p.add_argument("--operations", required=True)
    p.set_defaults(func=cmd_replay)

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except BillingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
