"""Integration tests spanning both services, reopens, and batch atomicity."""

from __future__ import annotations

import json
import unittest

from delegation.errors import ValidationError as DelegValidationError
from delivery.errors import ConflictError as DelConflictError
from delivery.errors import ValidationError as DelValidationError
from tests._helpers import TempStackCase, grant, receipt, supplement


class ReopenTests(TempStackCase):
    """State must survive closing and reopening the databases (new processes)."""

    def test_reports_and_confirmations_survive_reopen(self):
        from delegation import DelegationService
        from delivery import DeliveryService
        from tools.authority_adapter import DelegationAuthority

        self.stack.seed_eight_hundred()
        self.stack.delivery.ingest_shipments({"shipments": [{
            "shipment_id": "S1", "consignee": "P0", "region": "N",
            "document_type": "K", "required_parts": ["BX1", "BX2"],
        }]})
        self.stack.delivery.ingest_supplements({"receipts": [
            supplement("R1", parts=("BX1", "BX2"))
        ]})
        rep1 = self.stack.delegation.publish(label="REPORT1")
        prep = self.stack.delivery.prepare("S1")
        del1 = self.stack.delivery.confirm("S1", expected_evidence_hash=prep["evidence_hash"])
        self.stack.close()

        deleg2 = DelegationService(self.tmpdir / "delegation.db")
        auth2 = DelegationAuthority(str(self.tmpdir / "delegation.db"))
        depot2 = DeliveryService(self.tmpdir / "delivery.db", auth2)
        try:
            report = deleg2.get_report("REPORT1")
            self.assertEqual(report["valid_count"], rep1["valid_count"])
            self.assertTrue(deleg2.verify_report("REPORT1")["intact"])
            self.assertEqual(depot2.get_confirmation("DEL1"), del1)
            self.assertEqual(depot2.confirmation_status("S1")["status"], "CONFIRMED")
            # Coverage still recomputed identically after reopen.
            self.assertTrue(depot2.coverage("S1")["complete"])
        finally:
            depot2.close()
            auth2.close()
            deleg2.close()
            # Re-open the original stack handle so tearDown can close it.
            self.stack = type(self.stack)(self.tmpdir)
            self.stack.close()


class AdapterIndependenceTests(TempStackCase):
    def test_unknown_original_is_not_matched(self):
        self.stack.seed_eight_hundred()
        verdict = self.stack.authority.assess({
            "receipt_id": "NOPE", "consignee": "P0", "signer": "B",
            "signed_day": 10, "received_day": 18, "region": "N",
            "document_type": "K", "claimed_grant_ids": ["G1", "G2"],
        })
        self.assertFalse(verdict["matched"])
        self.assertFalse(verdict["valid"])

    def test_supplement_content_mismatch_is_not_matched(self):
        self.stack.seed_eight_hundred()
        verdict = self.stack.authority.assess({
            "receipt_id": "R1", "consignee": "P0", "signer": "B",
            "signed_day": 11, "received_day": 18, "region": "N",
            "document_type": "K", "claimed_grant_ids": ["G1", "G2"],
        })
        self.assertFalse(verdict["matched"])

    def test_authority_fingerprint_changes_with_revision(self):
        self.stack.seed_eight_hundred()
        core = {
            "receipt_id": "R4", "consignee": "P0", "signer": "P0",
            "signed_day": 10, "received_day": 19, "region": "N",
            "document_type": "K", "claimed_grant_ids": [],
        }
        self.stack.delegation.ingest_receipts({"receipts": [
            receipt("R4", signer="P0", claimed=(), signed_day=10, received_day=19)
        ]})
        before = self.stack.authority.assess(core)["authority_fingerprint"]
        self.stack.delegation.ingest_grants({"grants": [
            grant("G1", 2, "P0", "A", start=0, end=20, can_redelegate=True, revoked=9)
        ]})
        after_chain = dict(core, claimed_grant_ids=["G1"])
        fp_before = self.stack.authority.assess(
            dict(core, claimed_grant_ids=["G1"]))["authority_fingerprint"]
        # Different chain set => different fingerprint even before revision.
        self.assertNotEqual(before, fp_before)


class BatchAtomicityScaleTests(TempStackCase):
    def test_bad_shipment_rejects_whole_batch(self):
        with self.assertRaises(DelValidationError):
            self.stack.delivery.ingest_shipments({"shipments": [
                {"shipment_id": "S1", "consignee": "P0", "region": "N",
                 "document_type": "K", "required_parts": ["A"]},
                {"shipment_id": "S2", "consignee": "P0", "region": "N",
                 "document_type": "K", "required_parts": []},
            ]})
        self.assertEqual(self.stack.delivery.list_shipments(), [])

    def test_bad_supplement_rejects_whole_batch(self):
        self.stack.seed_eight_hundred()
        self.stack.delivery.ingest_shipments({"shipments": [{
            "shipment_id": "S1", "consignee": "P0", "region": "N",
            "document_type": "K", "required_parts": ["BX1", "BX2"],
        }]})
        with self.assertRaises(DelValidationError):
            self.stack.delivery.ingest_supplements({"receipts": [
                supplement("R1", parts=("BX1",)),
                supplement("R2", parts=("NOPE",)),  # unknown part
            ]})
        self.assertEqual(self.stack.delivery.list_delivery_receipts(), [])

    def test_supplement_same_id_diff_content_in_batch_rejected(self):
        self.stack.seed_eight_hundred()
        self.stack.delivery.ingest_shipments({"shipments": [{
            "shipment_id": "S1", "consignee": "P0", "region": "N",
            "document_type": "K", "required_parts": ["BX1", "BX2"],
        }]})
        with self.assertRaises(DelConflictError):
            self.stack.delivery.ingest_supplements({"receipts": [
                supplement("R1", parts=("BX1",)),
                supplement("R1", parts=("BX2",)),
            ]})


class ParentNominalCorrectionTests(TempStackCase):
    def test_child_validity_follows_parent_nominal_correction(self):
        # Parent G1 originally [0,20); child G2 [5,15).  A receipt at day 8.
        self.stack.delegation.ingest_grants({"grants": [
            grant("G1", 1, "P0", "A", start=0, end=20, can_redelegate=True),
            grant("G2", 1, "A", "B", start=5, end=15, parent="G1"),
        ]})
        self.stack.delegation.ingest_receipts({"receipts": [
            receipt("R8", signer="B", signed_day=8, claimed=("G1", "G2"))
        ]})
        self.assertTrue(self.stack.delegation.assess_receipt("R8")["valid"])
        # Correct the parent nominal window to [10,20): day 8 no longer effective.
        self.stack.delegation.ingest_grants({"grants": [
            grant("G1", 2, "P0", "A", start=10, end=20, can_redelegate=True),
        ]})
        r8 = self.stack.delegation.assess_receipt("R8")
        self.assertFalse(r8["valid"])
        self.assertEqual(r8["levels"][0]["revision"], 2)

    def test_historical_report_unaffected_by_parent_correction(self):
        self.stack.delegation.ingest_grants({"grants": [
            grant("G1", 1, "P0", "A", start=0, end=20, can_redelegate=True),
            grant("G2", 1, "A", "B", start=5, end=15, parent="G1"),
        ]})
        self.stack.delegation.ingest_receipts({"receipts": [
            receipt("R8", signer="B", signed_day=8, claimed=("G1", "G2"))
        ]})
        rep = self.stack.delegation.publish(label="REPORT1")
        self.assertEqual(rep["valid_count"], 1)
        self.stack.delegation.ingest_grants({"grants": [
            grant("G1", 2, "P0", "A", start=10, end=20, can_redelegate=True),
        ]})
        self.stack.delegation.publish(label="REPORT2")
        reopened = self.stack.delegation.get_report("REPORT1")
        self.assertEqual(reopened["valid_count"], 1)
        self.assertEqual(reopened["receipts"][0]["levels"][0]["revision"], 1)


if __name__ == "__main__":
    unittest.main()
