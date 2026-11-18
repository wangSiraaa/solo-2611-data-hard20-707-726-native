"""Independent nested-asset identity catalog (SQLite-backed).

The catalog owns exactly two facts per asset:

1. its stable identity (``asset_id`` + ``CONTAINER``/``ITEM`` type), and
2. its physical placement (at most one parent, only a ``CONTAINER`` may parent).

It deliberately stores no custodians: physical nesting never implies custody,
and the handoff warehouse in :mod:`asset_handoff.warehouse` is the only place
that records who is responsible for an asset.

All structural changes are *batch* operations. A batch is validated against
the **final** forest it would produce, never against intermediate states, so
the order of ``attaches``/``detaches``/``moves`` inside one batch cannot make
a temporarily invalid forest reject an ultimately valid one.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence

from .errors import (
    CycleError,
    DepthExceededError,
    DuplicateAssetError,
    InvalidParentError,
    UnknownAssetError,
)
CONTAINER = "CONTAINER"
ITEM = "ITEM"
_TYPES = frozenset((CONTAINER, ITEM))

# Maximum number of edges on a root -> node chain (root depth 0). The task
# bounds the forest at depth 8.
MAX_DEPTH = 8

_REVISION_KEY = "membership_revision"
_INITIAL_REVISION = 1


class AssetCatalog:
    """Read/write catalog of asset identities and container nesting."""

    def __init__(self, path: str):
        """Open (creating if needed) the catalog database at ``path``."""
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS assets (
                    asset_id TEXT PRIMARY KEY,
                    type     TEXT NOT NULL CHECK (type IN ('CONTAINER', 'ITEM'))
                );
                CREATE TABLE IF NOT EXISTS edges (
                    child_id  TEXT PRIMARY KEY REFERENCES assets(asset_id),
                    parent_id TEXT NOT NULL REFERENCES assets(asset_id)
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key   TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                );
                """
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
                (_REVISION_KEY, _INITIAL_REVISION),
            )

    # ------------------------------------------------------------------ lifecycle
    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    # ------------------------------------------------------------------ revision
    @property
    def revision(self) -> int:
        """Current membership revision; bumped once per mutating batch."""
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (_REVISION_KEY,)
        ).fetchone()
        return int(row[0])

    def _bump_revision(self) -> None:
        self.conn.execute(
            "UPDATE meta SET value = value + 1 WHERE key = ?", (_REVISION_KEY,)
        )

    # ------------------------------------------------------------------ identity
    def add_assets(self, assets: Sequence[Mapping[str, str]]) -> int:
        """Register new assets and return the new membership revision.

        ``assets`` is a sequence of ``{"asset_id": ..., "type": ...}`` mappings.
        Duplicate ids inside the batch or against the store are rejected, and
        no row is inserted for the whole batch. The optional custodian field
        accepted by the synthetic input format is ignored here: custody lives
        exclusively in the handoff warehouse.
        """
        prepared: list[tuple[str, str]] = []
        seen: set[str] = set()
        for entry in assets:
            asset_id = entry["asset_id"]
            asset_type = entry["type"]
            if asset_type not in _TYPES:
                raise ValueError(
                    f"asset {asset_id!r} has invalid type {asset_type!r}"
                )
            if asset_id in seen:
                raise DuplicateAssetError(asset_id)
            seen.add(asset_id)
            prepared.append((asset_id, asset_type))

        existing = {
            row[0]
            for row in self.conn.execute(
                f"SELECT asset_id FROM assets WHERE asset_id IN "
                f"({','.join('?' for _ in prepared)})",
                [asset_id for asset_id, _ in prepared],
            ).fetchall()
        } if prepared else set()
        for asset_id, _ in prepared:
            if asset_id in existing:
                raise DuplicateAssetError(asset_id)

        with self.conn:
            self.conn.executemany(
                "INSERT INTO assets(asset_id, type) VALUES (?, ?)", prepared
            )
            self._bump_revision()
        return self.revision

    def get_asset_type(self, asset_id: str) -> str:
        row = self.conn.execute(
            "SELECT type FROM assets WHERE asset_id = ?", (asset_id,)
        ).fetchone()
        if row is None:
            raise UnknownAssetError(asset_id)
        return row[0]

    def list_assets(self) -> list[dict[str, str]]:
        """Return all identities ordered by ``asset_id``."""
        return [
            {"asset_id": asset_id, "type": asset_type}
            for asset_id, asset_type in self.conn.execute(
                "SELECT asset_id, type FROM assets ORDER BY asset_id"
            )
        ]

    # ------------------------------------------------------------------ structure
    def get_parent(self, asset_id: str) -> str | None:
        """Return the current parent id, or ``None`` for a root / unknown edge."""
        # Prove the asset exists so callers get UnknownAssetError instead of None.
        self.get_asset_type(asset_id)
        row = self.conn.execute(
            "SELECT parent_id FROM edges WHERE child_id = ?", (asset_id,)
        ).fetchone()
        return None if row is None else row[0]

    def parent_chain(self, asset_id: str) -> list[str]:
        """Return ids from ``asset_id`` up through each parent to the root.

        A root returns its own one-element chain.
        """
        chain: list[str] = []
        current: str | None = asset_id
        guard: set[str] = set()
        while current is not None:
            if current in guard:  # defensive; the forest invariants forbid this
                raise CycleError(f"parent chain cycles at {current!r}")
            guard.add(current)
            chain.append(current)
            current = self.get_parent(current)
        return chain

    def subtree(self, root_id: str) -> list[str]:
        """All identity ids in ``root_id``'s subtree, including the root itself."""
        return self.expand_roots([root_id])

    def expand_roots(self, root_ids: Sequence[str]) -> list[str]:
        """Expand a set of scanned roots into every covered identity.

        Rules:

        * every scanned root is included, including root containers themselves;
        * when both an ancestor and one of its descendants are scanned, the
          descendant is reached through the ancestor and counted exactly once;
        * identities are distinct by ``asset_id``: same-named *different* ids
          are separate assets and both appear;
        * output order is preorder DFS with siblings sorted by id, which is
          deterministic and keeps parents ahead of their children.
        """
        children = self._children_map()
        roots = list(root_ids)
        for root_id in roots:
            self.get_asset_type(root_id)

        visited: set[str] = set()
        order: list[str] = []

        def walk(node_id: str) -> None:
            if node_id in visited:
                return
            visited.add(node_id)
            order.append(node_id)
            for child_id in children.get(node_id, ()):
                walk(child_id)

        for root_id in roots:
            walk(root_id)
        return order

    def revise_membership(
        self,
        *,
        attaches: Sequence[Mapping[str, str]] = (),
        detaches: Sequence[str] = (),
        moves: Sequence[Mapping[str, str]] = (),
    ) -> int:
        """Apply one batch of structural changes and return the new revision.

        * ``attaches``: ``{"child_id", "parent_id"}`` for currently-root assets;
        * ``detaches``: child ids that become roots (their edge is dropped);
        * ``moves``: ``{"asset_id", "new_parent"}`` replacing an existing edge.

        The whole batch is validated against the final forest it would produce:
        unknown assets, non-container parents, duplicated identities/edges,
        cycles and chains deeper than :data:`MAX_DEPTH` reject everything.
        """
        attaches = list(attaches)
        detaches = list(detaches)
        moves = list(moves)

        parent, types = self._load_maps()
        tentative = dict(parent)

        # One asset may only be rewritten once per batch: ambiguous batches
        # cannot hide duplicate identities or conflicting placements.
        touched: set[str] = set()

        def claim(child_id: str) -> None:
            if child_id in touched:
                raise DuplicateAssetError(child_id)
            touched.add(child_id)

        for entry in attaches:
            child_id = entry["child_id"]
            parent_id = entry["parent_id"]
            self._require_known(child_id, types)
            self._require_known(parent_id, types)
            if child_id in parent:
                raise DuplicateAssetError(child_id)
            claim(child_id)
            tentative[child_id] = parent_id

        for child_id in detaches:
            self._require_known(child_id, types)
            if child_id not in parent:
                raise InvalidParentError(
                    f"cannot detach {child_id!r}: it is already a root"
                )
            claim(child_id)
            tentative.pop(child_id, None)

        for entry in moves:
            child_id = entry["asset_id"]
            new_parent = entry["new_parent"]
            self._require_known(child_id, types)
            self._require_known(new_parent, types)
            if child_id not in parent:
                raise InvalidParentError(
                    f"cannot move {child_id!r}: it has no parent; use attach"
                )
            claim(child_id)
            tentative[child_id] = new_parent

        self._validate_final_forest(tentative, types)

        with self.conn:
            for entry in attaches:
                self.conn.execute(
                    "INSERT INTO edges(child_id, parent_id) VALUES (?, ?)",
                    (entry["child_id"], entry["parent_id"]),
                )
            if detaches:
                placeholders = ",".join("?" for _ in detaches)
                self.conn.execute(
                    f"DELETE FROM edges WHERE child_id IN ({placeholders})",
                    detaches,
                )
            for entry in moves:
                self.conn.execute(
                    "UPDATE edges SET parent_id = ? WHERE child_id = ?",
                    (entry["new_parent"], entry["asset_id"]),
                )
            self._bump_revision()
        return self.revision

    # ------------------------------------------------------------------ internals
    def _load_maps(self) -> tuple[dict[str, str], dict[str, str]]:
        parent = {
            child_id: parent_id
            for child_id, parent_id in self.conn.execute(
                "SELECT child_id, parent_id FROM edges"
            )
        }
        types = {
            asset_id: asset_type
            for asset_id, asset_type in self.conn.execute(
                "SELECT asset_id, type FROM assets"
            )
        }
        return parent, types

    def _children_map(self) -> dict[str, list[str]]:
        children: dict[str, list[str]] = {}
        for child_id, parent_id in self.conn.execute(
            "SELECT child_id, parent_id FROM edges ORDER BY child_id"
        ):
            children.setdefault(parent_id, []).append(child_id)
        for siblings in children.values():
            siblings.sort()
        return children

    @staticmethod
    def _require_known(asset_id: str, types: Mapping[str, str]) -> None:
        if asset_id not in types:
            raise UnknownAssetError(asset_id)

    @staticmethod
    def _validate_final_forest(
        parent: Mapping[str, str], types: Mapping[str, str]
    ) -> None:
        """Validate the tentative final parent map as one forest."""
        # Only containers may parent.
        for child_id, parent_id in parent.items():
            if types.get(parent_id) != CONTAINER:
                raise InvalidParentError(
                    f"asset {parent_id!r} cannot be parent of {child_id!r}: "
                    f"only CONTAINER assets parent, got {types.get(parent_id)!r}"
                )

        # Every chain must terminate at a root, never revisit a node (no cycle),
        # and never exceed the depth limit. Iterating every node is O(n * depth)
        # which is cheap at the 200-asset / depth-8 scale and needs no recursion.
        memo: dict[str, int] = {}
        for start in parent:
            chain: list[str] = []
            seen_in_chain: set[str] = set()
            node: str | None = start
            while node in parent:
                if node in seen_in_chain:
                    raise CycleError(
                        "cycle detected involving "
                        + " -> ".join(chain + [node])
                    )
                if node in memo:
                    depth_here = len(chain) + memo[node]
                    break
                seen_in_chain.add(node)
                chain.append(node)
                node = parent[node]
            else:
                # `node` is a root (no parent edge); depth of a root is 0.
                depth_here = len(chain)

            if depth_here > MAX_DEPTH:
                raise DepthExceededError(
                    f"asset {start!r} would be at depth {depth_here} "
                    f"(limit {MAX_DEPTH})"
                )
            # Memoize each chain node's root-relative depth so later walks
            # reuse it instead of re-walking the same suffix.
            for idx, node_id in enumerate(chain):
                memo[node_id] = depth_here - idx
