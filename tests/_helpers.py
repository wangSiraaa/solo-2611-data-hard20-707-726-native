"""Shared test helpers: temp databases that are cleaned up automatically."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from delegation import DelegationService  # noqa: E402
from delivery import DeliveryService  # noqa: E402
from tools.authority_adapter import DelegationAuthority  # noqa: E402


def grant(grant_id="G1", revision=1, issuer="P0", agent="A", regions=("N",),
          document_types=("K",), start=0, end=20, can_redelegate=True,
          parent=None, revoked=None):
    return {
        "grant_id": grant_id, "revision": revision, "issuer": issuer, "agent": agent,
        "regions": list(regions), "document_types": list(document_types),
        "start": start, "end": end, "can_redelegate": can_redelegate,
        "parent_grant_id": parent, "revoked_from": revoked,
    }


def receipt(receipt_id="R1", consignee="P0", signer="B", signed_day=10,
            received_day=18, region="N", document_type="K",
            claimed=("G1", "G2")):
    return {
        "receipt_id": receipt_id, "consignee": consignee, "signer": signer,
        "signed_day": signed_day, "received_day": received_day, "region": region,
        "document_type": document_type, "claimed_grant_ids": list(claimed),
    }


def supplement(receipt_id="R1", consignee="P0", signer="B", signed_day=10,
               received_day=18, region="N", document_type="K",
               claimed=("G1", "G2"), shipment_id="S1", parts=("BX1", "BX2")):
    return {
        "receipt_id": receipt_id, "consignee": consignee, "signer": signer,
        "signed_day": signed_day, "received_day": received_day, "region": region,
        "document_type": document_type, "claimed_grant_ids": list(claimed),
        "shipment_id": shipment_id, "part_ids": list(parts),
    }


class TempStack:
    """A delegation service + adapter + delivery depot over temp DBs."""

    def __init__(self, tmpdir: Path):
        self.delegation = DelegationService(tmpdir / "delegation.db")
        self.authority = DelegationAuthority(str(tmpdir / "delegation.db"))
        self.delivery = DeliveryService(tmpdir / "delivery.db", self.authority)

    def close(self):
        self.delivery.close()
        self.authority.close()
        self.delegation.close()

    def seed_eight_hundred(self):
        """The materials/756 baseline: G1 (revoked 12) -> G2, receipts R1..R3."""
        self.delegation.ingest_grants({"grants": [
            grant("G1", 1, "P0", "A", start=0, end=20, can_redelegate=True,
                  revoked=12),
            grant("G2", 1, "A", "B", start=5, end=15, can_redelegate=False,
                  parent="G1"),
        ]})
        self.delegation.ingest_receipts({"receipts": [
            receipt("R1", signer="B", signed_day=10, claimed=("G1", "G2")),
            receipt("R2", signer="B", signed_day=12, claimed=("G1", "G2")),
            receipt("R3", signer="B", signed_day=10, region="S",
                    claimed=("G1", "G2")),
        ]})


class TempStackCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.stack = TempStack(self.tmpdir)

    def tearDown(self):
        self.stack.close()
        self._tmp.cleanup()
