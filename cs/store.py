"""SQLite-backed revisioned catalog and fact store with atomic bundles.

Consistency model
-----------------
Only committed state is visible.  A bundle is validated against the projected
post-bundle catalog and post-bundle fact state, then *all* of its revision
records and the working tables are written inside one ``BEGIN IMMEDIATE``
transaction.  Any failure rolls the whole bundle back, so no reader can
observe half of it.

A query resolves every target from one cut (catalog revision + fact
watermark) inside a single ``BEGIN DEFERRED`` read transaction, whose snapshot
is fixed by its first read.  The same execution record feeds the union and any
export, so results cannot be assembled from different points in time.  The
exported product stores its cut and a copy of the payload; later publications
never rewrite it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from . import catalog

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    seq        INTEGER NOT NULL,
    request_id TEXT,
    kind       TEXT NOT NULL,
    details    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_request ON events (request_id, seq, event_id);
CREATE TABLE IF NOT EXISTS catalog_nodes (
    node_id TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    active  INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS catalog_edges (
    child_id  TEXT NOT NULL,
    parent_id TEXT NOT NULL,
    active    INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (child_id, parent_id)
);
CREATE TABLE IF NOT EXISTS catalog_commits (
    catalog_revision TEXT PRIMARY KEY,
    parent_revision  TEXT,
    request_id       TEXT,
    created_seq      INTEGER NOT NULL,
    nodes            TEXT NOT NULL,
    edges            TEXT NOT NULL,
    explanation      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tickets (
    ticket_id TEXT PRIMARY KEY,
    revision  INTEGER NOT NULL,
    amount    INTEGER NOT NULL,
    classes   TEXT NOT NULL,
    active    INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS fact_commits (
    commit_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id    TEXT NOT NULL,
    revision     INTEGER NOT NULL,
    amount       INTEGER NOT NULL,
    classes      TEXT NOT NULL,
    active       INTEGER NOT NULL,
    request_id   TEXT,
    content_hash TEXT NOT NULL,
    created_seq  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS fact_commits_ticket ON fact_commits (ticket_id, commit_id);
CREATE INDEX IF NOT EXISTS fact_commits_seq ON fact_commits (created_seq, commit_id);
CREATE TABLE IF NOT EXISTS fact_watermarks (
    watermark INTEGER PRIMARY KEY,
    seq       INTEGER NOT NULL,
    revision  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
    product_id       TEXT PRIMARY KEY,
    name             TEXT,
    catalog_revision TEXT NOT NULL,
    fact_revision    TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    payload          TEXT NOT NULL
);
"""


class StoreError(Exception):
    """Base class for rejected store operations."""


class CatalogError(StoreError):
    """Catalog validation failure (unknown node or cycle)."""


class FactError(StoreError):
    """Fact validation or revision conflict."""

    def __init__(self, message: str, ticket_id: str | None = None) -> None:
        super().__init__(message)
        self.ticket_id = ticket_id


class ConflictError(StoreError):
    """Optimistic concurrency conflict."""


class ProductError(StoreError):
    """Product lookup/immutability failure."""


@dataclass(frozen=True)
class Fact:
    ticket_id: str
    revision: int
    amount: int
    classes: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "revision": self.revision,
            "amount": self.amount,
            "classes": list(self.classes),
        }


@dataclass(frozen=True)
class Cut:
    catalog_revision: str
    fact_revision: str
    fact_watermark: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "catalog_revision": self.catalog_revision,
            "fact_revision": self.fact_revision,
            "fact_watermark": self.fact_watermark,
        }


@dataclass
class CatalogState:
    revision: str
    nodes: dict[str, str] = field(default_factory=dict)
    edges: frozenset[tuple[str, str]] = frozenset()

    @property
    def edge_list(self) -> list[tuple[str, str]]:
        return sorted(self.edges)

    def as_json(self) -> dict[str, Any]:
        return {
            "format": "classification-catalog/1",
            "revision": self.revision,
            "nodes": [
                {"id": node_id, "name": name}
                for node_id, name in sorted(self.nodes.items())
            ],
            "edges": [list(edge) for edge in self.edge_list],
        }


@dataclass
class Committed:
    request_id: str | None
    cut: Cut
    catalog_revision: str
    parent_catalog_revision: str | None
    edge_delta: dict[str, Any]
    facts: list[Fact] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    noop: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "cut": self.cut.as_dict(),
            "catalog_revision": self.catalog_revision,
            "parent_catalog_revision": self.parent_catalog_revision,
            "edge_delta": self.edge_delta,
            "facts": [f.as_dict() for f in self.facts],
            "warnings": self.warnings,
            "noop": self.noop,
        }


# --------------------------------------------------------------------- helpers


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(payload: Any) -> str:
    return hashlib.sha256(_json_dump(payload).encode("utf-8")).hexdigest()


def _canonical_fact(
    ticket_id: str, revision: Any, amount: Any, classes: Iterable[str]
) -> dict[str, Any]:
    # Values are kept raw so that type validation (e.g. rejecting 1.5 or a
    # string amount) happens against the actual request, not a coerced copy.
    return {
        "ticket_id": ticket_id,
        "revision": revision,
        "amount": amount,
        "classes": sorted(set(classes)),
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_revision_number(name: str | None, prefix: str) -> int:
    if not name or not name.startswith(prefix):
        return 0
    try:
        return int(name[len(prefix):])
    except ValueError:
        return 0


def ingest_operation(operation: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Normalise a request to ``(kind, canonical_payload)``.

    Kinds: ``bundle``, ``fact``, ``catalog``.  Canonical form makes request
    idempotency hashes stable.
    """

    if "operation" not in operation:
        raise StoreError("missing 'operation' field")
    kind = {
        "bundle": "bundle",
        "fact": "fact",
        "fact_revision": "fact",
        "catalog": "catalog",
        "catalog_revision": "catalog",
        "edges": "catalog",
    }.get(operation["operation"])
    if kind is None:
        raise StoreError(f"unknown operation {operation['operation']!r}")

    if kind == "bundle":
        payload: dict[str, Any] = {
            "add_edges": [list(e) for e in operation.get("add_edges", [])],
            "remove_edges": [list(e) for e in operation.get("remove_edges", [])],
            "fact_revisions": [
                _canonical_fact(
                    fr["ticket_id"],
                    fr["revision"],
                    fr["amount"],
                    fr.get("classes", []),
                )
                for fr in operation.get("fact_revisions", [])
            ],
        }
        if "metadata" in operation:
            payload["metadata"] = operation["metadata"]
        return kind, payload

    if kind == "catalog":
        payload = {
            "nodes": [
                {"id": n["id"], "name": n.get("name", n["id"])}
                for n in operation.get("nodes", [])
            ],
            "add_edges": [list(e) for e in operation.get("add_edges", [])],
            "remove_edges": [list(e) for e in operation.get("remove_edges", [])],
        }
        if "metadata" in operation:
            payload["metadata"] = operation["metadata"]
        return kind, payload

    return kind, _canonical_fact(
        operation["ticket_id"],
        operation["revision"],
        operation["amount"],
        operation.get("classes", []),
    )


class _Transaction:
    """``BEGIN IMMEDIATE`` writer transaction; implies the write lock."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")


class _ReadTransaction:
    """``BEGIN DEFERRED`` transaction; snapshot fixed by the first read."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN DEFERRED")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")


class Store:
    """A catalog and fact store over one local SQLite database file."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.conn = connection
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._write_lock = threading.RLock()

    # ------------------------------------------------------------------ setup
    @classmethod
    def open(cls, path: str) -> "Store":
        conn = sqlite3.connect(
            path, timeout=30.0, isolation_level=None, check_same_thread=False
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return cls(conn)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------- internals
    def _meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def _meta_int(self, key: str, default: int = 0) -> int:
        raw = self._meta(key)
        return int(raw) if raw is not None else default

    def _set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def _next_seq(self) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM events").fetchone()
        return int(row["n"])

    def _record_event(
        self, seq: int, request_id: str | None, kind: str, details: dict[str, Any]
    ) -> None:
        self.conn.execute(
            "INSERT INTO events(seq, request_id, kind, details) VALUES(?,?,?,?)",
            (seq, request_id, kind, _json_dump(details)),
        )

    def _event_for(self, request_id: str | None) -> sqlite3.Row | None:
        if not request_id:
            return None
        # The last event recorded for a commit unit wins; bundles record a
        # final "bundle" event after the catalog/fact events.
        return self.conn.execute(
            "SELECT * FROM events WHERE request_id=? "
            "ORDER BY seq DESC, event_id DESC LIMIT 1",
            (request_id,),
        ).fetchone()

    # --------------------------------------------------------------- catalog
    def is_initialized(self) -> bool:
        return self._meta("catalog_revision") is not None

    def catalog_revision(self) -> str | None:
        return self._meta("catalog_revision")

    def fact_revision(self) -> str:
        return self._meta("fact_revision", "F0") or "F0"

    def fact_watermark(self) -> int:
        return self._meta_int("fact_watermark", 0)

    def current_cut(self) -> Cut:
        # One SELECT so the three watermarks come from the same snapshot even
        # when this is called outside an enclosing transaction.
        rows = self.conn.execute(
            "SELECT key, value FROM meta WHERE key IN "
            "('catalog_revision', 'fact_revision', 'fact_watermark')"
        ).fetchall()
        meta = {row["key"]: row["value"] for row in rows}
        return Cut(
            catalog_revision=meta.get("catalog_revision") or "G0",
            fact_revision=meta.get("fact_revision") or "F0",
            fact_watermark=int(meta.get("fact_watermark", "0") or 0),
        )

    def _load_catalog_nodes(self) -> dict[str, str]:
        rows = self.conn.execute(
            "SELECT node_id, name FROM catalog_nodes WHERE active=1"
        ).fetchall()
        return {r["node_id"]: r["name"] for r in rows}

    def _load_catalog_edges(self) -> frozenset[tuple[str, str]]:
        rows = self.conn.execute(
            "SELECT child_id, parent_id FROM catalog_edges WHERE active=1"
        ).fetchall()
        return frozenset((r["child_id"], r["parent_id"]) for r in rows)

    def load_catalog_state(self, revision: str | None = None) -> CatalogState:
        revision = revision or self.catalog_revision()
        if revision is None:
            return CatalogState(revision="G0")
        row = self.conn.execute(
            "SELECT * FROM catalog_commits WHERE catalog_revision=?", (revision,)
        ).fetchone()
        if row is None:
            raise CatalogError(f"unknown catalog revision {revision!r}")
        nodes = {
            n["id"]: n["name"]
            for n in json.loads(row["nodes"])
        }
        edges = frozenset(tuple(e) for e in json.loads(row["edges"]))
        return CatalogState(revision=revision, nodes=nodes, edges=edges)

    def _write_catalog_snapshot(
        self,
        *,
        revision: str,
        parent: str | None,
        request_id: str | None,
        seq: int,
        nodes: dict[str, str],
        edges: frozenset[tuple[str, str]],
        explanation: dict[str, Any],
    ) -> None:
        self.conn.execute(
            "INSERT INTO catalog_commits"
            "(catalog_revision, parent_revision, request_id, created_seq, nodes, edges, explanation)"
            " VALUES(?,?,?,?,?,?,?)",
            (
                revision,
                parent,
                request_id,
                seq,
                _json_dump(
                    [{"id": nid, "name": name} for nid, name in sorted(nodes.items())]
                ),
                _json_dump([list(e) for e in sorted(edges)]),
                _json_dump(explanation),
            ),
        )
        self.conn.execute("UPDATE catalog_nodes SET active=0")
        self.conn.execute("UPDATE catalog_edges SET active=0")
        for nid, name in nodes.items():
            self.conn.execute(
                "INSERT INTO catalog_nodes(node_id, name, active) VALUES(?,?,1) "
                "ON CONFLICT(node_id) DO UPDATE SET name=excluded.name, active=1",
                (nid, name),
            )
        for child, parent_id in sorted(edges):
            self.conn.execute(
                "INSERT INTO catalog_edges(child_id, parent_id, active) VALUES(?,?,1) "
                "ON CONFLICT(child_id, parent_id) DO UPDATE SET active=1",
                (child, parent_id),
            )
        counter = _parse_revision_number(revision, "G")
        self._set_meta("catalog_revision", revision)
        self._set_meta("catalog_counter", str(counter))

    def init_catalog(
        self, data: dict[str, Any], *, request_id: str | None = None
    ) -> CatalogState:
        """Initialize an empty store from a classification-catalog/1 document."""

        if self.is_initialized():
            raise CatalogError("catalog already initialized")
        if data.get("format", "classification-catalog/1") != "classification-catalog/1":
            raise CatalogError("unsupported catalog format")
        nodes: dict[str, str] = {}
        for node in data.get("nodes", []):
            nid = node["id"]
            if not isinstance(nid, str) or not nid:
                raise CatalogError("node id must be a non-empty string")
            if nid in nodes:
                raise CatalogError(f"duplicate node id {nid!r}")
            nodes[nid] = node.get("name", nid)
        edges: set[tuple[str, str]] = set()
        for raw in data.get("edges", []):
            if len(raw) != 2:
                raise CatalogError(f"edge must be [child,parent]: {raw!r}")
            child, parent = str(raw[0]), str(raw[1])
            if child not in nodes or parent not in nodes:
                raise CatalogError(f"edge references unknown node: {child}->{parent}")
            if child == parent:
                raise CatalogError(f"self edge {child}->{parent} creates a cycle")
            edges.add((child, parent))
        cycle = catalog.find_cycle(nodes, edges)
        if cycle is not None:
            raise CatalogError("catalog contains a cycle: " + " -> ".join(cycle))

        revision = data.get("revision") or "G1"
        with self._write_lock, _Transaction(self.conn):
            seq = self._next_seq()
            explanation = {
                "operation": "init",
                "request_id": request_id,
                "catalog_revision": revision,
                "node_count": len(nodes),
                "edge_count": len(edges),
            }
            self._write_catalog_snapshot(
                revision=revision,
                parent=None,
                request_id=request_id,
                seq=seq,
                nodes=nodes,
                edges=frozenset(edges),
                explanation=explanation,
            )
            self._record_event(seq, request_id, "init", explanation)
            if self._meta("fact_revision") is None:
                self._set_meta("fact_revision", "F0")
                self._set_meta("fact_watermark", "0")
                self._set_meta("fact_counter", "0")
        return CatalogState(revision=revision, nodes=nodes, edges=frozenset(edges))

    def _project_catalog(
        self,
        *,
        add_nodes: Sequence[dict[str, str]],
        remove_edges: Sequence[Sequence[str]],
        add_edges: Sequence[Sequence[str]],
        base_nodes: dict[str, str] | None = None,
        base_edges: frozenset[tuple[str, str]] | None = None,
        allow_dangling_drop: bool = False,
    ) -> tuple[dict[str, str], frozenset[tuple[str, str]], list[str]]:
        nodes = dict(base_nodes if base_nodes is not None else self._load_catalog_nodes())
        edges = set(base_edges if base_edges is not None else self._load_catalog_edges())
        warnings: list[str] = []

        for node in add_nodes:
            nid = node["id"]
            if not isinstance(nid, str) or not nid:
                raise CatalogError("node id must be a non-empty string")
            nodes[nid] = node.get("name", nid)

        for raw in remove_edges:
            if len(raw) != 2:
                raise CatalogError(f"remove_edges entry must be [child,parent]: {raw!r}")
            edges.discard((str(raw[0]), str(raw[1])))

        for raw in add_edges:
            if len(raw) != 2:
                raise CatalogError(f"add_edges entry must be [child,parent]: {raw!r}")
            child, parent = str(raw[0]), str(raw[1])
            if child not in nodes:
                raise CatalogError(f"unknown catalog node {child!r} in add_edges")
            if parent not in nodes:
                raise CatalogError(f"unknown catalog node {parent!r} in add_edges")
            if child == parent:
                raise CatalogError(f"self edge {child}->{parent} would create a cycle")
            edges.add((child, parent))

        known = set(nodes)
        dangling = {(c, p) for c, p in edges if c not in known or p not in known}
        if dangling:
            if allow_dangling_drop:
                edges.difference_update(dangling)
                warnings.append(
                    "dropped edges referencing retired nodes: "
                    + ", ".join(f"{c}->{p}" for c, p in sorted(dangling))
                )
            else:
                bad = sorted(
                    {c for c, p in dangling if c not in known}
                    | {p for c, p in dangling if p not in known}
                )
                raise CatalogError(f"edges reference unknown nodes: {', '.join(bad)}")

        cycle = catalog.find_cycle(nodes, edges)
        if cycle is not None:
            raise CatalogError("catalog would contain a cycle: " + " -> ".join(cycle))
        return nodes, frozenset(edges), warnings

    # --------------------------------------------------------------- facts
    def _current_ticket_rows(self) -> dict[str, sqlite3.Row]:
        rows = self.conn.execute(
            "SELECT ticket_id, revision, amount, classes, active FROM tickets"
        ).fetchall()
        return {r["ticket_id"]: r for r in rows}

    def _normalise_facts(
        self, raw_updates: Sequence[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for raw in raw_updates:
            tid = raw.get("ticket_id")
            if not isinstance(tid, str) or not tid:
                raise FactError("ticket_id must be a non-empty string")
            revision = raw.get("revision")
            if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
                raise FactError(
                    f"ticket {tid}: revision must be an integer >= 1", ticket_id=tid
                )
            amount = raw.get("amount")
            if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
                raise FactError(
                    f"ticket {tid}: amount must be a non-negative integer", ticket_id=tid
                )
            classes = raw.get("classes", [])
            if (
                not isinstance(classes, (list, tuple))
                or len(classes) == 0
                or any(not isinstance(c, str) or not c for c in classes)
            ):
                raise FactError(
                    f"ticket {tid}: classes must be a non-empty list of class ids",
                    ticket_id=tid,
                )
            fact = {
                "ticket_id": tid,
                "revision": revision,
                "amount": amount,
                "classes": tuple(sorted(set(classes))),
            }
            if tid in merged:
                if merged[tid] != fact:
                    raise FactError(
                        f"ticket {tid}: conflicting updates for the same ticket",
                        ticket_id=tid,
                    )
                # Identical duplicate entries collapse; they are one revision.
                continue
            merged[tid] = fact
            order.append(tid)
        return [merged[tid] for tid in order]

    def _check_fact_against_current(
        self, fact: dict[str, Any], current_rows: dict[str, sqlite3.Row]
    ) -> None:
        tid = fact["ticket_id"]
        row = current_rows.get(tid)
        if row is None:
            return
        cur_rev = int(row["revision"])
        active = int(row["active"]) == 1
        same_content = (
            int(row["amount"]) == fact["amount"]
            and tuple(json.loads(row["classes"])) == fact["classes"]
        )
        if fact["revision"] < cur_rev:
            raise FactError(
                f"ticket {tid}: revision {fact['revision']} is older than current {cur_rev}",
                ticket_id=tid,
            )
        if fact["revision"] == cur_rev:
            if not active:
                raise FactError(
                    f"ticket {tid}: revision {cur_rev} was withdrawn; use a higher revision",
                    ticket_id=tid,
                )
            if not same_content:
                raise FactError(
                    f"ticket {tid}: revision {cur_rev} already committed with different content",
                    ticket_id=tid,
                )
            # Identical replay is allowed and is a no-op.

    def _fact_is_noop(self, fact: dict[str, Any], current_rows: dict[str, sqlite3.Row]) -> bool:
        row = current_rows.get(fact["ticket_id"])
        return (
            row is not None
            and int(row["active"]) == 1
            and int(row["revision"]) == fact["revision"]
            and int(row["amount"]) == fact["amount"]
            and tuple(json.loads(row["classes"])) == fact["classes"]
        )

    def _write_fact(self, fact: dict[str, Any], request_id: str | None, seq: int) -> None:
        self.conn.execute(
            "INSERT INTO tickets(ticket_id, revision, amount, classes, active) "
            "VALUES(?,?,?,?,1) ON CONFLICT(ticket_id) DO UPDATE SET "
            "revision=excluded.revision, amount=excluded.amount, "
            "classes=excluded.classes, active=1",
            (
                fact["ticket_id"],
                fact["revision"],
                fact["amount"],
                _json_dump(list(fact["classes"])),
            ),
        )
        self.conn.execute(
            "INSERT INTO fact_commits"
            "(ticket_id, revision, amount, classes, active, request_id, content_hash, created_seq)"
            " VALUES(?,?,?,?,1,?,?,?)",
            (
                fact["ticket_id"],
                fact["revision"],
                fact["amount"],
                _json_dump(list(fact["classes"])),
                request_id,
                _sha({"kind": "fact", **fact, "classes": list(fact["classes"])}),
                seq,
            ),
        )

    def _next_fact_cut(self, seq: int) -> tuple[Cut, str, int]:
        watermark = self.fact_watermark() + 1
        counter = self._meta_int("fact_counter", 0) + 1
        revision = f"F{counter}"
        self.conn.execute(
            "INSERT INTO fact_watermarks(watermark, seq, revision) VALUES(?,?,?)",
            (watermark, seq, revision),
        )
        self._set_meta("fact_watermark", str(watermark))
        self._set_meta("fact_counter", str(counter))
        self._set_meta("fact_revision", revision)
        cut = Cut(
            catalog_revision=self.catalog_revision() or "G0",
            fact_revision=revision,
            fact_watermark=watermark,
        )
        return cut, revision, watermark

    def _apply_facts_in_tx(
        self,
        facts: Sequence[dict[str, Any]],
        *,
        request_id: str | None,
        seq: int,
        current_rows: dict[str, sqlite3.Row],
    ) -> list[Fact]:
        applied = [f for f in facts if not self._fact_is_noop(f, current_rows)]
        for fact in applied:
            self._write_fact(fact, request_id, seq)
        return [
            Fact(
                ticket_id=f["ticket_id"],
                revision=f["revision"],
                amount=f["amount"],
                classes=f["classes"],
            )
            for f in applied
        ]

    def _load_active_facts_current(self) -> list[Fact]:
        rows = self.conn.execute(
            "SELECT ticket_id, revision, amount, classes FROM tickets "
            "WHERE active=1 ORDER BY ticket_id"
        ).fetchall()
        return [
            Fact(
                ticket_id=r["ticket_id"],
                revision=int(r["revision"]),
                amount=int(r["amount"]),
                classes=tuple(json.loads(r["classes"])),
            )
            for r in rows
        ]

    def _load_facts_at_seq(self, seq: int) -> list[Fact]:
        rows = self.conn.execute(
            "SELECT ticket_id, revision, amount, classes, active FROM fact_commits "
            "WHERE created_seq <= ? ORDER BY commit_id",
            (seq,),
        ).fetchall()
        latest: dict[str, sqlite3.Row] = {}
        for row in rows:
            latest[row["ticket_id"]] = row
        return [
            Fact(
                ticket_id=r["ticket_id"],
                revision=int(r["revision"]),
                amount=int(r["amount"]),
                classes=tuple(json.loads(r["classes"])),
            )
            for r in latest.values()
            if int(r["active"]) == 1
        ]

    def _watermark_seq(self, watermark: int) -> tuple[int, str] | None:
        row = self.conn.execute(
            "SELECT seq, revision FROM fact_watermarks WHERE watermark=?", (watermark,)
        ).fetchone()
        if row is None:
            return None
        return int(row["seq"]), row["revision"]

    # --------------------------------------------------------------- submit
    def submit(
        self, operation: dict[str, Any], *, request_id: str | None = None
    ) -> Committed:
        kind, payload = ingest_operation(operation)
        request_id = request_id or operation.get("request_id")
        if not request_id:
            request_id = "auto-" + _sha({"kind": kind, "payload": payload})[:16]
        if kind == "bundle":
            return self.apply_bundle(payload, request_id=request_id)
        if kind == "catalog":
            return self._apply_catalog_payload(payload, request_id=request_id)
        return self._apply_fact_payload(payload, request_id=request_id)

    def _replay(self, event: sqlite3.Row) -> Committed:
        details = json.loads(event["details"]) if event["details"] else {}
        kind = event["kind"]
        if kind in {"bundle", "catalog", "init"}:
            cut = Cut(
                catalog_revision=details.get(
                    "catalog_revision", self.catalog_revision() or "G0"
                ),
                fact_revision=details.get(
                    "fact_revision", self.fact_revision()
                ),
                fact_watermark=int(details.get("fact_watermark", self.fact_watermark())),
            )
            parent = details.get("parent_catalog_revision", details.get("parent_revision"))
            facts = [
                Fact(
                    ticket_id=f["ticket_id"],
                    revision=int(f["revision"]),
                    amount=int(f["amount"]),
                    classes=tuple(f.get("classes", [])),
                )
                for f in details.get("facts", [])
            ]
            return Committed(
                request_id=event["request_id"],
                cut=cut,
                catalog_revision=cut.catalog_revision,
                parent_catalog_revision=parent,
                edge_delta={
                    "add": details.get("added_edges", details.get("add_edges", [])),
                    "remove": details.get(
                        "removed_edges", details.get("remove_edges", [])
                    ),
                },
                facts=facts,
                warnings=details.get("warnings", []),
                noop=bool(details.get("noop", False)),
            )
        if kind == "facts":
            cut = Cut(
                catalog_revision=self.catalog_revision() or "G0",
                fact_revision=details.get("fact_revision", self.fact_revision()),
                fact_watermark=int(details.get("fact_watermark", self.fact_watermark())),
            )
            facts = [
                Fact(
                    ticket_id=f["ticket_id"],
                    revision=int(f["revision"]),
                    amount=int(f["amount"]),
                    classes=tuple(f.get("classes", [])),
                )
                for f in details.get("facts", [])
            ]
            return Committed(
                request_id=event["request_id"],
                cut=cut,
                catalog_revision=cut.catalog_revision,
                parent_catalog_revision=cut.catalog_revision,
                edge_delta={"add": [], "remove": []},
                facts=facts,
                warnings=details.get("warnings", []),
                noop=bool(details.get("noop", False)),
            )
        raise StoreError(f"cannot replay event kind {kind!r}")

    def _apply_fact_payload(
        self, fact: dict[str, Any], *, request_id: str
    ) -> Committed:
        payload_hash = _sha({"kind": "fact", "fact": fact})
        existing = self._event_for(request_id)
        if existing is not None:
            details = json.loads(existing["details"]) if existing["details"] else {}
            if details.get("payload_hash") not in (None, payload_hash):
                raise ConflictError(
                    f"request_id {request_id!r} already committed with different content"
                )
            return self._replay(existing)
        facts = self._normalise_facts([fact])
        with self._write_lock, _Transaction(self.conn):
            seq = self._next_seq()
            current_rows = self._current_ticket_rows()
            for item in facts:
                self._check_fact_against_current(item, current_rows)
            noop = all(self._fact_is_noop(f, current_rows) for f in facts)
            cut = self.current_cut()
            applied: list[Fact] = []
            if not noop:
                applied = self._apply_facts_in_tx(
                    facts,
                    request_id=request_id,
                    seq=seq,
                    current_rows=current_rows,
                )
                cut, _, _ = self._next_fact_cut(seq)
            self._record_event(
                seq,
                request_id,
                "facts",
                {
                    "payload_hash": payload_hash,
                    "fact_revision": cut.fact_revision,
                    "fact_watermark": cut.fact_watermark,
                    "catalog_revision": cut.catalog_revision,
                    "facts": [f.as_dict() for f in applied],
                    "noop": noop,
                    "warnings": [] if not noop else ["identical revision; no-op"],
                },
            )
        return Committed(
            request_id=request_id,
            cut=cut,
            catalog_revision=cut.catalog_revision,
            parent_catalog_revision=cut.catalog_revision,
            edge_delta={"add": [], "remove": []},
            facts=applied,
            warnings=[] if not noop else ["identical revision; no-op"],
            noop=noop,
        )

    def apply_fact_revisions(
        self,
        facts: Sequence[dict[str, Any]],
        *,
        request_id: str | None = None,
    ) -> Committed:
        """Apply several fact revisions in one fact-revision commit.

        This is *not* a bundle: the catalog does not change, but all supplied
        fact revisions share one fact watermark and are committed atomically.
        """

        request_id = request_id or "auto-" + _sha(
            {"kind": "facts-batch", "facts": [dict(f) for f in facts]}
        )[:16]
        payload_hash = _sha({"kind": "facts-batch", "facts": [dict(f) for f in facts]})
        existing = self._event_for(request_id)
        if existing is not None:
            details = json.loads(existing["details"]) if existing["details"] else {}
            if details.get("payload_hash") not in (None, payload_hash):
                raise ConflictError(
                    f"request_id {request_id!r} already committed with different content"
                )
            return self._replay(existing)
        normalized = self._normalise_facts(facts)
        with self._write_lock, _Transaction(self.conn):
            seq = self._next_seq()
            current_rows = self._current_ticket_rows()
            for fact in normalized:
                self._check_fact_against_current(fact, current_rows)
            applied = self._apply_facts_in_tx(
                normalized, request_id=request_id, seq=seq, current_rows=current_rows
            )
            noop = not applied
            if applied:
                cut, _, _ = self._next_fact_cut(seq)
            else:
                cut = self.current_cut()
            self._record_event(
                seq,
                request_id,
                "facts",
                {
                    "payload_hash": payload_hash,
                    "fact_revision": cut.fact_revision,
                    "fact_watermark": cut.fact_watermark,
                    "catalog_revision": cut.catalog_revision,
                    "facts": [f.as_dict() for f in applied],
                    "noop": noop,
                    "warnings": ["identical revisions; no-op"] if noop else [],
                },
            )
        return Committed(
            request_id=request_id,
            cut=cut,
            catalog_revision=cut.catalog_revision,
            parent_catalog_revision=cut.catalog_revision,
            edge_delta={"add": [], "remove": []},
            facts=applied,
            warnings=["identical revisions; no-op"] if noop else [],
            noop=noop,
        )

    def _apply_catalog_payload(
        self, payload: dict[str, Any], *, request_id: str
    ) -> Committed:
        payload_hash = _sha({"kind": "catalog", "payload": payload})
        existing = self._event_for(request_id)
        if existing is not None:
            details = json.loads(existing["details"]) if existing["details"] else {}
            if details.get("payload_hash") not in (None, payload_hash):
                raise ConflictError(
                    f"request_id {request_id!r} already committed with different content"
                )
            return self._replay(existing)
        with self._write_lock, _Transaction(self.conn):
            seq = self._next_seq()
            delta = self._apply_catalog_change_in_tx(
                add_nodes=payload.get("nodes", []),
                add_edges=payload.get("add_edges", []),
                remove_edges=payload.get("remove_edges", []),
                request_id=request_id,
                seq=seq,
                parent=self.catalog_revision(),
                payload_hash=payload_hash,
            )
            cut = self.current_cut()
        return Committed(
            request_id=request_id,
            cut=cut,
            catalog_revision=delta["catalog_revision"],
            parent_catalog_revision=delta["parent_catalog_revision"],
            edge_delta={"add": delta["added_edges"], "remove": delta["removed_edges"]},
            warnings=delta["warnings"],
            noop=delta["noop"],
        )

    def _apply_catalog_change_in_tx(
        self,
        *,
        add_nodes: Sequence[dict[str, str]],
        add_edges: Sequence[Sequence[str]],
        remove_edges: Sequence[Sequence[str]],
        request_id: str | None,
        seq: int,
        parent: str | None,
        payload_hash: str | None = None,
    ) -> dict[str, Any]:
        """Project/validate the batch, then commit it if it really changes.

        The full projected edge set is checked for cycles even for a no-op
        request, so an illegal batch is rejected rather than silently ignored.
        """

        base_nodes = self._load_catalog_nodes()
        base_edges = self._load_catalog_edges()
        nodes, edges, warnings = self._project_catalog(
            add_nodes=add_nodes,
            add_edges=add_edges,
            remove_edges=remove_edges,
            base_nodes=base_nodes,
            base_edges=base_edges,
            allow_dangling_drop=bool(base_nodes),
        )
        added = sorted(edges - base_edges)
        removed = sorted(base_edges - edges)
        added_nodes = sorted(set(nodes) - set(base_nodes))
        changed = bool(added or removed or added_nodes)
        requested = bool(add_nodes or add_edges or remove_edges)

        if not changed:
            warnings = warnings + (
                ["request is a no-op; nothing to publish"] if requested else []
            )
            self._record_event(
                seq,
                request_id,
                "catalog",
                {
                    "payload_hash": payload_hash,
                    "catalog_revision": parent,
                    "fact_revision": self.fact_revision(),
                    "fact_watermark": self.fact_watermark(),
                    "added_edges": [],
                    "removed_edges": [],
                    "warnings": warnings,
                    "noop": True,
                },
            )
            return {
                "catalog_revision": parent or "G0",
                "parent_catalog_revision": parent,
                "added_edges": [],
                "removed_edges": [],
                "warnings": warnings,
                "noop": True,
            }

        counter = self._meta_int("catalog_counter", 0) + 1
        revision = f"G{counter}"
        explanation = {
            "operation": "catalog",
            "request_id": request_id,
            "catalog_revision": revision,
            "parent_revision": parent,
            "added_nodes": [{"id": nid, "name": nodes[nid]} for nid in added_nodes],
            "add_edges": [list(e) for e in added],
            "remove_edges": [list(e) for e in removed],
            "warnings": warnings,
            "payload_hash": payload_hash,
        }
        self._write_catalog_snapshot(
            revision=revision,
            parent=parent,
            request_id=request_id,
            seq=seq,
            nodes=nodes,
            edges=edges,
            explanation=explanation,
        )
        self._record_event(seq, request_id, "catalog", explanation)
        return {
            "catalog_revision": revision,
            "parent_catalog_revision": parent,
            "added_edges": [list(e) for e in added],
            "removed_edges": [list(e) for e in removed],
            "warnings": warnings,
            "noop": False,
        }

    def add_nodes(
        self, nodes: Sequence[dict[str, str]], *, request_id: str | None = None
    ) -> Committed:
        """Re-open retired node ids (no edges) as a standalone catalog batch."""

        return self.submit(
            {"operation": "catalog", "nodes": list(nodes)},
            request_id=request_id,
        )

    def apply_fact_revision(
        self,
        ticket_id: str,
        revision: int,
        amount: int,
        classes: Sequence[str],
        *,
        request_id: str | None = None,
    ) -> Committed:
        return self.submit(
            {
                "operation": "fact",
                "ticket_id": ticket_id,
                "revision": revision,
                "amount": amount,
                "classes": list(classes),
            },
            request_id=request_id,
        )

    # --------------------------------------------------------------- bundles
    def apply_bundle(
        self, payload: dict[str, Any], *, request_id: str
    ) -> Committed:
        """Atomically apply catalog edge changes together with fact revisions.

        Catalog and fact validation both run against projected post-bundle
        state.  A cycle, an illegal class, or a revision conflict raises
        before any row is written, and the transaction rolls back, so the
        whole bundle has no effect.
        """

        payload_hash = _sha({"kind": "bundle", "payload": payload})
        existing = self._event_for(request_id)
        if existing is not None:
            details = json.loads(existing["details"]) if existing["details"] else {}
            if details.get("payload_hash") not in (None, payload_hash):
                raise ConflictError(
                    f"request_id {request_id!r} already committed with different content"
                )
            return self._replay(existing)

        add_edges = payload.get("add_edges", [])
        remove_edges = payload.get("remove_edges", [])
        raw_facts = payload.get("fact_revisions", [])

        with self._write_lock, _Transaction(self.conn):
            seq = self._next_seq()
            base_nodes = self._load_catalog_nodes()
            base_edges = self._load_catalog_edges()
            projected_nodes, projected_edges, warnings = self._project_catalog(
                add_nodes=[],
                add_edges=add_edges,
                remove_edges=remove_edges,
                base_nodes=base_nodes,
                base_edges=base_edges,
                allow_dangling_drop=bool(base_nodes),
            )
            facts = self._normalise_facts(raw_facts)
            current_rows = self._current_ticket_rows()
            for fact in facts:
                self._check_fact_against_current(fact, current_rows)
                for cls in fact["classes"]:
                    if cls not in projected_nodes:
                        raise FactError(
                            f"ticket {fact['ticket_id']}: unknown class {cls!r} "
                            f"under the projected catalog",
                            ticket_id=fact["ticket_id"],
                        )

            added = sorted(projected_edges - base_edges)
            removed = sorted(base_edges - projected_edges)
            catalog_changed = bool(added or removed)
            facts_to_apply = [f for f in facts if not self._fact_is_noop(f, current_rows)]
            fact_changed = bool(facts_to_apply)

            if not catalog_changed and not fact_changed:
                cut = self.current_cut()
                details = {
                    "payload_hash": payload_hash,
                    "catalog_revision": cut.catalog_revision,
                    "fact_revision": cut.fact_revision,
                    "fact_watermark": cut.fact_watermark,
                    "catalog_changed": False,
                    "added_edges": [],
                    "removed_edges": [],
                    "facts": [],
                    "warnings": ["bundle is a no-op; no new revisions were created"],
                    "noop": True,
                }
                self._record_event(seq, request_id, "bundle", details)
                return Committed(
                    request_id=request_id,
                    cut=cut,
                    catalog_revision=cut.catalog_revision,
                    parent_catalog_revision=cut.catalog_revision,
                    edge_delta={"add": [], "remove": []},
                    warnings=details["warnings"],
                    noop=True,
                )

            parent_catalog = self.catalog_revision()
            catalog_revision = parent_catalog or "G0"
            if catalog_changed:
                counter = self._meta_int("catalog_counter", 0) + 1
                catalog_revision = f"G{counter}"
                explanation = {
                    "operation": "bundle",
                    "request_id": request_id,
                    "catalog_revision": catalog_revision,
                    "parent_revision": parent_catalog,
                    "add_edges": [list(e) for e in added],
                    "remove_edges": [list(e) for e in removed],
                    "warnings": warnings,
                    "payload_hash": payload_hash,
                }
                self._write_catalog_snapshot(
                    revision=catalog_revision,
                    parent=parent_catalog,
                    request_id=request_id,
                    seq=seq,
                    nodes=projected_nodes,
                    edges=projected_edges,
                    explanation=explanation,
                )
                self._record_event(seq, request_id, "catalog", explanation)

            applied = self._apply_facts_in_tx(
                facts, request_id=request_id, seq=seq, current_rows=current_rows
            )
            if applied:
                cut, _, _ = self._next_fact_cut(seq)
            else:
                cut = Cut(
                    catalog_revision=catalog_revision,
                    fact_revision=self.fact_revision(),
                    fact_watermark=self.fact_watermark(),
                )
            details = {
                "payload_hash": payload_hash,
                "catalog_revision": catalog_revision,
                "parent_catalog_revision": parent_catalog,
                "fact_revision": cut.fact_revision,
                "fact_watermark": cut.fact_watermark,
                "catalog_changed": catalog_changed,
                "added_edges": [list(e) for e in added],
                "removed_edges": [list(e) for e in removed],
                "facts": [f.as_dict() for f in applied],
                "warnings": warnings,
                "noop": False,
            }
            self._record_event(seq, request_id, "bundle", details)

        return Committed(
            request_id=request_id,
            cut=cut,
            catalog_revision=catalog_revision,
            parent_catalog_revision=parent_catalog,
            edge_delta={"add": [list(e) for e in added], "remove": [list(e) for e in removed]},
            facts=applied,
            warnings=warnings,
        )

    # -------------------------------------------------------------- withdraw
    def withdraw(
        self,
        ticket_id: str,
        expected_revision: int,
        *,
        request_id: str | None = None,
    ) -> Committed:
        request_id = request_id or f"withdraw-{ticket_id}-{expected_revision}"
        existing = self._event_for(request_id)
        if existing is not None:
            return self._replay(existing)
        with self._write_lock, _Transaction(self.conn):
            seq = self._next_seq()
            row = self.conn.execute(
                "SELECT revision, amount, classes, active FROM tickets WHERE ticket_id=?",
                (ticket_id,),
            ).fetchone()
            if row is None:
                raise FactError(f"ticket {ticket_id} does not exist", ticket_id=ticket_id)
            if int(row["revision"]) != expected_revision:
                raise FactError(
                    f"ticket {ticket_id}: expected revision {expected_revision}, "
                    f"current is {row['revision']}",
                    ticket_id=ticket_id,
                )
            if int(row["active"]) == 0:
                cut = self.current_cut()
                self._record_event(
                    seq,
                    request_id,
                    "facts",
                    {
                        "fact_revision": cut.fact_revision,
                        "fact_watermark": cut.fact_watermark,
                        "catalog_revision": cut.catalog_revision,
                        "facts": [],
                        "noop": True,
                        "warnings": ["ticket already withdrawn; no-op"],
                    },
                )
                return Committed(
                    request_id=request_id,
                    cut=cut,
                    catalog_revision=cut.catalog_revision,
                    parent_catalog_revision=cut.catalog_revision,
                    edge_delta={"add": [], "remove": []},
                    warnings=["ticket already withdrawn; no-op"],
                    noop=True,
                )
            self.conn.execute(
                "UPDATE tickets SET active=0 WHERE ticket_id=?", (ticket_id,)
            )
            self.conn.execute(
                "INSERT INTO fact_commits"
                "(ticket_id, revision, amount, classes, active, request_id, content_hash, created_seq)"
                " VALUES(?,?,?,?,0,?,?,?)",
                (
                    ticket_id,
                    expected_revision,
                    int(row["amount"]),
                    row["classes"],
                    request_id,
                    _sha({"kind": "withdraw", "ticket_id": ticket_id, "revision": expected_revision}),
                    seq,
                ),
            )
            cut, _, _ = self._next_fact_cut(seq)
            self._record_event(
                seq,
                request_id,
                "facts",
                {
                    "fact_revision": cut.fact_revision,
                    "fact_watermark": cut.fact_watermark,
                    "catalog_revision": cut.catalog_revision,
                    "facts": [],
                    "noop": False,
                    "warnings": [],
                    "withdrawn": ticket_id,
                    "withdrawn_revision": expected_revision,
                },
            )
        return Committed(
            request_id=request_id,
            cut=cut,
            catalog_revision=cut.catalog_revision,
            parent_catalog_revision=cut.catalog_revision,
            edge_delta={"add": [], "remove": []},
            warnings=[],
        )

    # --------------------------------------------------------------- queries
    def query(
        self,
        targets: Sequence[str],
        *,
        snapshot: Cut | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Resolve all targets, the union, and one cut together.

        Every target id is resolved against the same catalog revision and the
        same fact watermark; contributions from several targets are merged by
        ticket id for the union.  A target may be a parent and a child at the
        same time; the union still counts each ticket once.
        """

        target_list = [str(t) for t in targets]
        if not target_list:
            raise StoreError("at least one target is required")
        seen: set[str] = set()
        for target in target_list:
            if target in seen:
                raise StoreError(f"duplicate target {target!r} in one query")
            seen.add(target)

        with self._write_lock, _ReadTransaction(self.conn):
            facts: list[Fact]
            if snapshot is not None:
                state = self.load_catalog_state(snapshot.catalog_revision)
                if snapshot.fact_watermark == 0:
                    # Initial state: no fact commit has been made yet.
                    if snapshot.fact_revision not in ("F0",):
                        raise ConflictError(
                            "snapshot fact revision does not match its watermark"
                        )
                    facts = []
                else:
                    resolved = self._watermark_seq(snapshot.fact_watermark)
                    if resolved is None:
                        raise ConflictError(
                            f"unknown fact watermark {snapshot.fact_watermark}"
                        )
                    seq, revision = resolved
                    if revision != snapshot.fact_revision:
                        raise ConflictError(
                            "snapshot fact revision does not match its watermark"
                        )
                    facts = self._load_facts_at_seq(seq)
                cut = snapshot
            else:
                catalog_rev = self.catalog_revision() or "G0"
                state = self.load_catalog_state(catalog_rev)
                facts = self._load_active_facts_current()
                cut = Cut(
                    catalog_revision=catalog_rev,
                    fact_revision=self.fact_revision(),
                    fact_watermark=self.fact_watermark(),
                )
            for target in target_list:
                if target not in state.nodes:
                    raise CatalogError(f"unknown target {target!r}")

            execution = self._execute(state, facts, target_list)
            execution["cut"] = cut.as_dict()
            execution["name"] = name
            return execution

    def _execute(
        self, state: CatalogState, facts: Sequence[Fact], targets: Sequence[str]
    ) -> dict[str, Any]:
        edges = state.edges
        groups: list[dict[str, Any]] = []
        union_members: dict[str, Fact] = {}
        for target in targets:
            dist, next_hop = catalog.distances_to_target(target, edges)
            contributions = []
            for fact in facts:
                path = catalog.choose_path(fact.classes, target, dist, next_hop)
                if path is None:
                    continue
                contributions.append(
                    {
                        "ticket_id": fact.ticket_id,
                        "amount": fact.amount,
                        "path": path,
                    }
                )
                union_members.setdefault(fact.ticket_id, fact)
            contributions.sort(key=lambda c: c["ticket_id"])
            groups.append(
                {
                    "target_id": target,
                    "count": len(contributions),
                    "amount": sum(c["amount"] for c in contributions),
                    "contributions": contributions,
                }
            )
        union_contributions = [
            {"ticket_id": tid, "amount": union_members[tid].amount}
            for tid in sorted(union_members)
        ]
        return {
            "format": "classification-summary/1",
            "groups": groups,
            "union": {
                "count": len(union_contributions),
                "amount": sum(c["amount"] for c in union_contributions),
                "contributions": union_contributions,
            },
        }

    def explain_target(
        self,
        target: str,
        classes: Sequence[str],
        *,
        snapshot: Cut | None = None,
    ) -> dict[str, Any]:
        """Explain coverage of direct classes by one summary target."""

        with self._write_lock, _ReadTransaction(self.conn):
            state = self.load_catalog_state(
                snapshot.catalog_revision if snapshot else None
            )
            if target not in state.nodes:
                raise CatalogError(f"unknown target {target!r}")
            result = catalog.explain_coverage(target, classes, state.edges)
            result["cut"] = (
                snapshot.as_dict()
                if snapshot
                else self.current_cut().as_dict()
            )
            return result

    def ancestors(self, node_id: str) -> list[str]:
        state = self.load_catalog_state()
        if node_id not in state.nodes:
            raise CatalogError(f"unknown node {node_id!r}")
        return sorted(catalog.ancestors(node_id, state.edges))

    def descendants(self, node_id: str) -> list[str]:
        state = self.load_catalog_state()
        if node_id not in state.nodes:
            raise CatalogError(f"unknown node {node_id!r}")
        return sorted(catalog.descendants(node_id, state.edges))

    # -------------------------------------------------------------- products
    def build_product(
        self,
        targets: Sequence[str],
        *,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Build a classification-summary/1 payload from one query execution."""

        execution = self.query(targets, name=name)
        payload: dict[str, Any] = {
            "format": "classification-summary/1",
            "cut": execution["cut"],
            "groups": execution["groups"],
            "union": execution["union"],
        }
        if name is not None:
            payload["name"] = name
        return payload

    def publish(
        self,
        targets: Sequence[str],
        *,
        name: str | None = None,
        product_id: str | None = None,
        created_at: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist an immutable product (querying first if no payload given)."""

        if payload is None:
            payload = self.build_product(targets, name=name)
        product_id = product_id or "P-" + _sha(payload)[:16]
        created_at = created_at or _utc_now()
        with self._write_lock, _Transaction(self.conn):
            existing = self.conn.execute(
                "SELECT payload FROM products WHERE product_id=?", (product_id,)
            ).fetchone()
            if existing is not None:
                if json.loads(existing["payload"]) != payload:
                    raise ProductError(
                        f"product {product_id!r} already exists with different content"
                    )
            else:
                self.conn.execute(
                    "INSERT INTO products"
                    "(product_id, name, catalog_revision, fact_revision, created_at, payload)"
                    " VALUES(?,?,?,?,?,?)",
                    (
                        product_id,
                        name,
                        payload["cut"]["catalog_revision"],
                        payload["cut"]["fact_revision"],
                        created_at,
                        _json_dump(payload),
                    ),
                )
        return {"product_id": product_id, "created_at": created_at, "payload": payload}

    def get_product(self, product_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM products WHERE product_id=?", (product_id,)
        ).fetchone()
        if row is None:
            raise ProductError(f"unknown product {product_id!r}")
        return {
            "product_id": row["product_id"],
            "name": row["name"],
            "created_at": row["created_at"],
            "payload": json.loads(row["payload"]),
        }

    def list_products(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT product_id, name, catalog_revision, fact_revision, created_at "
            "FROM products ORDER BY created_at, product_id"
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------- reporting
    def history(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM events ORDER BY seq, event_id"
        ).fetchall()
        return [
            {
                "event_id": int(row["event_id"]),
                "seq": int(row["seq"]),
                "request_id": row["request_id"],
                "kind": row["kind"],
                "details": json.loads(row["details"]) if row["details"] else {},
            }
            for row in rows
        ]
