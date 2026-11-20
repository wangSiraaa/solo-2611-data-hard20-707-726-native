"""Material-list publishing on top of an actual position-index cut.

The publisher is deliberately separate from :class:`~index_service.catalogue.Catalogue`:
the source catalogue knows nothing about phrases, exclusion rules or
publication versions.  Publishing always works against an explicit cut
(list of ``(document_id, revision)`` pairs), so an old publication can be
read back long after the catalogue has advanced.

Rules implemented here
----------------------
* A document is a candidate iff it matches the *include* phrase in the
  required field (default ``title``).
* Candidates that match the full *exclude* phrase (default ``body``) are
  removed only *after* the complete include match has been established.
  Seeing a single exclusion word is never enough to drop a document.
* A publication is accepted only when at least ``minimum_documents``
  distinct materials remain.  Otherwise nothing is stored.
* Replaying a successful request returns the original artifact (including
  its original cut); a request conflict on the same id/version is an error.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Sequence

from .catalogue import Catalogue
from .phrase import Phrase, find_matches_for_cut, parse_phrase


class PublicationError(Exception):
    """Base class for publish-service failures."""


class PublicationRejected(PublicationError):
    """Not enough materials remained after exclusion; nothing was stored."""

    def __init__(self, message: str, *, minimum: int, selected: list[dict]) -> None:
        super().__init__(message)
        self.minimum = minimum
        self.selected = selected


class PublicationConflict(PublicationError):
    """An id or version was reused for a different request."""


@dataclass(frozen=True)
class PublicationRequest:
    include: Phrase
    exclude: Phrase
    minimum_documents: int

    def fingerprint(self) -> str:
        payload = {
            "include": {
                "field": self.include.field,
                "words": list(self.include.words),
                "g": self.include.g,
            },
            "exclude": {
                "field": self.exclude.field,
                "words": list(self.exclude.words),
                "g": self.exclude.g,
            },
            "minimum_documents": self.minimum_documents,
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def parse_request(payload: object) -> PublicationRequest:
    if not isinstance(payload, dict):
        raise ValueError("query must be an object")
    query = payload.get("query", payload)
    include = parse_phrase(query.get("include"), where="query.include")
    exclude = parse_phrase(query.get("exclude"), where="query.exclude")
    minimum = query.get("minimum_documents", 2)
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 1:
        raise ValueError("query.minimum_documents must be a positive integer")
    return PublicationRequest(include, exclude, minimum)


class Publisher:
    """Versioned, cut-aware publication store."""

    def __init__(self, database: str = ":memory:") -> None:
        self._conn = sqlite3.connect(database)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS publications (
                publication_id TEXT PRIMARY KEY,
                version        TEXT NOT NULL,
                fingerprint    TEXT NOT NULL,
                request_json   TEXT NOT NULL,
                cut_json       TEXT NOT NULL,
                artifact_json  TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS publications_by_version
            ON publications (version)
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Publisher":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- publish -------------------------------------------------------
    def publish(
        self,
        catalogue: Catalogue,
        *,
        publication_id: str,
        version: str,
        request: PublicationRequest,
        cut: Sequence[tuple[str, int]] | None = None,
    ) -> dict:
        """Publish ``publication_id``/``version`` against ``cut``.

        ``cut`` defaults to the catalogue's current canonical revision set.
        On success the artifact is stored and returned.  If the same
        request was already published, that original artifact is returned
        unchanged.
        """

        if not publication_id:
            raise ValueError("publication_id must be non-empty")
        if not version:
            raise ValueError("version must be non-empty")

        fingerprint = request.fingerprint()
        resolved_cut = tuple(cut) if cut is not None else catalogue.snapshot()

        existing_id = self._fetch_by_id(publication_id)
        if existing_id is not None:
            if (
                existing_id["version"] != version
                or existing_id["fingerprint"] != fingerprint
            ):
                raise PublicationConflict(
                    f"publication {publication_id} already exists with a "
                    "different version or request; read it back instead"
                )
            # Exact replay of the same successful request.
            return json.loads(existing_id["artifact_json"])

        existing_version = self._fetch_by_version(version)
        if existing_version is not None:
            # A version is a label for one publication.  Reusing it for a
            # different id or query would make "read back the published
            # artifact" ambiguous, so we refuse rather than replay.
            raise PublicationConflict(
                f"version {version} is already used by publication "
                f"{existing_version['publication_id']}"
            )

        # A *new* publication id/version is always evaluated against the cut
        # given (or the current cut).  We deliberately do not replay a
        # previous artifact merely because the query text matches: the same
        # query may legitimately select fewer materials after a correction.
        artifact = self._build_artifact(
            catalogue,
            publication_id=publication_id,
            version=version,
            request=request,
            cut=resolved_cut,
        )

        selected = artifact["selected"]
        if len(selected) < request.minimum_documents:
            raise PublicationRejected(
                f"only {len(selected)} material(s) selected; "
                f"minimum_documents={request.minimum_documents}",
                minimum=request.minimum_documents,
                selected=selected,
            )

        self._insert(
            publication_id,
            version,
            fingerprint,
            request,
            json.dumps([list(pair) for pair in resolved_cut]),
            artifact,
        )
        return artifact

    def _build_artifact(
        self,
        catalogue: Catalogue,
        *,
        publication_id: str,
        version: str,
        request: PublicationRequest,
        cut: Sequence[tuple[str, int]],
    ) -> dict:
        cut_pairs = [(str(d), int(r)) for d, r in cut]
        include_matches = find_matches_for_cut(catalogue, request.include, cut_pairs)
        exclude_matches = find_matches_for_cut(catalogue, request.exclude, cut_pairs)

        include_by_doc: dict[tuple[str, int], list] = {}
        for match in include_matches:
            include_by_doc.setdefault((match.document_id, match.revision), []).append(match)

        exclude_by_doc: dict[tuple[str, int], list] = {}
        for match in exclude_matches:
            exclude_by_doc.setdefault((match.document_id, match.revision), []).append(match)

        selected: list[dict] = []
        excluded: list[dict] = []
        for key in sorted(include_by_doc, key=lambda k: (k[0], k[1])):
            matches = include_by_doc[key]
            best = matches[0]
            entry = {
                "document_id": best.document_id,
                "revision": best.revision,
                "title_positions": [
                    {"segment": best.segment, "positions": list(best.positions)}
                ],
            }
            if key in exclude_by_doc:
                excluded.append(
                    {
                        "document_id": key[0],
                        "revision": key[1],
                        "reason": "exclude_phrase",
                        "exclude_query": {
                            "field": request.exclude.field,
                            "words": list(request.exclude.words),
                            "g": request.exclude.g,
                        },
                        "exclude_positions": [
                            {"segment": m.segment, "positions": list(m.positions)}
                            for m in exclude_by_doc[key]
                        ],
                    }
                )
            else:
                selected.append(entry)

        return {
            "publication_id": publication_id,
            "version": version,
            "cut": [[d, r] for d, r in cut_pairs],
            "query": {
                "include": {
                    "field": request.include.field,
                    "words": list(request.include.words),
                    "g": request.include.g,
                },
                "exclude": {
                    "field": request.exclude.field,
                    "words": list(request.exclude.words),
                    "g": request.exclude.g,
                },
                "minimum_documents": request.minimum_documents,
            },
            "selected": selected,
            "excluded": excluded,
        }

    # -- read ----------------------------------------------------------
    def read(self, publication_id: str) -> dict:
        row = self._fetch_by_id(publication_id)
        if row is None:
            raise PublicationError(f"unknown publication {publication_id}")
        return json.loads(row["artifact_json"])

    def list_publications(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT publication_id, version FROM publications "
            "ORDER BY publication_id"
        ).fetchall()
        return [
            {"publication_id": r["publication_id"], "version": r["version"]}
            for r in rows
        ]

    # -- persistence helpers ------------------------------------------
    def _fetch_by_id(self, publication_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM publications WHERE publication_id = ?",
            (publication_id,),
        ).fetchone()

    def _fetch_by_version(self, version: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM publications WHERE version = ?",
            (version,),
        ).fetchone()

    def _insert(
        self,
        publication_id: str,
        version: str,
        fingerprint: str,
        request: PublicationRequest,
        cut_json: str,
        artifact: dict,
    ) -> None:
        request_json = json.dumps(
            {
                "include": {
                    "field": request.include.field,
                    "words": list(request.include.words),
                    "g": request.include.g,
                },
                "exclude": {
                    "field": request.exclude.field,
                    "words": list(request.exclude.words),
                    "g": request.exclude.g,
                },
                "minimum_documents": request.minimum_documents,
            },
            sort_keys=True,
        )
        with self._conn:
            self._conn.execute(
                "INSERT INTO publications "
                "(publication_id, version, fingerprint, request_json, cut_json, artifact_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    publication_id,
                    version,
                    fingerprint,
                    request_json,
                    cut_json,
                    json.dumps(artifact, ensure_ascii=False),
                ),
            )
