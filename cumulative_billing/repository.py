"""Bill repository: immutable issued bills + signed adjustments (the top-up store).

This module genuinely **consumes** the pricing service: an issued bill was
priced from ``F``; a recalculation asks the pricing service for each period's
*current* fee and emits signed deltas against what was already issued.

Semantics:

* Imported bills are stored verbatim (ID, amount, source) and never rewritten.
* A recalculation over periods ``1..through_period`` compares each period's
  current fee to that period's **net issued amount** (original bill plus all
  adjustments stated against it).  Only non-zero deltas become ASJUSTMENT
  documents, so unchanged periods produce nothing and re-running the same
  recalculation produces no duplicate adjustments.
* Because a period fee is ``F(end) - F(start)``, revising an earlier period
  shifts every later cumulative position: the delta propagates to all affected
  later periods, and untouched later periods emit no delta.
* Further corrections are computed relative to the *net* (bill + prior
  adjustments), never relative to the original bill amount again.
* ``previous_statement_id`` on an adjustment always names the original bill;
  adjustment identity is ``(account, year, period, sequence)``.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from .contract import Contract
from .documents import ADJUSTMENT, BILL, BillDocument, load_jsonl_raw
from .errors import DocumentError, IncompleteChainError, UsageError
from .pricing import MAX_PERIOD, PricingService, UsageRevision
from .store import connect


@dataclass(frozen=True)
class PeriodLedger:
    period: int
    original_amount_cents: int | None     # None when no bill was issued
    adjustment_cents: int                 # sum of adjustments for this period
    net_issued_cents: int | None          # original + adjustments, if a bill exists
    target_fee_cents: int | None          # current fee from pricing
    delta_cents: int | None               # target - net, if both known
    status: str                           # no_bill | unconfirmed | unchanged | adjusted


@dataclass(frozen=True)
class RecalcResult:
    run_id: str
    account_id: str
    year: int
    through_period: int
    save_as: str | None
    issued: list[BillDocument]
    periods: list[PeriodLedger]
    complete_through: int
    gap_periods: list[int]

    @property
    def net_adjustment_cents(self) -> int:
        return sum(d.amount_cents for d in self.issued)


class BillRepository:
    def __init__(self, db_path: str, pricing: PricingService | None = None):
        self.db_path = db_path
        self.conn = connect(db_path)
        self._pricing_override = pricing

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ #
    # Contracts and usage
    # ------------------------------------------------------------------ #
    def store_contract(self, contract: Contract) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO contracts (contract_id, revision, payload) VALUES (?,?,?)",
            (contract.contract_id, contract.revision, json.dumps(contract.to_dict())),
        )
        self.conn.commit()

    def load_contract(self, contract_id: str, revision: int) -> Contract:
        row = self.conn.execute(
            "SELECT payload FROM contracts WHERE contract_id=? AND revision=?",
            (contract_id, revision),
        ).fetchone()
        if row is None:
            raise DocumentError(f"unknown contract {contract_id}@{revision}")
        return Contract.from_dict(json.loads(row["payload"]))

    def add_usage_revision(self, usage: UsageRevision) -> None:
        usage.validate()
        row = self.conn.execute(
            "SELECT quantity FROM usage_revisions "
            "WHERE account_id=? AND year=? AND period=? AND revision=?",
            (usage.account_id, usage.year, usage.period, usage.revision),
        ).fetchone()
        if row is not None:
            if row["quantity"] != usage.quantity:
                raise UsageError(
                    f"usage revision {usage.account_id}/{usage.year}/"
                    f"{usage.period}#{usage.revision} already exists with a different quantity"
                )
            return  # idempotent re-insert of the same revision
        self.conn.execute(
            "INSERT INTO usage_revisions (account_id, year, period, revision, quantity) "
            "VALUES (?,?,?,?,?)",
            (usage.account_id, usage.year, usage.period, usage.revision, usage.quantity),
        )
        self.conn.commit()

    def usage_for_year(self, account_id: str, year: int) -> list[UsageRevision]:
        rows = self.conn.execute(
            "SELECT account_id, year, period, revision, quantity FROM usage_revisions "
            "WHERE account_id=? AND year=? ORDER BY period, revision",
            (account_id, year),
        ).fetchall()
        return [UsageRevision(**dict(r)) for r in rows]

    def confirmed_usage_revisions(self, account_id: str, year: int) -> dict[int, int]:
        """Highest confirmed revision per period."""
        rows = self.conn.execute(
            "SELECT period, MAX(revision) AS rev FROM usage_revisions "
            "WHERE account_id=? AND year=? GROUP BY period",
            (account_id, year),
        ).fetchall()
        return {r["period"]: r["rev"] for r in rows}

    # ------------------------------------------------------------------ #
    # Document storage
    # ------------------------------------------------------------------ #
    def import_documents(self, path: str) -> list[BillDocument]:
        """Import period-bill/1 JSONL, preserving IDs, amounts and raw text."""
        pairs = load_jsonl_raw(path)
        return self.import_document_dicts(
            [payload for _, payload in pairs],
            raw_lines=[raw for raw, _ in pairs],
        )

    def import_document_dicts(
        self, payloads: list[dict], raw_lines: list[str] | None = None
    ) -> list[BillDocument]:
        """Import period-bill/1 documents, preserving IDs and amounts."""
        if raw_lines is not None and len(raw_lines) != len(payloads):
            raise DocumentError("raw_lines must align one-to-one with payloads")
        imported = []
        existing = self._existing_ids()
        for i, payload in enumerate(payloads):
            doc = BillDocument.from_dict(payload, source="imported")
            if doc.document_id in existing:
                raise DocumentError(f"duplicate document ID: {doc.document_id}")
            if doc.document_type == ADJUSTMENT and doc.previous_statement_id not in existing:
                raise DocumentError(
                    f"adjustment {doc.document_id} references unknown bill "
                    f"{doc.previous_statement_id}"
                )
            raw = raw_lines[i] if raw_lines is not None else doc.to_json()
            if not isinstance(raw, str) or not raw.strip():
                raise DocumentError(f"empty source text for document {doc.document_id}")
            self._insert_document(doc, raw_payload=raw)
            if doc.document_type == ADJUSTMENT:
                # Register imported adjustments so sequence numbering and the
                # net-issued view treat them like generated ones.
                sequence = self._next_sequence(doc.account_id, doc.year, doc.period)
                self.conn.execute(
                    "INSERT INTO adjustments (document_id, account_id, year, period, "
                    "sequence, previous_statement_id, target_amount_cents, recalc_run_id) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (
                        doc.document_id,
                        doc.account_id,
                        doc.year,
                        doc.period,
                        sequence,
                        doc.previous_statement_id,
                        self._net_after_import(doc),
                        "imported",
                    ),
                )
            existing.add(doc.document_id)
            imported.append(doc)
        self.conn.commit()
        return imported

    def _existing_ids(self) -> set[str]:
        return {
            r["document_id"]
            for r in self.conn.execute("SELECT document_id FROM documents").fetchall()
        }

    def _insert_document(self, doc: BillDocument, *, raw_payload: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO documents "
            "(document_id, format, document_type, account_id, year, period, amount_cents, "
            " contract_revision, usage_revisions, previous_statement_id, source, payload) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                doc.document_id,
                "period-bill/1",
                doc.document_type,
                doc.account_id,
                doc.year,
                doc.period,
                doc.amount_cents,
                doc.contract_revision,
                json.dumps({str(k): v for k, v in sorted(doc.usage_revisions.items())}),
                doc.previous_statement_id,
                doc.source,
                raw_payload if raw_payload is not None else doc.to_json(),
            ),
        )

    def documents(
        self, account_id: str | None = None, year: int | None = None
    ) -> list[BillDocument]:
        """All documents, bills before adjustments within each period."""
        query = "SELECT payload FROM documents"
        clauses, params = [], []
        if account_id is not None:
            clauses.append("account_id=?")
            params.append(account_id)
        if year is not None:
            clauses.append("year=?")
            params.append(year)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += (
            " ORDER BY year, period, "
            "CASE document_type WHEN 'BILL' THEN 0 ELSE 1 END, document_id"
        )
        rows = self.conn.execute(query, params).fetchall()
        return [BillDocument.from_dict(json.loads(r["payload"])) for r in rows]

    def bills_for_year(self, account_id: str, year: int) -> list[BillDocument]:
        rows = self.conn.execute(
            "SELECT payload FROM documents "
            "WHERE account_id=? AND year=? AND document_type=? ORDER BY period",
            (account_id, year, BILL),
        ).fetchall()
        return [BillDocument.from_dict(json.loads(r["payload"])) for r in rows]

    def adjustments_for_year(self, account_id: str, year: int) -> list[BillDocument]:
        rows = self.conn.execute(
            "SELECT payload FROM documents "
            "WHERE account_id=? AND year=? AND document_type=? "
            "ORDER BY period, document_id",
            (account_id, year, ADJUSTMENT),
        ).fetchall()
        return [BillDocument.from_dict(json.loads(r["payload"])) for r in rows]

    def export_jsonl(self, path: str, *, account_id: str | None = None,
                     year: int | None = None) -> int:
        """Write stored documents (imported + adjustments) as period-bill/1 JSONL.

        Imported documents are emitted from their stored raw text, so the old
        bill lines are unchanged; generated adjustments use canonical JSON.
        """
        query = "SELECT payload FROM documents"
        clauses, params = [], []
        if account_id is not None:
            clauses.append("account_id=?")
            params.append(account_id)
        if year is not None:
            clauses.append("year=?")
            params.append(year)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += (
            " ORDER BY year, period, "
            "CASE document_type WHEN 'BILL' THEN 0 ELSE 1 END, document_id"
        )
        rows = self.conn.execute(query, params).fetchall()
        with open(path, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(row["payload"] + "\n")
        return len(rows)

    # ------------------------------------------------------------------ #
    # Net issued amounts
    # ------------------------------------------------------------------ #
    def _adjustment_sum(self, account_id: str, year: int, period: int) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM documents "
            "WHERE account_id=? AND year=? AND period=? AND document_type=?",
            (account_id, year, period, ADJUSTMENT),
        ).fetchone()
        return row["total"]

    def net_issued_amount(self, account_id: str, year: int, period: int) -> int | None:
        """Original bill plus all adjustments for this period, or None if no bill."""
        bill = self._bill_for_period(account_id, year, period)
        if bill is None:
            return None
        return bill.amount_cents + self._adjustment_sum(account_id, year, period)

    def _bill_for_period(self, account_id: str, year: int, period: int) -> BillDocument | None:
        row = self.conn.execute(
            "SELECT payload FROM documents "
            "WHERE account_id=? AND year=? AND period=? AND document_type=?",
            (account_id, year, period, BILL),
        ).fetchone()
        return BillDocument.from_dict(json.loads(row["payload"])) if row else None

    def _next_sequence(self, account_id: str, year: int, period: int) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS seq FROM adjustments "
            "WHERE account_id=? AND year=? AND period=?",
            (account_id, year, period),
        ).fetchone()
        return row["seq"] + 1

    def _net_after_import(self, doc: BillDocument) -> int:
        """Net issued amount right after importing this adjustment."""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM documents "
            "WHERE account_id=? AND year=? AND period=?",
            (doc.account_id, doc.year, doc.period),
        ).fetchone()
        return row["total"]

    # ------------------------------------------------------------------ #
    # Pricing context
    # ------------------------------------------------------------------ #
    def _contract_id(self, explicit: str | None) -> str:
        if explicit is not None:
            return explicit
        rows = self.conn.execute("SELECT DISTINCT contract_id FROM contracts").fetchall()
        if not rows:
            raise DocumentError("no contract stored")
        if len(rows) > 1:
            raise DocumentError("multiple contracts stored; specify contract_id")
        return rows[0]["contract_id"]

    def _resolve_contract(
        self, account_id: str, year: int, contract_id: str | None, contract_revision: int | None
    ) -> Contract:
        if self._pricing_override is not None and contract_id is None and contract_revision is None:
            return self._pricing_override.contract

        cid = self._contract_id(contract_id)
        bills = self.bills_for_year(account_id, year)
        revisions = {b.contract_revision for b in bills}
        if contract_revision is None:
            if len(revisions) > 1:
                raise DocumentError(
                    f"bills for {account_id}/{year} span contract revisions {sorted(revisions)}; "
                    "specify contract_revision"
                )
            if revisions:
                contract_revision = revisions.pop()
            else:
                # No bills yet (e.g. a pure reassessment): use the newest stored revision.
                row = self.conn.execute(
                    "SELECT MAX(revision) AS rev FROM contracts WHERE contract_id=?", (cid,)
                ).fetchone()
                if row is None or row["rev"] is None:
                    raise DocumentError(f"unknown contract {cid}")
                contract_revision = row["rev"]
        if self._pricing_override is not None:
            c = self._pricing_override.contract
            if c.contract_id == cid and c.revision == contract_revision:
                return c
        return self.load_contract(cid, contract_revision)

    def pricing_for_year(
        self, account_id: str, year: int, *,
        contract_id: str | None = None, contract_revision: int | None = None,
    ) -> PricingService:
        return PricingService(
            self._resolve_contract(account_id, year, contract_id, contract_revision)
        )

    # ------------------------------------------------------------------ #
    # Recalculation + adjustment issuance
    # ------------------------------------------------------------------ #
    def recalculate_and_issue_adjustments(
        self,
        account_id: str,
        year: int,
        through_period: int,
        *,
        save_as: str | None = None,
        contract_id: str | None = None,
        contract_revision: int | None = None,
    ) -> RecalcResult:
        if not (1 <= through_period <= MAX_PERIOD):
            raise UsageError(f"through_period must be in 1..{MAX_PERIOD}")

        bills = {b.period: b for b in self.bills_for_year(account_id, year)}
        if not bills:
            raise DocumentError(f"no issued bills for {account_id}/{year}")

        contract = self._resolve_contract(account_id, year, contract_id, contract_revision)
        pricing = PricingService(contract)

        revisions = self.usage_for_year(account_id, year)
        statement = pricing.assess_year(account_id, year, revisions)
        if statement.complete_through < through_period:
            raise IncompleteChainError(
                f"cannot issue formal fees for {account_id}/{year} through period "
                f"{through_period}: chain complete only through "
                f"{statement.complete_through} (gaps: {statement.gap_periods})"
            )

        run_id = str(uuid.uuid4())
        issued: list[BillDocument] = []
        ledgers: list[PeriodLedger] = []
        confirmed = self.confirmed_usage_revisions(account_id, year)

        try:
            self.conn.execute(
                "INSERT INTO recalc_runs (run_id, account_id, year, through_period, save_as) "
                "VALUES (?,?,?,?,?)",
                (run_id, account_id, year, through_period, save_as),
            )
            self._issue_adjustments(
                run_id=run_id,
                save_as=save_as,
                account_id=account_id,
                year=year,
                through_period=through_period,
                contract=contract,
                statement=statement,
                bills=bills,
                confirmed=confirmed,
                issued=issued,
                ledgers=ledgers,
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        return RecalcResult(
            run_id=run_id,
            account_id=account_id,
            year=year,
            through_period=through_period,
            save_as=save_as,
            issued=issued,
            periods=ledgers,
            complete_through=statement.complete_through,
            gap_periods=statement.gap_periods,
        )

    def _issue_adjustments(
        self,
        *,
        run_id: str,
        save_as: str | None,
        account_id: str,
        year: int,
        through_period: int,
        contract: Contract,
        statement,
        bills: dict[int, BillDocument],
        confirmed: dict[int, int],
        issued: list[BillDocument],
        ledgers: list[PeriodLedger],
    ) -> None:
        for period in range(1, through_period + 1):
            target = statement.fee_at(period)
            bill = bills.get(period)
            adj_sum = self._adjustment_sum(account_id, year, period)

            if bill is None:
                ledgers.append(
                    PeriodLedger(
                        period=period,
                        original_amount_cents=None,
                        adjustment_cents=adj_sum,
                        net_issued_cents=None,
                        target_fee_cents=target,
                        delta_cents=None,
                        status="no_bill",
                    )
                )
                continue

            net = bill.amount_cents + adj_sum
            delta = target - net
            if delta == 0:
                # Unchanged period: no fresh adjustment, and net is already correct.
                ledgers.append(
                    PeriodLedger(
                        period=period,
                        original_amount_cents=bill.amount_cents,
                        adjustment_cents=adj_sum,
                        net_issued_cents=net,
                        target_fee_cents=target,
                        delta_cents=0,
                        status="unchanged",
                    )
                )
                continue

            # Adjustment states the current target and the period's usage
            # revisions as confirmed for this recalculation.
            usage_map = {
                p: confirmed[p] for p in sorted(confirmed) if p <= period
            }
            doc = self._make_adjustment(
                run_id=run_id,
                save_as=save_as,
                account_id=account_id,
                year=year,
                period=period,
                bill=bill,
                contract_revision=contract.revision,
                target=target,
                delta=delta,
                usage_revisions=usage_map,
                sequence=self._next_sequence(account_id, year, period),
            )
            self._insert_document(doc)
            self._insert_adjustment_meta(
                doc, account_id, year, period, bill, target, run_id
            )
            issued.append(doc)
            ledgers.append(
                PeriodLedger(
                    period=period,
                    original_amount_cents=bill.amount_cents,
                    adjustment_cents=adj_sum + delta,
                    net_issued_cents=net + delta,
                    target_fee_cents=target,
                    delta_cents=delta,
                    status="adjusted",
                )
            )

    def _make_adjustment(
        self,
        *,
        run_id: str,
        save_as: str | None,
        account_id: str,
        year: int,
        period: int,
        bill: BillDocument,
        contract_revision: int,
        target: int,
        delta: int,
        usage_revisions: dict[int, int],
        sequence: int,
    ) -> BillDocument:
        if save_as:
            document_id = f"{save_as}-P{period}-{sequence}"
        else:
            document_id = f"ADJ-{account_id}-{year}-P{period}-{run_id[:8]}-{sequence}"

        # Always state against the original bill; the amount is computed on net.
        previous_id = bill.document_id

        return BillDocument(
            document_id=document_id,
            document_type=ADJUSTMENT,
            account_id=account_id,
            year=year,
            period=period,
            amount_cents=delta,
            contract_revision=contract_revision,
            usage_revisions=usage_revisions,
            previous_statement_id=previous_id,
            source="recalculated",
            extra={
                "sequence": sequence,
                "target_amount_cents": target,
                "previous_net_cents": target - delta,
                "recalc_run_id": run_id,
            },
        )

    def _insert_adjustment_meta(
        self,
        doc: BillDocument,
        account_id: str,
        year: int,
        period: int,
        bill: BillDocument,
        target: int,
        run_id: str,
    ) -> None:
        sequence = doc.extra["sequence"]
        self.conn.execute(
            "INSERT INTO adjustments (document_id, account_id, year, period, sequence, "
            "previous_statement_id, target_amount_cents, recalc_run_id) VALUES (?,?,?,?,?,?,?,?)",
            (doc.document_id, account_id, year, period, sequence, bill.document_id, target, run_id),
        )

    # ------------------------------------------------------------------ #
    # Ledger view
    # ------------------------------------------------------------------ #
    def current_ledger(self, account_id: str, year: int) -> list[PeriodLedger]:
        """Per-period original, adjustments, net, and target from current usage."""
        bills = {b.period: b for b in self.bills_for_year(account_id, year)}
        revisions = self.usage_for_year(account_id, year)
        statement = None
        try:
            statement = self.pricing_for_year(account_id, year).assess_year(
                account_id, year, revisions
            )
        except DocumentError:
            statement = None

        ledgers: list[PeriodLedger] = []
        for period in range(1, MAX_PERIOD + 1):
            bill = bills.get(period)
            adj_sum = self._adjustment_sum(account_id, year, period)
            target = None
            if statement is not None:
                pos = statement.position(period)
                target = pos.fee_cents
            net = (bill.amount_cents + adj_sum) if bill is not None else None
            if bill is None:
                status = "no_bill"
            elif target is None:
                status = "unconfirmed"
            elif target == net:
                status = "unchanged"
            else:
                status = "adjusted"
            ledgers.append(
                PeriodLedger(
                    period=period,
                    original_amount_cents=bill.amount_cents if bill else None,
                    adjustment_cents=adj_sum,
                    net_issued_cents=net,
                    target_fee_cents=target,
                    delta_cents=(target - net)
                    if (target is not None and net is not None)
                    else None,
                    status=status,
                )
            )
        return ledgers
