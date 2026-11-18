"""Tests for the cumulative-usage pricing service and bill top-up repository."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from cumulative_billing import (
    ADJUSTMENT,
    BILL,
    BillRepository,
    Contract,
    ContractError,
    IncompleteChainError,
    PricingService,
    UsageError,
    UsageRevision,
)
from cumulative_billing.documents import BillDocument

ROOT = Path(__file__).resolve().parent.parent
MATERIALS = ROOT / "materials" / "754"

CONTRACT = {
    "contract_id": "K1",
    "revision": 1,
    "bands": [
        {"lower": 0, "upper": 100, "unit_cents": 2},
        {"lower": 100, "upper": None, "unit_cents": 1},
    ],
}


def contract() -> Contract:
    return Contract.from_dict(CONTRACT)


def usage(period: int, quantity: int, revision: int = 1, account="AC1", year=2025):
    return UsageRevision(account, year, period, revision, quantity)


class ContractTests(unittest.TestCase):
    def test_marginal_cost(self):
        c = contract()
        # x<=100 -> 2x; beyond -> 200 + (x-100).
        self.assertEqual(c.cost(0), 0)
        self.assertEqual(c.cost(60), 120)
        self.assertEqual(c.cost(80), 160)
        self.assertEqual(c.cost(100), 200)
        self.assertEqual(c.cost(110), 210)
        self.assertEqual(c.cost(130), 230)
        self.assertEqual(c.cost(150), 250)

    def test_period_fee_is_difference_not_single_band(self):
        c = contract()
        # 80 -> 130 spans the 100 boundary: 20 at 2 + 30 at 1 = 70.
        self.assertEqual(c.period_fee(80, 130), 70)
        # Crossing down: 60 -> 110 = 80 at 2 + 10 at 1 = 90.
        self.assertEqual(c.period_fee(60, 110), 90)

    def test_breakdown_sums_to_fee(self):
        c = contract()
        parts = c.marginal_breakdown(80, 130)
        self.assertEqual(sum(p["cents"] for p in parts), c.period_fee(80, 130))
        self.assertEqual(sum(p["units"] for p in parts), 50)

    def test_contract_validation(self):
        bad_gap = {
            "contract_id": "X",
            "revision": 1,
            "bands": [
                {"lower": 0, "upper": 10, "unit_cents": 1},
                {"lower": 20, "upper": None, "unit_cents": 1},
            ],
        }
        with self.assertRaises(ContractError):
            Contract.from_dict(bad_gap)

        no_unbounded_last = {
            "contract_id": "X",
            "revision": 1,
            "bands": [{"lower": 0, "upper": 10, "unit_cents": 1}],
        }
        with self.assertRaises(ContractError):
            Contract.from_dict(no_unbounded_last)

        not_from_zero = {
            "contract_id": "X",
            "revision": 1,
            "bands": [{"lower": 1, "upper": None, "unit_cents": 1}],
        }
        with self.assertRaises(ContractError):
            Contract.from_dict(not_from_zero)

    def test_three_band_contract(self):
        c = Contract.from_dict(
            {
                "contract_id": "K3",
                "revision": 1,
                "bands": [
                    {"lower": 0, "upper": 10, "unit_cents": 5},
                    {"lower": 10, "upper": 30, "unit_cents": 3},
                    {"lower": 30, "upper": None, "unit_cents": 1},
                ],
            }
        )
        self.assertEqual(c.cost(10), 50)
        self.assertEqual(c.cost(30), 50 + 60)
        self.assertEqual(c.cost(40), 50 + 60 + 10)
        self.assertEqual(c.period_fee(5, 35), (50 - 25) + (60) + 5)


class PricingTests(unittest.TestCase):
    def setUp(self):
        self.pricing = PricingService(contract())

    def test_initial_statement(self):
        st = self.pricing.assess_year(
            "AC1", 2025, [usage(1, 80), usage(2, 50), usage(3, 20)]
        )
        self.assertEqual(
            [(p.cumulative_start, p.cumulative_end, p.fee_cents) for p in st.periods[:3]],
            [(0, 80, 160), (80, 130, 70), (130, 150, 20)],
        )
        self.assertEqual(st.complete_through, 3)

    def test_revision_shifts_later_bands(self):
        st = self.pricing.assess_year(
            "AC1", 2025, [usage(1, 60, revision=2), usage(1, 80, 1), usage(2, 50), usage(3, 20)]
        )
        self.assertEqual(
            [(p.cumulative_start, p.cumulative_end, p.fee_cents) for p in st.periods[:3]],
            [(0, 60, 120), (60, 110, 90), (110, 130, 20)],
        )

    def test_missing_period_is_not_zero(self):
        st = self.pricing.assess_year("AC1", 2025, [usage(1, 80), usage(3, 20)])
        self.assertFalse(st.position(2).confirmed)
        self.assertIsNone(st.position(3).cumulative_start)  # undefined past gap
        self.assertIsNone(st.position(3).fee_cents)
        self.assertEqual(st.complete_through, 1)
        self.assertEqual(st.gap_periods, [2])
        with self.assertRaises(IncompleteChainError):
            st.fee_at(3)

    def test_trailing_absence_is_not_a_gap(self):
        st = self.pricing.assess_year("AC1", 2025, [usage(1, 80)])
        self.assertEqual(st.gap_periods, [])
        self.assertEqual(st.complete_through, 1)

    def test_period_revision_pin(self):
        revs = [usage(1, 80, 1), usage(1, 60, 2)]
        st = self.pricing.assess_year("AC1", 2025, revs, period_revision={1: 1})
        self.assertEqual(st.position(1).quantity, 80)
        st2 = self.pricing.assess_year("AC1", 2025, revs)
        self.assertEqual(st2.position(1).quantity, 60)

    def test_duplicate_revision_rejected(self):
        with self.assertRaises(UsageError):
            self.pricing.assess_year("AC1", 2025, [usage(1, 10, 1), usage(1, 10, 1)])

    def test_negative_usage_rejected(self):
        with self.assertRaises(UsageError):
            usage(1, -1).validate()


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "b.db")
        self.repo = BillRepository(self.db)
        self.repo.store_contract(contract())
        for u in (usage(1, 80), usage(2, 50), usage(3, 20)):
            self.repo.add_usage_revision(u)
        self.repo.import_documents(str(MATERIALS / "issued_bills.jsonl"))

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _issued(self):
        return self.repo.documents("AC1", 2025)

    def test_import_preserves_original_bills(self):
        docs = {d.document_id: d for d in self._issued()}
        self.assertEqual(set(docs), {"B1", "B2", "B3"})
        self.assertEqual([docs[k].amount_cents for k in ("B1", "B2", "B3")], [160, 70, 20])
        self.assertEqual({docs[k].document_type for k in docs}, {BILL})

    def test_recalc_without_change_issues_nothing(self):
        result = self.repo.recalculate_and_issue_adjustments("AC1", 2025, 3)
        self.assertEqual(result.issued, [])
        self.assertEqual(result.net_adjustment_cents, 0)

    def test_first_correction_propagates(self):
        self.repo.add_usage_revision(usage(1, 60, revision=2))
        result = self.repo.recalculate_and_issue_adjustments("AC1", 2025, 3, save_as="ADJ1")
        by_period = {d.period: d.amount_cents for d in result.issued}
        self.assertEqual(by_period, {1: -40, 2: 20})
        self.assertEqual(result.net_adjustment_cents, -20)
        # Every adjustment is signed and names the original bill.
        for d in result.issued:
            self.assertEqual(d.document_type, ADJUSTMENT)
            self.assertIn(d.previous_statement_id, {"B1", "B2", "B3"})
        self.assertEqual(self.repo.net_issued_amount("AC1", 2025, 1), 120)
        self.assertEqual(self.repo.net_issued_amount("AC1", 2025, 2), 90)
        self.assertEqual(self.repo.net_issued_amount("AC1", 2025, 3), 20)

    def test_duplicate_correction_is_idempotent(self):
        self.repo.add_usage_revision(usage(1, 60, revision=2))
        self.repo.recalculate_and_issue_adjustments("AC1", 2025, 3, save_as="ADJ1")
        second = self.repo.recalculate_and_issue_adjustments("AC1", 2025, 3)
        self.assertEqual(second.issued, [])
        self.assertEqual(second.net_adjustment_cents, 0)
        adj = self.repo.adjustments_for_year("AC1", 2025)
        self.assertEqual(sorted((d.period, d.amount_cents) for d in adj), [(1, -40), (2, 20)])

    def test_further_correction_uses_net_not_original(self):
        self.repo.add_usage_revision(usage(1, 60, revision=2))
        self.repo.recalculate_and_issue_adjustments("AC1", 2025, 3, save_as="ADJ1")
        # Move period 1 back to 70: cumulative 70,120,140 -> fees 140/80/20.
        self.repo.add_usage_revision(usage(1, 70, revision=3))
        result = self.repo.recalculate_and_issue_adjustments("AC1", 2025, 3, save_as="ADJ2")
        by_period = {d.period: d.amount_cents for d in result.issued}
        # Period 1 net was 120 -> target 140 => +20 (not target - original 160).
        # Period 2 net was 90 -> target 80 => -10.
        self.assertEqual(by_period, {1: 20, 2: -10})
        self.assertEqual(self.repo.net_issued_amount("AC1", 2025, 1), 140)
        self.assertEqual(self.repo.net_issued_amount("AC1", 2025, 2), 80)

    def test_reverting_correction_nets_to_zero(self):
        self.repo.add_usage_revision(usage(1, 60, revision=2))
        self.repo.recalculate_and_issue_adjustments("AC1", 2025, 3, save_as="ADJ1")
        # Revise period 1 exactly back to the original quantity.
        self.repo.add_usage_revision(usage(1, 80, revision=3))
        result = self.repo.recalculate_and_issue_adjustments("AC1", 2025, 3, save_as="ADJ2")
        by_period = {d.period: d.amount_cents for d in result.issued}
        self.assertEqual(by_period, {1: 40, 2: -20})
        self.assertEqual(self.repo.net_issued_amount("AC1", 2025, 1), 160)
        self.assertEqual(self.repo.net_issued_amount("AC1", 2025, 2), 70)

    def test_unchanged_period_gets_no_delta_but_later_does(self):
        # Revise period 2 only: P1 unchanged, P2 and P3 propagate.
        self.repo.add_usage_revision(usage(2, 60, revision=2))
        result = self.repo.recalculate_and_issue_adjustments("AC1", 2025, 3, save_as="X")
        by_period = {d.period: d.amount_cents for d in result.issued}
        self.assertNotIn(1, by_period)  # untouched, no invented delta
        # Cumulative: 80, 140, 160 -> fees 160, 80 (80->140), 20 (140->160).
        self.assertEqual(by_period, {2: 10})

    def test_gap_blocks_formal_fees(self):
        # Fresh account with a hole at period 2.
        repo = BillRepository(os.path.join(self.tmp.name, "gap.db"))
        repo.store_contract(contract())
        repo.add_usage_revision(usage(1, 80))
        repo.add_usage_revision(usage(3, 20))
        repo.import_document_dicts(
            [
                {
                    "format": "period-bill/1",
                    "document_id": "G1",
                    "document_type": "BILL",
                    "account_id": "AC1",
                    "year": 2025,
                    "period": 1,
                    "amount_cents": 160,
                    "contract_revision": 1,
                    "usage_revisions": {"1": 1},
                    "previous_statement_id": None,
                }
            ]
        )
        with self.assertRaises(IncompleteChainError):
            repo.recalculate_and_issue_adjustments("AC1", 2025, 3)
        repo.close()

    def test_explicit_zero_usage_confirms(self):
        repo = BillRepository(os.path.join(self.tmp.name, "zero.db"))
        repo.store_contract(contract())
        repo.add_usage_revision(usage(1, 80))
        repo.add_usage_revision(usage(2, 0))  # explicit zero: confirmed
        repo.add_usage_revision(usage(3, 20))
        st = repo.pricing_for_year("AC1", 2025).assess_year(
            "AC1", 2025, repo.usage_for_year("AC1", 2025)
        )
        self.assertEqual(st.gap_periods, [])
        self.assertEqual(st.position(2).fee_cents, 0)
        self.assertEqual(st.position(3).cumulative_start, 80)
        repo.close()

    def test_usage_revision_conflict(self):
        with self.assertRaises(UsageError):
            self.repo.add_usage_revision(usage(1, 99, revision=1))

    def test_client_reads_export(self):
        self.repo.add_usage_revision(usage(1, 60, revision=2))
        self.repo.recalculate_and_issue_adjustments("AC1", 2025, 3, save_as="ADJ1")
        out = os.path.join(self.tmp.name, "out.jsonl")
        self.repo.export_jsonl(out, account_id="AC1", year=2025)
        proc = subprocess.run(
            [sys.executable, str(MATERIALS / "bill_reader.py"), out],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(proc.stdout.strip().splitlines()[-1], "NET 230")
        self.assertIn("ADJ1-P1-1 ADJUSTMENT 1 -40 B1", proc.stdout)
        self.assertIn("ADJ1-P2-1 ADJUSTMENT 2 20 B2", proc.stdout)

    def test_client_rejects_duplicate_ids(self):
        # Reading the same export twice must trip the client's duplicate check.
        self.repo.add_usage_revision(usage(1, 60, revision=2))
        self.repo.recalculate_and_issue_adjustments("AC1", 2025, 3, save_as="ADJ1")
        out = os.path.join(self.tmp.name, "out.jsonl")
        self.repo.export_jsonl(out, account_id="AC1", year=2025)
        proc = subprocess.run(
            [sys.executable, str(MATERIALS / "bill_reader.py"), out, out],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("duplicate", proc.stderr.lower())

    def test_original_raw_text_preserved(self):
        out = os.path.join(self.tmp.name, "out.jsonl")
        self.repo.export_jsonl(out, account_id="AC1", year=2025)
        original = (MATERIALS / "issued_bills.jsonl").read_text().strip().splitlines()
        exported = Path(out).read_text().strip().splitlines()
        self.assertEqual(exported, original)

    def test_imported_adjustment_counts_toward_net(self):
        # An adjustment arriving as an imported document must be part of net.
        self.repo.import_document_dicts(
            [
                {
                    "format": "period-bill/1",
                    "document_id": "IMP-A1",
                    "document_type": "ADJUSTMENT",
                    "account_id": "AC1",
                    "year": 2025,
                    "period": 1,
                    "amount_cents": -10,
                    "contract_revision": 1,
                    "usage_revisions": {"1": 1},
                    "previous_statement_id": "B1",
                }
            ]
        )
        self.assertEqual(self.repo.net_issued_amount("AC1", 2025, 1), 150)
        # Recalc against current usage (period 1 = 80 -> target 160) must add +10.
        result = self.repo.recalculate_and_issue_adjustments("AC1", 2025, 3, save_as="R")
        self.assertEqual({d.period: d.amount_cents for d in result.issued}, {1: 10})
        self.assertEqual(self.repo.net_issued_amount("AC1", 2025, 1), 160)
        # And it is now stable.
        self.assertEqual(
            self.repo.recalculate_and_issue_adjustments("AC1", 2025, 3).issued, []
        )

    def test_original_rows_untouched_by_recalc(self):
        before = self.repo.documents("AC1", 2025)
        self.repo.add_usage_revision(usage(1, 60, revision=2))
        self.repo.recalculate_and_issue_adjustments("AC1", 2025, 3, save_as="ADJ1")
        after = {d.document_id: d for d in self.repo.documents("AC1", 2025)}
        for doc in before:
            self.assertEqual(after[doc.document_id], doc)

    def test_three_band_cross_period(self):
        repo = BillRepository(os.path.join(self.tmp.name, "k3.db"))
        c3 = Contract.from_dict(
            {
                "contract_id": "K3",
                "revision": 1,
                "bands": [
                    {"lower": 0, "upper": 10, "unit_cents": 5},
                    {"lower": 10, "upper": 30, "unit_cents": 3},
                    {"lower": 30, "upper": None, "unit_cents": 1},
                ],
            }
        )
        repo.store_contract(c3)
        for p, q in ((1, 10), (2, 20), (3, 10)):
            repo.add_usage_revision(usage(p, q))
        # Cumulative 10,30,40 with bands 5/3/1 -> cost 50,110,120 -> fees 50,60,10.
        bills = []
        for p, amt in ((1, 50), (2, 60), (3, 10)):
            bills.append(
                {
                    "format": "period-bill/1",
                    "document_id": f"K{p}",
                    "document_type": "BILL",
                    "account_id": "AC1",
                    "year": 2025,
                    "period": p,
                    "amount_cents": amt,
                    "contract_revision": 1,
                    "usage_revisions": {str(i): 1 for i in range(1, p + 1)},
                    "previous_statement_id": None,
                }
            )
        repo.import_document_dicts(bills)
        # No change => no adjustments.
        self.assertEqual(repo.recalculate_and_issue_adjustments("AC1", 2025, 3).issued, [])
        # Lower period 1 to 5: cumulative 5,25,35 -> cost 25,95,115 -> fees 25,70,20.
        repo.add_usage_revision(usage(1, 5, revision=2))
        res = repo.recalculate_and_issue_adjustments("AC1", 2025, 3, save_as="KA")
        by_period = {d.period: d.amount_cents for d in res.issued}
        self.assertEqual(by_period, {1: -25, 2: 10, 3: 10})
        repo.close()


class DocumentTests(unittest.TestCase):
    def test_adjustment_must_reference_bill(self):
        base = {
            "format": "period-bill/1",
            "document_id": "A1",
            "document_type": "ADJUSTMENT",
            "account_id": "AC1",
            "year": 2025,
            "period": 1,
            "amount_cents": -40,
            "contract_revision": 1,
            "usage_revisions": {"1": 2},
            "previous_statement_id": "B1",
        }
        doc = BillDocument.from_dict(base)
        self.assertEqual(doc.amount_cents, -40)

        bad = dict(base)
        bad["previous_statement_id"] = None
        from cumulative_billing import DocumentError

        with self.assertRaises(DocumentError):
            BillDocument.from_dict(bad)

    def test_negative_bill_rejected(self):
        from cumulative_billing import DocumentError

        with self.assertRaises(DocumentError):
            BillDocument.from_dict(
                {
                    "format": "period-bill/1",
                    "document_id": "B9",
                    "document_type": "BILL",
                    "account_id": "AC1",
                    "year": 2025,
                    "period": 1,
                    "amount_cents": -1,
                    "contract_revision": 1,
                    "usage_revisions": {"1": 1},
                    "previous_statement_id": None,
                }
            )


if __name__ == "__main__":
    unittest.main()
