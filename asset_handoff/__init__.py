"""Nested asset catalog and custody handoff warehouse.

Two independent stores that deliberately know different things:

* :class:`~asset_handoff.catalog.AssetCatalog` only stores asset identity
  (``asset_id`` + type) and the physical parent/child forest. It never stores
  custodians.
* :class:`~asset_handoff.warehouse.HandoffWarehouse` only stores current
  custodians and frozen handoff membership. It asks the catalog for identity,
  type and subtree expansion instead of duplicating the forest.
"""

from .catalog import AssetCatalog, CONTAINER, ITEM
from .warehouse import HandoffWarehouse
from .errors import (
    AssetHandoffError,
    UnknownAssetError,
    DuplicateAssetError,
    DuplicateHandoffError,
    InvalidParentError,
    CycleError,
    DepthExceededError,
    CustodyError,
    CustodianMismatchError,
    PreparedStaleError,
    HandoffNotFoundError,
)

__all__ = [
    "AssetCatalog",
    "HandoffWarehouse",
    "CONTAINER",
    "ITEM",
    "AssetHandoffError",
    "UnknownAssetError",
    "DuplicateAssetError",
    "DuplicateHandoffError",
    "InvalidParentError",
    "CycleError",
    "DepthExceededError",
    "CustodyError",
    "CustodianMismatchError",
    "PreparedStaleError",
    "HandoffNotFoundError",
]
