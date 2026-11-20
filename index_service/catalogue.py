"""SQLite-backed position index catalogue.

Design notes
------------
* Every accepted revision of a document is kept; corrections replace the
  *current* revision but never delete the previous one or its postings.
* The postings table is a pure position index: it does not know about
  queries, exclusion rules or publish versions.  A posting is
  ``(token, document_id, revision, field, segment, position)``.
* Segments are tokenized independently so a phrase can never accidentally
  bridge a segment boundary.
* Replaying an identical revision (same segment order *and* identical
  segment text) is a no-op.  A revision number that is reused with
  different text is rejected as a whole batch.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Iterable, Iterator

from .tokenizer import tokenize_segments

FIELDS = ("title", "body")


class CatalogueError(Exception):
    """Base class for catalogue failures."""


class RevisionConflict(CatalogueError):
    """A revision number was reused with different content."""


class UnknownField(CatalogueError):
    pass


@dataclass(frozen=True)
class Document:
    document_id: str
    revision: int
    title: tuple[str, ...]
    body: tuple[str, ...]

    def segments(self, field_name: str) -> tuple[str, ...]:
        if field_name == "title":
            return self.title
        if field_name == "body":
            return self.body
        raise UnknownField(field_name)


@dataclass(frozen=True)
class Posting:
    document_id: str
    revision: int
    field: str
    segment: int
    position: int
    token: str


def _coerce_segments(value: object, *, where: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise CatalogueError(f"{where} must be a list of strings")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise CatalogueError(f"{where} contains a non-string segment: {item!r}")
        out.append(item)
    return tuple(out)


def _normalise_revision(value: object, *, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CatalogueError(f"{where} revision must be a positive integer")
    if value <= 0:
        raise CatalogueError(f"{where} revision must be a positive integer")
    return value


def _document_from_payload(payload: object) -> Document:
    if not isinstance(payload, dict):
        raise CatalogueError("document payload must be an object")
    document_id = payload.get("id", payload.get("document_id"))
    if not isinstance(document_id, str) or not document_id:
        raise CatalogueError("document id must be a non-empty string")
    revision = _normalise_revision(payload.get("revision"), where=f"document {document_id}")
    title = _coerce_segments(payload.get("title", []), where=f"{document_id}.title")
    body = _coerce_segments(payload.get("body", []), where=f"{document_id}.body")
    return Document(document_id, revision, title, body)


class Catalogue:
    """Persistent position index.

    The catalogue owns a single SQLite connection.  All mutations are
    wrapped in transactions so an invalid batch leaves no partial state.
    """

    def __init__(self, database: str = ":memory:") -> None:
        self._conn = sqlite3.connect(database)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    # -- lifecycle -----------------------------------------------------
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Catalogue":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _create_schema(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    document_id TEXT NOT NULL,
                    revision    INTEGER NOT NULL,
                    title_json  TEXT NOT NULL,
                    body_json   TEXT NOT NULL,
                    canonical   INTEGER NOT NULL,
                    PRIMARY KEY (document_id, revision)
                );

                CREATE TABLE IF NOT EXISTS postings (
                    token       TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    revision    INTEGER NOT NULL,
                    field       TEXT NOT NULL,
                    segment     INTEGER NOT NULL,
                    position    INTEGER NOT NULL,
                    PRIMARY KEY (token, document_id, revision, field, segment, position)
                );

                CREATE INDEX IF NOT EXISTS postings_by_revision
                    ON postings (document_id, revision, token);
                """
            )

    # -- writes --------------------------------------------------------
    def add_revision(self, payload: object) -> bool:
        """Add or replay a single revision.

        Returns ``True`` when a new revision was stored, ``False`` when it
        was an exact replay.  Raises :class:`RevisionConflict` if the
        revision number exists with different content.
        """

        return self.add_revisions([payload])[0]

    def add_revisions(self, payloads: Iterable[object]) -> list[bool]:
        """Validate, then apply a whole batch atomically.

        Every payload is checked before *any* row is written.  If one
        revision conflicts, the entire batch is rejected and the
        catalogue is unchanged.
        """

        documents = [_document_from_payload(p) for p in payloads]
        stored: list[bool] = []
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for document in documents:
                stored.append(self._apply_locked(document))
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return stored

    def _apply_locked(self, document: Document) -> bool:
        existing = self._conn.execute(
            "SELECT title_json, body_json FROM documents "
            "WHERE document_id = ? AND revision = ?",
            (document.document_id, document.revision),
        ).fetchone()
        if existing is not None:
            same = (
                json.loads(existing["title_json"]) == list(document.title)
                and json.loads(existing["body_json"]) == list(document.body)
            )
            if same:
                return False
            raise RevisionConflict(
                f"revision {document.revision} of {document.document_id} "
                "already exists with different content"
            )

        current = self._current_revision(document.document_id)
        if current is not None and document.revision <= current:
            raise RevisionConflict(
                f"revision {document.revision} of {document.document_id} "
                f"is not newer than current revision {current}"
            )

        self._conn.execute(
            "INSERT INTO documents (document_id, revision, title_json, body_json, canonical) "
            "VALUES (?, ?, ?, ?, 0)",
            (
                document.document_id,
                document.revision,
                json.dumps(list(document.title), ensure_ascii=False),
                json.dumps(list(document.body), ensure_ascii=False),
            ),
        )
        self._insert_postings(document)
        self._mark_canonical(document.document_id, document.revision)
        return True

    def _insert_postings(self, document: Document) -> None:
        rows: list[tuple[object, ...]] = []
        for field_name in FIELDS:
            segments = tokenize_segments(document.segments(field_name))
            for segment_index, tokens in segments:
                for position, token in enumerate(tokens):
                    rows.append(
                        (
                            token,
                            document.document_id,
                            document.revision,
                            field_name,
                            segment_index,
                            position,
                        )
                    )
        self._conn.executemany(
            "INSERT INTO postings "
            "(token, document_id, revision, field, segment, position) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )

    def _mark_canonical(self, document_id: str, revision: int) -> None:
        self._conn.execute(
            "UPDATE documents SET canonical = 0 WHERE document_id = ?",
            (document_id,),
        )
        self._conn.execute(
            "UPDATE documents SET canonical = 1 "
            "WHERE document_id = ? AND revision = ?",
            (document_id, revision),
        )

    def _current_revision(self, document_id: str) -> int | None:
        row = self._conn.execute(
            "SELECT revision FROM documents "
            "WHERE document_id = ? AND canonical = 1",
            (document_id,),
        ).fetchone()
        return None if row is None else int(row["revision"])

    # -- reads ---------------------------------------------------------
    def current_revision(self, document_id: str) -> int | None:
        return self._current_revision(document_id)

    def revisions(self, document_id: str) -> list[int]:
        rows = self._conn.execute(
            "SELECT revision FROM documents WHERE document_id = ? ORDER BY revision",
            (document_id,),
        ).fetchall()
        return [int(r["revision"]) for r in rows]

    def document(self, document_id: str, revision: int) -> Document:
        row = self._conn.execute(
            "SELECT title_json, body_json FROM documents "
            "WHERE document_id = ? AND revision = ?",
            (document_id, revision),
        ).fetchone()
        if row is None:
            raise CatalogueError(
                f"no revision {revision} for document {document_id}"
            )
        return Document(
            document_id,
            revision,
            tuple(json.loads(row["title_json"])),
            tuple(json.loads(row["body_json"])),
        )

    def current_documents(self) -> list[Document]:
        rows = self._conn.execute(
            "SELECT document_id, revision FROM documents "
            "WHERE canonical = 1 ORDER BY document_id"
        ).fetchall()
        return [self.document(r["document_id"], int(r["revision"])) for r in rows]

    def snapshot(self) -> tuple[tuple[str, int], ...]:
        """Return the canonical (document_id, revision) cut of the catalogue."""

        rows = self._conn.execute(
            "SELECT document_id, revision FROM documents "
            "WHERE canonical = 1 ORDER BY document_id"
        ).fetchall()
        return tuple((r["document_id"], int(r["revision"])) for r in rows)

    def postings_for_token(
        self, token: str, *, documents: Iterable[tuple[str, int]] | None = None
    ) -> list[Posting]:
        """Fetch postings for a token, optionally restricted to a cut."""

        if documents is None:
            rows = self._conn.execute(
                "SELECT document_id, revision, field, segment, position "
                "FROM postings WHERE token = ? "
                "ORDER BY document_id, revision, field, segment, position",
                (token,),
            ).fetchall()
        else:
            cut = sorted(set(documents))
            if not cut:
                return []
            placeholders = ",".join("(?, ?)" for _ in cut)
            params: list[object] = [token]
            for document_id, revision in cut:
                params.extend((document_id, revision))
            rows = self._conn.execute(
                "SELECT document_id, revision, field, segment, position "
                "FROM postings WHERE token = ? "
                f"AND (document_id, revision) IN (VALUES {placeholders}) "
                "ORDER BY document_id, revision, field, segment, position",
                params,
            ).fetchall()
        return [
            Posting(
                r["document_id"],
                int(r["revision"]),
                r["field"],
                int(r["segment"]),
                int(r["position"]),
                token,
            )
            for r in rows
        ]

    def all_postings(
        self, *, documents: Iterable[tuple[str, int]] | None = None
    ) -> Iterator[Posting]:
        if documents is None:
            rows = self._conn.execute(
                "SELECT token, document_id, revision, field, segment, position "
                "FROM postings "
                "ORDER BY document_id, revision, field, segment, position, token"
            )
        else:
            cut = sorted(set(documents))
            if not cut:
                return iter(())
            placeholders = ",".join("(?, ?)" for _ in cut)
            params: list[object] = []
            for document_id, revision in cut:
                params.extend((document_id, revision))
            rows = self._conn.execute(
                "SELECT token, document_id, revision, field, segment, position "
                "FROM postings "
                f"WHERE (document_id, revision) IN (VALUES {placeholders}) "
                "ORDER BY document_id, revision, field, segment, position, token",
                params,
            )
        return iter(
            Posting(
                r["document_id"],
                int(r["revision"]),
                r["field"],
                int(r["segment"]),
                int(r["position"]),
                r["token"],
            )
            for r in rows
        )
