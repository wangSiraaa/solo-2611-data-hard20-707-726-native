"""Delegation-side tests: chain connectivity, scope, timing, revisioning."""

from __future__ import annotations

import unittest

from tests._helpers import TempStackCase, grant, receipt


class Baseline756Tests(TempStackCase):
    def setUp(self):
        super().setUp()
        self.stack.seed_eight_hundred()

    def test_only_r1_valid_initially(self):
        result = self.stack.delegation.assess_all()
        by_id = {r["receipt_id"]: r for r in result["receipts"]}
        self.assertTrue(by_id["R1"]["valid"])
        self.assertFalse(by_id["R2"]["valid"])
        self.assertFalse(by_id["R3"]["valid"])
        self.assertEqual(result["valid_count"], 1)

    def test_r1_valid_despite_late_intake(self):
        r1 = self.stack.delegation.assess_receipt("R1")
        self.assertTrue(r1["valid"])
        self.assertEqual(r1["received_day"], 18)  # intake later than signing
        self.assertGreater(r1["received_day"], r1["signed_day"])

    def test_r2_boundary_revocation_invalid(self):
        r2 = self.stack.delegation.assess_receipt("R2")
        self.assertFalse(r2["valid"])
        self.assertEqual(r2["signed_day"], 12)
        self.assertIn("revoked_from 12", r2["reason"])

    def test_r3_region_out_of_scope(self):
        r3 = self.stack.delegation.assess_receipt("R3")
        self.assertFalse(r3["valid"])
        self.assertIn("region", r3["reason"])

    def test_per_level_revisions_reported(self):
        r1 = self.stack.delegation.assess_receipt("R1")
        self.assertEqual([lv["grant_id"] for lv in r1["levels"]], ["G1", "G2"])
        self.assertEqual([lv["revision"] for lv in r1["levels"]], [1, 1])
        self.assertTrue(all(lv["effective"] for lv in r1["levels"]))

    def test_revocation_correction_invalidates_r1_in_new_assessment(self):
        self.stack.delegation.ingest_grants({"grants": [
            grant("G1", 2, "P0", "A", start=0, end=20, can_redelegate=True,
                  revoked=9)
        ]})
        after = self.stack.delegation.assess_all()
        self.assertEqual(after["valid_count"], 0)
        r1 = self.stack.delegation.assess_receipt("R1")
        self.assertFalse(r1["valid"])
        self.assertEqual(r1["levels"][0]["revision"], 2)

    def test_historical_report_unchanged_and_not_current(self):
        rep1 = self.stack.delegation.publish(label="REPORT1")
        self.assertEqual(rep1["valid_count"], 1)
        self.stack.delegation.ingest_grants({"grants": [
            grant("G1", 2, "P0", "A", start=0, end=20, can_redelegate=True,
                  revoked=9)
        ]})
        self.stack.delegation.publish(label="REPORT2")
        reopened = self.stack.delegation.get_report("REPORT1")
        self.assertEqual(reopened["valid_count"], 1)
        verify = self.stack.delegation.verify_report("REPORT1")
        self.assertTrue(verify["intact"])
        self.assertFalse(verify["is_current_grants"])
        # And REPORT2 is current.
        self.assertTrue(self.stack.delegation.verify_report("REPORT2")["is_current_grants"])

    def test_receipt_chain_not_substituted(self):
        # R3 claims G1/G2 but is invalid; it must never be re-pointed to G1 alone.
        r3 = self.stack.delegation.assess_receipt("R3")
        self.assertEqual(r3["claimed_grant_ids"], ["G1", "G2"])
        self.assertFalse(r3["valid"])


class SelfSignedTests(TempStackCase):
    def setUp(self):
        super().setUp()
        self.stack.seed_eight_hundred()

    def test_self_signed_empty_chain_valid(self):
        self.stack.delegation.ingest_receipts({"receipts": [
            receipt("RS", signer="P0", claimed=(), signed_day=10)
        ]})
        r = self.stack.delegation.assess_receipt("RS")
        self.assertTrue(r["valid"])
        self.assertEqual(r["levels"], [])

    def test_non_consignee_without_chain_invalid(self):
        self.stack.delegation.ingest_receipts({"receipts": [
            receipt("RX", signer="Z", claimed=(), signed_day=10)
        ]})
        r = self.stack.delegation.assess_receipt("RX")
        self.assertFalse(r["valid"])

    def test_missing_signer_invalid(self):
        self.stack.delegation.ingest_receipts({"receipts": [
            receipt("RN", signer=None, claimed=(), signed_day=10)
        ]})
        self.assertFalse(self.stack.delegation.assess_receipt("RN")["valid"])

    def test_signer_must_be_final_agent(self):
        self.stack.delegation.ingest_receipts({"receipts": [
            receipt("RW", signer="C", claimed=("G1", "G2"), signed_day=10)
        ]})
        r = self.stack.delegation.assess_receipt("RW")
        self.assertFalse(r["valid"])
        self.assertIn("final claimed agent", r["reason"])

    def test_signer_must_be_first_level_agent_for_single_level(self):
        self.stack.delegation.ingest_receipts({"receipts": [
            receipt("RU", signer="A", claimed=("G1",), signed_day=10)
        ]})
        self.assertTrue(self.stack.delegation.assess_receipt("RU")["valid"])
        self.stack.delegation.ingest_receipts({"receipts": [
            receipt("RV", signer="B", claimed=("G1",), signed_day=10)
        ]})
        self.assertFalse(self.stack.delegation.assess_receipt("RV")["valid"])


class ChainStructureTests(TempStackCase):
    def test_child_must_be_issued_by_parent_agent(self):
        self.stack.delegation.ingest_grants({"grants": [
            grant("G1", 1, "P0", "A"),
        ]})
        with self.assertRaises(Exception):
            self.stack.delegation.ingest_grants({"grants": [
                grant("G2", 1, issuer="Q", agent="B", start=5, end=15, parent="G1"),
            ]})

    def test_parent_must_allow_redelegation(self):
        self.stack.delegation.ingest_grants({"grants": [
            grant("G1", 1, "P0", "A", can_redelegate=False),
        ]})
        with self.assertRaises(Exception):
            self.stack.delegation.ingest_grants({"grants": [
                grant("G2", 1, "A", "B", start=5, end=15, parent="G1"),
            ]})

    def test_child_nominal_must_not_exceed_parent(self):
        self.stack.delegation.ingest_grants({"grants": [grant("G1", 1, "P0", "A",
                                                               start=0, end=20)]})
        with self.assertRaises(Exception):
            self.stack.delegation.ingest_grants({"grants": [
                grant("G2", 1, "A", "B", start=-1, end=15, parent="G1"),
            ]})
        with self.assertRaises(Exception):
            self.stack.delegation.ingest_grants({"grants": [
                grant("G3", 1, "A", "B", start=5, end=25, parent="G1"),
            ]})

    def test_child_scope_sets_must_be_subset(self):
        self.stack.delegation.ingest_grants({"grants": [
            grant("G1", 1, "P0", "A", regions=("N",), document_types=("K",)),
        ]})
        with self.assertRaises(Exception):
            self.stack.delegation.ingest_grants({"grants": [
                grant("G2", 1, "A", "B", regions=("N", "S"), start=5, end=15,
                      parent="G1"),
            ]})
        with self.assertRaises(Exception):
            self.stack.delegation.ingest_grants({"grants": [
                grant("G3", 1, "A", "B", document_types=("K", "L"), start=5,
                      end=15, parent="G1"),
            ]})

    def test_third_layer_rejected(self):
        self.stack.delegation.ingest_grants({"grants": [
            grant("G1", 1, "P0", "A"),
            grant("G2", 1, "A", "B", start=5, end=15, parent="G1",
                  can_redelegate=True),
        ]})
        with self.assertRaises(Exception):
            self.stack.delegation.ingest_grants({"grants": [
                grant("G3", 1, "B", "C", start=6, end=14, parent="G2"),
            ]})

    def test_child_without_claimed_parent_is_broken_path(self):
        self.stack.delegation.ingest_grants({"grants": [
            grant("G1", 1, "P0", "A"),
            grant("G2", 1, "A", "B", start=5, end=15, parent="G1"),
        ]})
        self.stack.delegation.ingest_receipts({"receipts": [
            receipt("Rchild", signer="B", claimed=("G2",), signed_day=10)
        ]})
        r = self.stack.delegation.assess_receipt("Rchild")
        self.assertFalse(r["valid"])
        self.assertIn("path is incomplete", r["reason"])

    def test_root_issuer_must_be_consignee(self):
        self.stack.delegation.ingest_grants({"grants": [
            grant("G1", 1, "OTHER", "A"),
        ]})
        self.stack.delegation.ingest_receipts({"receipts": [
            receipt("Rorig", signer="A", claimed=("G1",), signed_day=10)
        ]})
        r = self.stack.delegation.assess_receipt("Rorig")
        self.assertFalse(r["valid"])
        self.assertIn("root issuer", r["reason"])


class ParentRevocationTests(TempStackCase):
    def test_parent_revocation_truncates_child_path(self):
        self.stack.seed_eight_hundred()
        # Revoke only the parent; child stays nominally fine.
        self.stack.delegation.ingest_grants({"grants": [
            grant("G1", 2, "P0", "A", start=0, end=20, can_redelegate=True,
                  revoked=9)
        ]})
        r1 = self.stack.delegation.assess_receipt("R1")
        self.assertFalse(r1["valid"])
        self.assertEqual(r1["broken_at_level"], 0)
        # Level 1 is reported as not checked, never silently repaired.
        self.assertEqual(r1["levels"][1]["kind"], "not_checked")

    def test_revocation_day_equals_signing_is_invalid(self):
        self.stack.delegation.ingest_grants({"grants": [
            grant("G1", 1, "P0", "A", revoked=10),
        ]})
        self.stack.delegation.ingest_receipts({"receipts": [
            receipt("Rd", signer="A", claimed=("G1",), signed_day=10)
        ]})
        self.assertFalse(self.stack.delegation.assess_receipt("Rd")["valid"])

    def test_day_before_revocation_valid(self):
        self.stack.delegation.ingest_grants({"grants": [
            grant("G1", 1, "P0", "A", revoked=10),
        ]})
        self.stack.delegation.ingest_receipts({"receipts": [
            receipt("Rb", signer="A", claimed=("G1",), signed_day=9)
        ]})
        self.assertTrue(self.stack.delegation.assess_receipt("Rb")["valid"])

    def test_nominal_window_bounds(self):
        self.stack.delegation.ingest_grants({"grants": [
            grant("G1", 1, "P0", "A", start=5, end=15),
        ]})
        self.stack.delegation.ingest_receipts({"receipts": [
            receipt("Rstart", signer="A", claimed=("G1",), signed_day=5),
            receipt("Rend", signer="A", claimed=("G1",), signed_day=15),
        ]})
        self.assertTrue(self.stack.delegation.assess_receipt("Rstart")["valid"])
        self.assertFalse(self.stack.delegation.assess_receipt("Rend")["valid"])


if __name__ == "__main__":
    unittest.main()
