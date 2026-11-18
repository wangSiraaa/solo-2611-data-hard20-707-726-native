"""Independent delegation verification service (委托核查服务).

This package owns grants, receipt originals, chain assessment and immutable
assessment reports.  It has no knowledge of shipments, parts, delivery
coverage or delivery confirmations.
"""

from .service import DelegationService

__all__ = ["DelegationService"]
