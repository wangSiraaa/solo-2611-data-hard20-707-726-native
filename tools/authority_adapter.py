"""Read-only authority adapter: the delivery depot's view of the delegation side.

This is an integration-layer object.  Neither business package imports the
other: the depot only depends on the ``AuthorityPort`` protocol, and this
adapter supplies the concrete delegation-backed implementation.  The adapter
never mutates delegation state; it matches a delivery supplement against a
stored receipt original and forwards that original's assessment.

A supplement's receipt content is matched byte-for-byte (canonically) against
the delegation original.  If the same receipt id exists but its content
differs, the supplement cannot lean on it (``matched`` is False).
"""

from __future__ import annotations

from delegation.service import DelegationService
from delegation.util import content_hash


class DelegationAuthority:
    def __init__(self, delegation_db_path: str):
        self._delegation = DelegationService(delegation_db_path)

    def close(self) -> None:
        self._delegation.close()

    def assess(self, receipt_core: dict) -> dict:
        rid = receipt_core["receipt_id"]
        try:
            original = self._delegation.get_receipt(rid)
        except Exception:
            return {
                "receipt_id": rid,
                "matched": False,
                "valid": False,
                "reason": f"no receipt original {rid!r} on the delegation side",
                "levels": [],
                "original_hash": None,
                "authority_fingerprint": self._fingerprint(receipt_core.get("claimed_grant_ids", [])),
            }

        supplied = {
            "receipt_id": receipt_core["receipt_id"],
            "consignee": receipt_core["consignee"],
            "signer": receipt_core["signer"],
            "signed_day": receipt_core["signed_day"],
            "received_day": receipt_core["received_day"],
            "region": receipt_core["region"],
            "document_type": receipt_core["document_type"],
            "claimed_chain": list(receipt_core.get("claimed_grant_ids", [])),
        }
        if content_hash(supplied) != original["content_hash"]:
            return {
                "receipt_id": rid,
                "matched": False,
                "valid": False,
                "reason": "supplement content differs from the stored original (异文)",
                "levels": [],
                "original_hash": original["content_hash"],
                "authority_fingerprint": self._fingerprint(supplied["claimed_chain"]),
            }

        assessment = self._delegation.assess_receipt(rid)
        return {
            "receipt_id": rid,
            "matched": True,
            "valid": assessment["valid"],
            "reason": assessment["reason"],
            "levels": assessment["levels"],
            "original_hash": original["content_hash"],
            "authority_fingerprint": self._fingerprint(supplied["claimed_chain"]),
        }

    def _fingerprint(self, claimed_chain: list[str]) -> str:
        """Stable digest of current authority for the claimed chain.

        Includes the newest revision and its content hash for every claimed
        grant, so any granted revision (including revocation/nominal
        corrections) changes the fingerprint.
        """
        parts = []
        for gid in claimed_chain:
            current = self._delegation.latest_grant(gid)
            if current is None:
                parts.append({"grant_id": gid, "missing": True})
            else:
                parts.append(
                    {
                        "grant_id": gid,
                        "revision": current["revision"],
                        "grant_hash": current["content_hash"],
                    }
                )
        return content_hash(parts)
