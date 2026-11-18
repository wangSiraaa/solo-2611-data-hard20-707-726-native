"""Independent classification catalog and fact summary store.

Public entry points:

* :mod:`cs.catalog` - pure DAG algorithms (cycles, ancestors, descendants,
  coverage explanation).
* :mod:`cs.store` - SQLite-backed catalog and fact store with atomic bundles
  and cut-pinned queries.
* :mod:`cs.cli` - command line interface.
"""

from .store import (  # noqa: F401
    CatalogError,
    CatalogState,
    Committed,
    ConflictError,
    Cut,
    Fact,
    FactError,
    ProductError,
    Store,
    StoreError,
    ingest_operation,
)

__all__ = [
    "CatalogError",
    "CatalogState",
    "Committed",
    "ConflictError",
    "Cut",
    "Fact",
    "FactError",
    "ProductError",
    "Store",
    "StoreError",
    "ingest_operation",
]
