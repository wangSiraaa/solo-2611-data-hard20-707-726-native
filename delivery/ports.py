"""Authority port consumed by the delivery depot.

The depot must not re-derive delegation authority.  It asks an injected
authority provider to assess a receipt original and receives a verdict.  The
provider is responsible for matching the receipt content against the
delegation store; the depot only records what it was told, together with the
fingerprint it was given.
"""

from __future__ import annotations

from typing import Protocol


class AuthorityPort(Protocol):
    def assess(self, receipt_core: dict) -> dict:
        """Return a verdict for a receipt original.

        Required keys in the returned mapping:
            matched (bool)     -- content matched a stored original on the
                                  delegation side (False => cannot rely on it)
            valid   (bool)     -- delegation-side assessment result
            reason  (str|None) -- human-readable failure reason
            levels  (list)     -- per-level grant revision/validity detail
            original_hash (str|None) -- hash of the matched original content
            authority_fingerprint (str) -- stable digest of the authority state
                                           used for the verdict
        """
        ...
