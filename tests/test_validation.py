"""Input-validation tests for the delegation side (batch rejection, conflicts)."""

from __future__ import annotations

import unittest

from delegation.errors import ConflictError, ValidationError
from tests._helpers import TempStackCase, grant


class BatchValidationTests(TempStackCase):
    def test_whole_batch_rejected_on_bad_grant(self):
        # Second grant is malformed; nothing may be written.
        with self.assertRaises(ValidationError):
            self.stack.delegation.ingest_grants({"grants": [
                grant("G1", 1),
                {"grant_id": "G2", "revision": "one"},  # bad
            ]})
        self.assertEqual(self.stack.delegation.count_grants(), 0)

    def test_duplicate_id_different_content_in_batch_rejected(self):
        with self.assertRaises(ConflictError):
            self.stack.delegation.ingest_grants({"grants": [
                grant("G1", 1, agent="A"),
                grant("G1", 1, agent="B"),
            ]})
        self.assertEqual(self.stack.delegation.count_grants(), 0)

    def test_same_grant_replayed_is_idempotent(self):
        first = self.stack.delegation.ingest_grants({"grants": [grant("G1", 1)]})
        second = self.stack.delegation.ingest_grants({"grants": [grant("G1", 1)]})
        self.assertEqual(first["applied"], 1)
        self.assertEqual(second["applied"], 0)
        self.assertEqual(self.stack.delegation.count_grants(), 1)

    def test_conflicting_revision_content_rejected(self):
        self.stack.delegation.ingest_grants({"grants": [grant("G1", 1, agent="A")]})
        with self.assertRaises(ConflictError):
            self.stack.delegation.ingest_grants({"grants": [grant("G1", 1, agent="Z")]})

    def test_older_revision_rejected(self):
        self.stack.delegation.ingest_grants({"grants": [grant("G1", 5)]})
        with self.assertRaises(ValidationError):
            self.stack.delegation.ingest_grants({"grants": [grant("G1", 3)]})

    def test_revocation_outside_nominal_rejected(self):
        with self.assertRaises(ValidationError):
            self.stack.delegation.ingest_grants({"grants": [
                grant("G1", 1, start=0, end=20, revoked=25)
            ]})

    def test_revocation_at_interval_end_rejected(self):
        # Half-open [start,end): a revocation at exactly end can never apply.
        with self.assertRaises(ValidationError):
            self.stack.delegation.ingest_grants({"grants": [
                grant("G1", 1, start=0, end=20, revoked=20)
            ]})

    def test_revocation_at_interval_start_allowed(self):
        self.stack.delegation.ingest_grants({"grants": [
            grant("G1", 1, start=0, end=20, revoked=0)
        ]})
        self.stack.delegation.ingest_receipts({"receipts": [
            {"receipt_id": "R0", "consignee": "P0", "signer": "A",
             "signed_day": 0, "received_day": 1, "region": "N",
             "document_type": "K", "claimed_grant_ids": ["G1"]}
        ]})
        # Signed at day 0 == revoked_from 0 => invalid.
        self.assertFalse(self.stack.delegation.assess_receipt("R0")["valid"])

    def test_receipt_duplicate_id_different_content_rejected(self):
        self.stack.delegation.ingest_grants({"grants": [grant("G1", 1)]})
        self.stack.delegation.ingest_receipts({"receipts": [
            {"receipt_id": "R1", "consignee": "P0", "signer": "A",
             "signed_day": 10, "received_day": 18, "region": "N",
             "document_type": "K", "claimed_grant_ids": ["G1"]}
        ]})
        with self.assertRaises(ConflictError):
            self.stack.delegation.ingest_receipts({"receipts": [
                {"receipt_id": "R1", "consignee": "P0", "signer": "A",
                 "signed_day": 30, "received_day": 18, "region": "N",
                 "document_type": "K", "claimed_grant_ids": ["G1"]}
            ]})

    def test_same_receipt_replay_idempotent(self):
        self.stack.delegation.ingest_grants({"grants": [grant("G1", 1)]})
        body = {"receipts": [{"receipt_id": "R1", "consignee": "P0",
                              "signer": "A", "signed_day": 10, "received_day": 18,
                              "region": "N", "document_type": "K",
                              "claimed_grant_ids": ["G1"]}]}
        self.assertEqual(self.stack.delegation.ingest_receipts(body)["new"], 1)
        replay = self.stack.delegation.ingest_receipts(body)
        self.assertEqual(replay["new"], 0)
        self.assertEqual(replay["replayed"], 1)
        self.assertEqual(self.stack.delegation.count_receipts(), 1)

    def test_claimed_chain_duplicate_rejected(self):
        with self.assertRaises(ValidationError):
            self.stack.delegation.ingest_receipts({"receipts": [
                {"receipt_id": "R1", "consignee": "P0", "signer": "A",
                 "signed_day": 10, "received_day": 18, "region": "N",
                 "document_type": "K", "claimed_grant_ids": ["G1", "G1"]}
            ]})
        self.assertEqual(self.stack.delegation.count_receipts(), 0)


if __name__ == "__main__":
    unittest.main()
