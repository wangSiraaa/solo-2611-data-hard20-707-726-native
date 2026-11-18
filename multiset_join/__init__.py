"""Signed multiset source catalog + replayable LEFT JOIN derived store.

The source catalog is an independent, durable multiset of R/S tuples with
positive integer multiplicities.  Batches carry signed deltas, are merged per
complete tuple, must leave every count non-negative, and are keyed by a
canonical digest of their merged content so that a batch can be replayed
idempotently.  The derived store consumes materialised source slices; each
result is pinned to the source version it was built from and does not change
when later batches arrive.
"""

from .errors import (
    BatchConflictError,
    CatalogError,
    MultisetError,
    NegativeCountError,
    ResultConflictError,
    UnknownBatchError,
    UnknownResultError,
    UnknownSliceError,
    ValidationError,
)
from .catalog import SourceCatalog, ApplyResult, BatchRecord
from .derive import DerivedStore, derive_slice
from .runner import load_document, run_document_path, run_operations

__all__ = [
    "SourceCatalog",
    "DerivedStore",
    "derive_slice",
    "run_operations",
    "run_document_path",
    "load_document",
    "ApplyResult",
    "BatchRecord",
    "MultisetError",
    "ValidationError",
    "CatalogError",
    "NegativeCountError",
    "BatchConflictError",
    "UnknownBatchError",
    "UnknownSliceError",
    "ResultConflictError",
    "UnknownResultError",
]
