"""SQLite settlement ledger.

Facts are append-only:

* ``clock_revision`` / ``clock_zone`` — every catalogue version ever installed,
  stored as a full snapshot.  Old evaluations keep pointing at the revision
  they were computed under.
* ``session`` — the stable wall-clock input of one session.
* ``evaluation`` — one settlement outcome per ``evaluation_id``.  Successful
  rows carry real UTC bounds, duration, rate and cost; rejected rows carry
  ``NULL`` for all of those so they can never contribute to pay.
* ``event`` — an audit trail of operations (including rejected evaluations).

Rejected evaluations are recorded as outcomes (so an ``evaluation_id`` is
idempotent for both success and rejection) but are explicitly excluded from
every aggregate.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from .clock import ClockCatalogue, ClockError, ClockStore, InvalidCatalogue
from .evaluate import (
    EvaluationRejected,
    SessionInput,
    evaluate_session,
    resolve_local_point,
)


class LedgerError(Exception):
    """Base class for ledger errors."""


class ConflictingSession(LedgerError):
    """A session_id was re-registered with different content."""


class ConflictingEvaluation(LedgerError):
    """An evaluation_id was reused with different content."""


class UnknownSession(LedgerError):
    """No session with the requested id exists."""


class UnknownEvaluation(LedgerError):
    """No evaluation with the requested id exists."""


class ImmutableRevisionConflict(LedgerError):
    """A revision name already exists with different content."""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clock_revision (
    revision      TEXT PRIMARY KEY,
    revision_seq  INTEGER NOT NULL UNIQUE,
    document_json TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS clock_zone (
    revision   TEXT NOT NULL REFERENCES clock_revision(revision),
    zone_id    TEXT NOT NULL,
    domain_start INTEGER NOT NULL,
    domain_end   INTEGER NOT NULL,
    segments_json TEXT NOT NULL,
    PRIMARY KEY (revision, zone_id)
);

CREATE TABLE IF NOT EXISTS session (
    session_id      TEXT PRIMARY KEY,
    session_seq     INTEGER NOT NULL UNIQUE,
    definition_json TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS evaluation (
    evaluation_id  TEXT PRIMARY KEY,
    evaluation_seq INTEGER NOT NULL UNIQUE,
    session_id     TEXT NOT NULL REFERENCES session(session_id),
    revision       TEXT NOT NULL REFERENCES clock_revision(revision),
    zone_id        TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('ok','rejected')),
    content_hash   TEXT NOT NULL,
    outcome_json   TEXT NOT NULL,
    utc_start      INTEGER,
    utc_end        INTEGER,
    duration_minutes INTEGER,
    rate_per_utc_minute INTEGER,
    cost           INTEGER,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    CHECK (
        (status = 'ok' AND utc_start IS NOT NULL AND utc_end IS NOT NULL
         AND duration_minutes IS NOT NULL AND rate_per_utc_minute IS NOT NULL
         AND cost IS NOT NULL AND utc_end > utc_start)
        OR
        (status = 'rejected' AND utc_start IS NULL AND utc_end IS NULL
         AND duration_minutes IS NULL AND rate_per_utc_minute IS NULL
         AND cost IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS evaluation_session
    ON evaluation(session_id, evaluation_seq);
CREATE INDEX IF NOT EXISTS evaluation_interval
    ON evaluation(utc_start, utc_end) WHERE status = 'ok';

CREATE TABLE IF NOT EXISTS event (
    event_seq  INTEGER PRIMARY KEY AUTOINCREMENT,
    op         TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
"""


class Ledger:
    """A SQLite-backed clock catalogue plus settlement facts."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.conn = connection
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---------------------------------------------------------------- setup
    @classmethod
    def open(cls, path: str | Path) -> "Ledger":
        conn = sqlite3.connect(str(path), isolation_level=None)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        return cls(conn)

    @classmethod
    def memory(cls) -> "Ledger":
        return cls(sqlite3.connect(":memory:", isolation_level=None))

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """A write transaction that either fully commits or fully rolls back."""

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    def _next_seq(self, table: str, column: str) -> int:
        row = self.conn.execute(
            f"SELECT COALESCE(MAX({column}), 0) + 1 AS next FROM {table}"
        ).fetchone()
        return int(row["next"])

    def _event(self, op: str, detail: Mapping[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO event (op, detail_json) VALUES (?, ?)",
            (op, canonical_json(detail)),
        )

    def log_event(self, op: str, detail: Mapping[str, Any]) -> None:
        """Append one audit event in its own transaction."""

        with self.transaction():
            self._event(op, detail)

    # ------------------------------------------------------------- catalogue
    def install_catalogue(self, catalogue: ClockCatalogue) -> ClockCatalogue:
        """Store a validated catalogue under its revision name, atomically.

        Installing the same revision twice with identical content is a no-op;
        different content is a conflict.
        """

        with self.transaction():
            return self._install_catalogue(catalogue, op="install_catalogue")

    def _install_catalogue(
        self, catalogue: ClockCatalogue, op: str
    ) -> ClockCatalogue:
        document = catalogue.to_json()
        document_json = canonical_json(document)
        row = self.conn.execute(
            "SELECT document_json FROM clock_revision WHERE revision = ?",
            (catalogue.revision,),
        ).fetchone()
        if row is not None:
            if row["document_json"] != document_json:
                raise ImmutableRevisionConflict(
                    f"revision {catalogue.revision!r} already exists with "
                    f"different content"
                )
            self._event(
                op,
                {"revision": catalogue.revision, "status": "replayed"},
            )
            return catalogue
        seq = self._next_seq("clock_revision", "revision_seq")
        self.conn.execute(
            "INSERT INTO clock_revision (revision, revision_seq, document_json)"
            " VALUES (?, ?, ?)",
            (catalogue.revision, seq, document_json),
        )
        for zone in catalogue.zones.values():
            self.conn.execute(
                "INSERT INTO clock_zone (revision, zone_id, domain_start,"
                " domain_end, segments_json) VALUES (?, ?, ?, ?, ?)",
                (
                    catalogue.revision,
                    zone.zone_id,
                    zone.utc_start,
                    zone.utc_end,
                    canonical_json([seg.to_json() for seg in zone.segments]),
                ),
            )
        self._event(
            op,
            {"revision": catalogue.revision, "status": "created"},
        )
        return catalogue

    def load_store(self) -> ClockStore:
        rows = self.conn.execute(
            "SELECT document_json FROM clock_revision ORDER BY revision_seq"
        ).fetchall()
        if not rows:
            raise LedgerError("ledger has no clock revision yet")
        catalogues = [
            ClockCatalogue.from_document(json.loads(row["document_json"]))
            for row in rows
        ]
        return ClockStore(catalogues=catalogues, active=catalogues[-1].revision)

    def active_revision(self) -> str:
        row = self.conn.execute(
            "SELECT revision FROM clock_revision ORDER BY revision_seq DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise LedgerError("ledger has no clock revision yet")
        return row["revision"]

    def catalogue(self, revision: str | None = None) -> ClockCatalogue:
        name = revision or self.active_revision()
        row = self.conn.execute(
            "SELECT document_json FROM clock_revision WHERE revision = ?", (name,)
        ).fetchone()
        if row is None:
            raise ClockError(f"unknown revision {name!r}")
        return ClockCatalogue.from_document(json.loads(row["document_json"]))

    def list_revisions(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT revision, revision_seq, document_json, created_at"
            " FROM clock_revision ORDER BY revision_seq"
        ).fetchall()
        active = self.active_revision()
        return [
            {
                "revision": row["revision"],
                "active": row["revision"] == active,
                "document": json.loads(row["document_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def apply_revision(self, document: Mapping[str, Any]) -> dict[str, Any]:
        """Validate and install a new revision as one all-or-nothing batch.

        Validation happens against the current active catalogue.  If anything
        fails, no revision row, zone row or event row is written and the
        active revision is unchanged.
        """

        store = self.load_store()
        before = store.active.revision
        try:
            candidate = store.preview_revision(document)
        except (InvalidCatalogue, ClockError) as exc:
            self._append_rejected_revision(document, str(exc))
            raise
        with self.transaction():
            self._install_catalogue(candidate, op="apply_clock_revision")
            self._event(
                "apply_clock_revision",
                {
                    "previous": before,
                    "revision": candidate.revision,
                    "status": "activated",
                    "zones": {
                        zone_id: len(zone)
                        for zone_id, zone in candidate.zones.items()
                    },
                },
            )
        return {
            "status": "installed",
            "previous": before,
            "revision": candidate.revision,
            "zones": {
                zone_id: zone.to_json()
                for zone_id, zone in candidate.zones.items()
            },
        }

    def _append_rejected_revision(
        self, document: Mapping[str, Any], message: str
    ) -> None:
        # An invalid revision is an observation, not a catalogue version.
        # Recording it must not change the active revision.
        with self.transaction():
            self._event(
                "apply_clock_revision",
                {
                    "status": "rejected",
                    "revision": document.get("revision")
                    if isinstance(document, Mapping)
                    else None,
                    "message": message,
                    "active": self.active_revision(),
                },
            )

    # ---------------------------------------------------------------- point
    def resolve_point(
        self,
        local: int,
        zone_id: str,
        revision: str | None = None,
        occurrence: str | None = None,
    ) -> dict[str, Any]:
        """Session-independent clock query against a stored revision."""

        catalogue = self.catalogue(revision)
        return resolve_local_point(catalogue, zone_id, local, occurrence)

    # --------------------------------------------------------------- session
    def register_session(self, raw: Mapping[str, Any]) -> SessionInput:
        session = SessionInput.from_mapping(raw)
        definition = session.to_json()
        encoded = canonical_json(definition)
        with self.transaction():
            row = self.conn.execute(
                "SELECT definition_json FROM session WHERE session_id = ?",
                (session.session_id,),
            ).fetchone()
            if row is not None:
                if row["definition_json"] != encoded:
                    raise ConflictingSession(
                        f"session {session.session_id!r} already exists with a "
                        f"different definition"
                    )
                return session
            seq = self._next_seq("session", "session_seq")
            self.conn.execute(
                "INSERT INTO session (session_id, session_seq, definition_json)"
                " VALUES (?, ?, ?)",
                (session.session_id, seq, encoded),
            )
            self._event(
                "register_session",
                {"session_id": session.session_id, "definition": definition},
            )
        return session

    def _session_definition(self, session_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT definition_json FROM session WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise UnknownSession(f"unknown session {session_id!r}")
        return json.loads(row["definition_json"])

    def list_sessions(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT session_id, definition_json FROM session"
            " ORDER BY session_seq"
        ).fetchall()
        out = []
        for row in rows:
            evaluations = self.conn.execute(
                "SELECT evaluation_id, revision, status, duration_minutes, cost"
                " FROM evaluation WHERE session_id = ?"
                " ORDER BY evaluation_seq",
                (row["session_id"],),
            ).fetchall()
            out.append(
                {
                    "session_id": row["session_id"],
                    "definition": json.loads(row["definition_json"]),
                    "evaluations": [dict(e) for e in evaluations],
                }
            )
        return out

    def get_session(self, session_id: str) -> dict[str, Any]:
        definition = self._session_definition(session_id)
        rows = self.conn.execute(
            "SELECT evaluation_id, revision, status, content_hash, outcome_json"
            " FROM evaluation WHERE session_id = ? ORDER BY evaluation_seq",
            (session_id,),
        ).fetchall()
        return {
            "session_id": session_id,
            "definition": definition,
            "evaluations": [
                {
                    "evaluation_id": row["evaluation_id"],
                    "revision": row["revision"],
                    "status": row["status"],
                    "outcome": json.loads(row["outcome_json"]),
                }
                for row in rows
            ],
        }

    # ------------------------------------------------------------ evaluation
    def _hash_outcome(
        self, session: SessionInput, revision: str, outcome: Mapping[str, Any]
    ) -> str:
        observed = {key: value for key, value in outcome.items() if key != "evaluation_id"}
        return content_hash(
            {
                "session": session.to_json(),
                "revision": revision,
                "outcome": observed,
            }
        )

    def evaluate(
        self, session_id: str, evaluation_id: str
    ) -> dict[str, Any]:
        """Settle one session under the active catalogue version.

        On success the real UTC bounds, duration, rate, cost, candidate lists,
        chosen segments, segment chain and a full catalogue snapshot are
        stored.  A rejection stores no UTC/duration/cost facts.  Reusing an
        ``evaluation_id``:
          * identical content -> the stored outcome is returned unchanged;
          * different content -> ``ConflictingEvaluation`` and nothing written.
        """

        if not isinstance(evaluation_id, str) or not evaluation_id:
            raise LedgerError("evaluation_id must be a non-empty string")
        definition = self._session_definition(session_id)
        session = SessionInput.from_mapping(definition)
        revision = self.active_revision()
        catalogue = self.catalogue(revision)

        try:
            result = evaluate_session(session, catalogue, evaluation_id)
            outcome = result.to_json()
            status = "ok"
            utc_start = result.utc_start
            utc_end = result.utc_end
            duration = result.duration_minutes
            rate = result.rate_per_utc_minute
            cost = result.cost
        except EvaluationRejected as rejection:
            outcome = rejection.to_json(evaluation_id)
            status = "rejected"
            utc_start = utc_end = duration = rate = cost = None

        digest = self._hash_outcome(session, revision, outcome)
        encoded = canonical_json(outcome)

        with self.transaction():
            row = self.conn.execute(
                "SELECT content_hash, outcome_json, status FROM evaluation"
                " WHERE evaluation_id = ?",
                (evaluation_id,),
            ).fetchone()
            if row is not None:
                if row["content_hash"] != digest:
                    raise ConflictingEvaluation(
                        f"evaluation_id {evaluation_id!r} already exists with "
                        f"different content; use a new evaluation_id"
                    )
                self._event(
                    "evaluate",
                    {
                        "evaluation_id": evaluation_id,
                        "status": "replayed",
                        "revision": revision,
                    },
                )
                return {
                    "evaluation_id": evaluation_id,
                    "status": row["status"],
                    "replayed": True,
                    "outcome": json.loads(row["outcome_json"]),
                }
            seq = self._next_seq("evaluation", "evaluation_seq")
            self.conn.execute(
                "INSERT INTO evaluation (evaluation_id, evaluation_seq,"
                " session_id, revision, zone_id, status, content_hash,"
                " outcome_json, utc_start, utc_end, duration_minutes,"
                " rate_per_utc_minute, cost)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    evaluation_id,
                    seq,
                    session_id,
                    revision,
                    session.zone_id,
                    status,
                    digest,
                    encoded,
                    utc_start,
                    utc_end,
                    duration,
                    rate,
                    cost,
                ),
            )
            self._event(
                "evaluate",
                {
                    "evaluation_id": evaluation_id,
                    "status": "created",
                    "revision": revision,
                },
            )
        return {
            "evaluation_id": evaluation_id,
            "status": status,
            "replayed": False,
            "outcome": outcome,
        }

    def read_evaluation(self, evaluation_id: str) -> dict[str, Any]:
        """Read a stored fact without re-interpreting it under current rules."""

        row = self.conn.execute(
            "SELECT * FROM evaluation WHERE evaluation_id = ?", (evaluation_id,)
        ).fetchone()
        if row is None:
            raise UnknownEvaluation(f"unknown evaluation {evaluation_id!r}")
        outcome = json.loads(row["outcome_json"])
        return {
            "evaluation_id": row["evaluation_id"],
            "session_id": row["session_id"],
            "revision": row["revision"],
            "zone_id": row["zone_id"],
            "status": row["status"],
            "created_at": row["created_at"],
            # The amount is read from the stored row, never recomputed.
            "stored_duration_minutes": row["duration_minutes"],
            "stored_rate_per_utc_minute": row["rate_per_utc_minute"],
            "stored_cost": row["cost"],
            "outcome": outcome,
        }

    def list_evaluations(
        self, session_id: str | None = None, include_rejected: bool = True
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT evaluation_id, session_id, revision, zone_id, status,"
            " utc_start, utc_end, duration_minutes, rate_per_utc_minute, cost"
            " FROM evaluation"
        )
        params: list[Any] = []
        clauses = []
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if not include_rejected:
            clauses.append("status = 'ok'")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY evaluation_seq"
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    # -------------------------------------------------------------- summary
    def summarize(
        self,
        utc_from: int,
        utc_to: int,
        zone_id: str | None = None,
        session_id: str | None = None,
        group_by: str = "session",
        revision: str | None = None,
        latest_per_session: bool = True,
    ) -> dict[str, Any]:
        """Settle pay over the intersection of real UTC intervals.

        Every successful evaluation contributes only the minutes that fall
        inside ``[utc_from, utc_to)`` — a session straddling the boundary is
        never billed as a whole at its start minute.  Rejected evaluations have
        no interval and are excluded by construction.

        A revision never rewrites an old fact, so a re-evaluated session can
        have both an old and a new evaluation.  ``latest_per_session = True``
        (the default) settles each session once, using its newest evaluation;
        if that newest evaluation was rejected the session contributes
        nothing, so a rejection cannot leave old pay standing in the settlement
        view.  Pass ``False`` to see every stored successful fact, and/or
        ``revision=`` to settle one clock version only.
        """

        if not isinstance(utc_from, int) or not isinstance(utc_to, int):
            raise LedgerError("summary bounds must be integer minutes")
        if utc_from > utc_to:
            raise LedgerError("summary utc_from must be <= utc_to")
        if group_by not in ("session", "zone", "revision"):
            raise LedgerError("group_by must be 'session', 'zone' or 'revision'")
        if not isinstance(latest_per_session, bool):
            raise LedgerError("latest_per_session must be a boolean")

        clauses = ["status = 'ok'", "utc_start < ?", "utc_end > ?"]
        params: list[Any] = [utc_to, utc_from]
        if zone_id is not None:
            clauses.append("zone_id = ?")
            params.append(zone_id)
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if revision is not None:
            clauses.append("revision = ?")
            params.append(revision)
        latest_clause = ""
        if latest_per_session:
            # Pick the newest evaluation per session (whatever its status),
            # then apply the interval/zone/status predicates to that choice.
            # A session whose newest determination was rejected therefore
            # contributes nothing; its older success stays readable but is not
            # settled.  When a revision is pinned, "newest" is scoped to that
            # revision so the view stays historical without double-counting
            # repeated evaluations of the same session under one table.
            latest_clause = (
                " AND evaluation_seq = (SELECT MAX(e2.evaluation_seq)"
                " FROM evaluation e2 WHERE e2.session_id = evaluation.session_id"
            )
            if revision is not None:
                latest_clause += " AND e2.revision = ?"
            latest_clause += ")"
        sql = (
            "SELECT evaluation_id, session_id, revision, zone_id, utc_start,"
            " utc_end, duration_minutes, rate_per_utc_minute, cost"
            " FROM evaluation WHERE " + " AND ".join(clauses)
        )
        if latest_clause:
            sql += latest_clause
            if revision is not None:
                params.append(revision)
        sql += " ORDER BY utc_start, evaluation_seq"
        rows = self.conn.execute(sql, params).fetchall()

        groups: dict[str, dict[str, Any]] = {}
        items = []
        total_minutes = 0
        total_cost = 0
        group_column = {"session": "session_id", "zone": "zone_id",
                        "revision": "revision"}[group_by]
        for row in rows:
            overlap_start = max(row["utc_start"], utc_from)
            overlap_end = min(row["utc_end"], utc_to)
            minutes = overlap_end - overlap_start
            if minutes <= 0:
                continue
            cost = minutes * row["rate_per_utc_minute"]
            key = str(row[group_column])
            group = groups.setdefault(
                key,
                {"key": key, "minutes": 0, "cost": 0, "evaluations": 0},
            )
            group["minutes"] += minutes
            group["cost"] += cost
            group["evaluations"] += 1
            total_minutes += minutes
            total_cost += cost
            items.append(
                {
                    "evaluation_id": row["evaluation_id"],
                    "session_id": row["session_id"],
                    "revision": row["revision"],
                    "zone_id": row["zone_id"],
                    "session_utc_start": row["utc_start"],
                    "session_utc_end": row["utc_end"],
                    "session_minutes": row["duration_minutes"],
                    "overlap_utc_start": overlap_start,
                    "overlap_utc_end": overlap_end,
                    "overlap_minutes": minutes,
                    "rate_per_utc_minute": row["rate_per_utc_minute"],
                    "overlap_cost": cost,
                }
            )
        return {
            "query": {
                "utc_from": utc_from,
                "utc_to": utc_to,
                "zone_id": zone_id,
                "session_id": session_id,
                "revision": revision,
                "group_by": group_by,
                "latest_per_session": latest_per_session,
            },
            "total_minutes": total_minutes,
            "total_cost": total_cost,
            "groups": list(groups.values()),
            "intersections": items,
        }

    # ---------------------------------------------------------------- events
    def events(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT event_seq, op, detail_json, created_at FROM event"
            " ORDER BY event_seq"
        ).fetchall()
        return [
            {
                "event_seq": row["event_seq"],
                "op": row["op"],
                "detail": json.loads(row["detail_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]
