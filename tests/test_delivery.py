"""Delivery-side tests: coverage, confirmation, review lifecycle, staleness."""

from __future__ import annotations

import unittest

from delivery.errors import (
    ConflictError,
    StalePreparationError,
    StateError,
    ValidationError,
)
from tests._helpers import TempStackCase, receipt, supplement


class DeliveryBase(TempStackCase):
    def setUp(self):
        super().setUp()
        self.stack.seed_eight_hundred()
        self.depot = self.stack.delivery
        self.depot.ingest_shipments({"shipments": [{
            "shipment_id": "S1", "consignee": "P0", "region": "N",
            "document_type": "K", "required_parts": ["BX1", "BX2"],
        }]})
        self.depot.ingest_supplements({"receipts": [
            supplement("R1", parts=("BX1", "BX2")),
            supplement("R2", signed_day=12, parts=("BX2",)),
        ]})

    def revoke_g1_from_9(self):
        from tests._helpers import grant
        self.stack.delegation.ingest_grants({"grants": [
            grant("G1", 2, "P0", "A", start=0, end=20, can_redelegate=True,
                  revoked=9)
        ]})


class InitialCoverageTests(DeliveryBase):
    def test_r1_covers_both_parts(self):
        cov = self.depot.coverage("S1")
        self.assertTrue(cov["complete"])
        self.assertEqual(cov["covered_parts"], ["BX1", "BX2"])
        self.assertEqual(cov["covered_by"]["BX1"], ["R1"])

    def test_r2_boundary_does_not_contribute(self):
        cov = self.depot.coverage("S1")
        by_id = {e["receipt_id"]: e for e in cov["evidence"]}
        self.assertTrue(by_id["R1"]["counts"])
        self.assertFalse(by_id["R2"]["counts"])
        self.assertEqual(cov["covered_by"]["BX2"], ["R1"])

    def test_duplicate_import_does_not_add_quantity(self):
        self.depot.ingest_supplements({"receipts": [supplement("R1", parts=("BX1", "BX2"))]})
        cov = self.depot.coverage("S1")
        self.assertEqual(cov["covered_by"]["BX1"], ["R1"])
        self.assertEqual(cov["covered_by"]["BX2"], ["R1"])

    def test_confirm_del1(self):
        prep = self.depot.prepare("S1")
        del1 = self.depot.confirm("S1", expected_evidence_hash=prep["evidence_hash"])
        self.assertEqual(del1["confirmation_id"], "DEL1")
        self.assertEqual(del1["parts"], ["BX1", "BX2"])
        status = self.depot.confirmation_status("S1")
        self.assertEqual(status["status"], "CONFIRMED")


class AuthorityCorrectionTests(DeliveryBase):
    def _confirm_del1(self):
        prep = self.depot.prepare("S1")
        return self.depot.confirm("S1", expected_evidence_hash=prep["evidence_hash"])

    def test_revocation_moves_to_needs_review_keeping_del1(self):
        del1 = self._confirm_del1()
        self.revoke_g1_from_9()
        rec = self.depot.reconcile(["S1"])
        self.assertEqual(rec["flagged_count"], 1)
        status = self.depot.confirmation_status("S1")
        self.assertEqual(status["status"], "NEEDS_REVIEW")
        # DEL1 identity retained unchanged.
        self.assertEqual(self.depot.get_confirmation("DEL1"), del1)
        self.assertEqual(status["confirmation"]["confirmation_id"], "DEL1")
        self.assertFalse(status["confirmed_complete"])
        self.assertFalse(status["current_complete"])

    def test_needs_review_hides_not_rewrites_confirmation(self):
        del1 = self._confirm_del1()
        self.revoke_g1_from_9()
        self.depot.reconcile(["S1"])
        history = self.depot.confirmations_for("S1")
        self.assertEqual([c["confirmation_id"] for c in history], ["DEL1"])
        self.assertEqual(history[0], del1)

    def test_new_evidence_del2_links_previous(self):
        del1 = self._confirm_del1()
        self.revoke_g1_from_9()
        self.depot.reconcile(["S1"])
        # New self-signed original R4 (both parts) on both sides.
        self.stack.delegation.ingest_receipts({"receipts": [
            receipt("R4", signer="P0", claimed=(), signed_day=10,
                    received_day=19)
        ]})
        self.depot.ingest_supplements({"receipts": [
            supplement("R4", signer="P0", signed_day=10, received_day=19,
                       claimed=(), parts=("BX1", "BX2"))
        ]})
        cov = self.depot.coverage("S1")
        self.assertTrue(cov["complete"])
        prep = self.depot.prepare("S1")
        del2 = self.depot.confirm("S1", expected_evidence_hash=prep["evidence_hash"])
        self.assertEqual(del2["confirmation_id"], "DEL2")
        self.assertEqual(del2["previous_confirmation_id"], "DEL1")
        self.assertEqual(self.depot.confirmation_status("S1")["status"], "CONFIRMED")
        # Historical DEL1 still readable and unchanged.
        self.assertEqual(self.depot.get_confirmation("DEL1"), del1)

    def test_reconcile_does_not_flag_when_still_complete(self):
        self._confirm_del1()
        rec = self.depot.reconcile(["S1"])
        self.assertEqual(rec["flagged_count"], 0)


class BindingIntegrityTests(DeliveryBase):
    def test_parts_must_belong_to_shipment(self):
        with self.assertRaises(ValidationError):
            self.depot.ingest_supplements({"receipts": [
                supplement("RX", parts=("BX9",))
            ]})

    def test_region_mismatch_does_not_cover(self):
        # A supplement with a mismatched region/type is accepted as a binding
        # but never covers a part.
        self.depot.ingest_supplements({"receipts": [
            supplement("RX", region="N", document_type="L")
        ]})
        cov = self.depot.coverage("S1")
        by_id = {e["receipt_id"]: e for e in cov["evidence"]}
        self.assertFalse(by_id["RX"]["context_ok"])
        self.assertFalse(by_id["RX"]["counts"])

    def test_receipt_cannot_be_moved_to_other_shipment(self):
        self.depot.ingest_shipments({"shipments": [{
            "shipment_id": "S2", "consignee": "P0", "region": "N",
            "document_type": "K", "required_parts": ["BX1"],
        }]})
        with self.assertRaises(ConflictError):
            self.depot.ingest_supplements({"receipts": [
                supplement("R1", shipment_id="S2", parts=("BX1",))
            ]})

    def test_parts_cannot_change_for_same_receipt(self):
        with self.assertRaises(ConflictError):
            self.depot.ingest_supplements({"receipts": [
                supplement("R1", parts=("BX1",))
            ]})

    def test_supplement_content_must_match_original(self):
        # Same receipt id, but different signed_day => no delegation original match.
        self.depot.ingest_supplements({"receipts": [
            supplement("Rz", signed_day=3, parts=("BX1",))
        ]})
        cov = self.depot.coverage("S1")
        by_id = {e["receipt_id"]: e for e in cov["evidence"]}
        self.assertFalse(by_id["Rz"]["counts"])
        self.assertFalse(by_id["Rz"]["authority_matched"])

    def test_consignee_must_match_for_evidence_to_count(self):
        # A self-consistent receipt bound to S1 but with a different consignee.
        self.stack.delegation.ingest_receipts({"receipts": [
            receipt("RC", consignee="P0", signer="P0", claimed=(), signed_day=10)
        ]})
        self.depot.ingest_supplements({"receipts": [
            supplement("RC", consignee="PX", signer="P0", claimed=(),
                       parts=("BX1",))
        ]})
        cov = self.depot.coverage("S1")
        by_id = {e["receipt_id"]: e for e in cov["evidence"]}
        self.assertFalse(by_id["RC"]["counts"])
        self.assertFalse(by_id["RC"]["context_ok"])


class PreparationTests(DeliveryBase):
    def test_confirm_requires_preparation(self):
        with self.assertRaises(StalePreparationError):
            self.depot.confirm("S1")

    def test_prepare_then_evidence_change_rejects_stale_prep(self):
        prep = self.depot.prepare("S1")
        # Adding a part-covering original changes the evidence snapshot.
        self.stack.delegation.ingest_receipts({"receipts": [
            receipt("R5", signer="P0", claimed=(), signed_day=10)
        ]})
        self.depot.ingest_supplements({"receipts": [
            supplement("R5", signer="P0", claimed=(), parts=("BX1",))
        ]})
        with self.assertRaises(StalePreparationError):
            self.depot.confirm("S1", expected_evidence_hash=prep["evidence_hash"])

    def test_prepare_then_authority_change_rejects_stale_prep(self):
        prep = self.depot.prepare("S1")
        # Authority correction that does not (yet) break coverage but changes
        # the authority fingerprint (e.g. G2 correction that keeps it valid).
        from tests._helpers import grant
        self.stack.delegation.ingest_grants({"grants": [
            grant("G2", 2, "A", "B", start=5, end=15, parent="G1"),
        ]})
        with self.assertRaises(StalePreparationError):
            self.depot.confirm("S1", expected_evidence_hash=prep["evidence_hash"])

    def test_prepare_incomplete_raises(self):
        self.depot.ingest_shipments({"shipments": [{
            "shipment_id": "S9", "consignee": "P0", "region": "N",
            "document_type": "K", "required_parts": ["BX1"],
        }]})
        with self.assertRaises(StateError):
            self.depot.prepare("S9")

    def test_confirm_idempotent_same_evidence(self):
        prep = self.depot.prepare("S1")
        del1 = self.depot.confirm("S1", expected_evidence_hash=prep["evidence_hash"])
        again = self.depot.confirm("S1")
        self.assertEqual(again["confirmation_id"], del1["confirmation_id"])


class CompletenessIsolationTests(DeliveryBase):
    def test_one_valid_evidence_is_not_whole_shipment(self):
        # A separate shipment covered only by an invalid receipt (R2, boundary).
        self.depot.ingest_shipments({"shipments": [{
            "shipment_id": "S3", "consignee": "P0", "region": "N",
            "document_type": "K", "required_parts": ["BX1", "BX2"],
        }]})
        self.stack.delegation.ingest_receipts({"receipts": [
            receipt("R2b", signed_day=12)
        ]})
        self.depot.ingest_supplements({"receipts": [
            _alt_supplement("R2b", signed_day=12, shipment_id="S3",
                            parts=("BX1",))
        ]})
        cov = self.depot.coverage("S3")
        self.assertFalse(cov["complete"])
        self.assertTrue(cov["evidence"][0]["authority_matched"])
        self.assertFalse(cov["evidence"][0]["authority_valid"])
        self.assertEqual(cov["missing_parts"], ["BX1", "BX2"])

    def test_partial_coverage_missing_one_part(self):
        self.depot.ingest_shipments({"shipments": [{
            "shipment_id": "S4", "consignee": "P0", "region": "N",
            "document_type": "K", "required_parts": ["BX1", "BX2"],
        }]})
        self.stack.delegation.ingest_receipts({"receipts": [
            receipt("R1b", signer="P0", claimed=(), signed_day=10)
        ]})
        self.depot.ingest_supplements({"receipts": [
            _alt_supplement("R1b", signer="P0", claimed=(), shipment_id="S4",
                            parts=("BX1",))
        ]})
        cov = self.depot.coverage("S4")
        self.assertFalse(cov["complete"])
        self.assertEqual(cov["missing_parts"], ["BX2"])


def _alt_supplement(receipt_id, **kw):
    from tests._helpers import supplement
    return supplement(receipt_id, **kw)


if __name__ == "__main__":
    unittest.main()
