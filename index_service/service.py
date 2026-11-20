"""High-level facade tying the catalogue and publisher together."""

from __future__ import annotations

from typing import Iterable, Sequence

from .catalogue import Catalogue
from .phrase import find_matches, find_matches_for_cut, parse_phrase
from .publisher import (
    PublicationRequest,
    Publisher,
    parse_request,
)


class Service:
    """Combines a position-index catalogue with a versioned publisher."""

    def __init__(
        self,
        catalogue: Catalogue | None = None,
        publisher: Publisher | None = None,
    ) -> None:
        self.catalogue = catalogue if catalogue is not None else Catalogue()
        self.publisher = publisher if publisher is not None else Publisher()

    def close(self) -> None:
        self.catalogue.close()
        self.publisher.close()

    def __enter__(self) -> "Service":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- catalogue -----------------------------------------------------
    def load_documents(self, documents: Iterable[object]) -> list[bool]:
        return self.catalogue.add_revisions(documents)

    def apply_revision(self, payload: object) -> bool:
        """Apply one whole-document replacement revision."""

        return self.catalogue.add_revision(payload)

    def current_snapshot(self) -> tuple[tuple[str, int], ...]:
        return self.catalogue.snapshot()

    def query_positions(
        self,
        spec: object,
        *,
        cut: Sequence[tuple[str, int]] | None = None,
    ) -> list[dict]:
        """Independent position-phrase query (no publish side effects)."""

        phrase = parse_phrase(spec, where="phrase")
        if cut is None:
            matches = find_matches(self.catalogue, phrase)
        else:
            matches = find_matches_for_cut(self.catalogue, phrase, cut)
        return [
            {
                "document_id": m.document_id,
                "revision": m.revision,
                "field": m.field,
                "segment": m.segment,
                "positions": list(m.positions),
            }
            for m in matches
        ]

    # -- publishing ----------------------------------------------------
    def publish(
        self,
        *,
        publication_id: str,
        version: str,
        query: object,
        cut: Sequence[tuple[str, int]] | None = None,
    ) -> dict:
        request = query if isinstance(query, PublicationRequest) else parse_request(query)
        return self.publisher.publish(
            self.catalogue,
            publication_id=publication_id,
            version=version,
            request=request,
            cut=cut,
        )

    def read(self, publication_id: str) -> dict:
        return self.publisher.read(publication_id)
