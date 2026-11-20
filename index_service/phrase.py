"""Exact position-phrase matching over the position index.

A phrase is an ordered list of query words plus a maximum number of
intervening words ``g``.  A match is a sequence of positions that satisfy:

* same document, revision, field and segment;
* positions are strictly increasing;
* ``next.position - previous.position <= g + 1``.

Because positions are strict, a repeated query word can only use distinct
positions.  No match can ever span a segment boundary.

Matching inside a segment is a backwards dynamic program, so it is linear
in the number of postings rather than exponential in the phrase length.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from typing import Iterable, Sequence

from .catalogue import Catalogue, Posting
from .tokenizer import normalize_query_word


@dataclass(frozen=True)
class Match:
    document_id: str
    revision: int
    field: str
    segment: int
    positions: tuple[int, ...]


@dataclass(frozen=True)
class Phrase:
    field: str
    words: tuple[str, ...]
    g: int

    @property
    def max_step(self) -> int:
        return self.g + 1


def parse_phrase(spec: object, *, where: str) -> Phrase:
    if not isinstance(spec, dict):
        raise ValueError(f"{where} must be an object")
    field = spec.get("field")
    if field not in ("title", "body"):
        raise ValueError(f"{where}.field must be 'title' or 'body'")
    raw_words = spec.get("words")
    if not isinstance(raw_words, (list, tuple)) or not raw_words:
        raise ValueError(f"{where}.words must be a non-empty list")
    words = tuple(normalize_query_word(w) for w in raw_words)
    g = spec.get("g", 0)
    if isinstance(g, bool) or not isinstance(g, int) or g < 0:
        raise ValueError(f"{where}.g must be a non-negative integer")
    return Phrase(field, words, g)


def _search_segment(
    word_positions: Sequence[Sequence[int]], max_step: int
) -> tuple[int, ...] | None:
    """Find one strictly increasing position chain within one segment.

    ``word_positions[i]`` is the sorted list of positions carrying word i.
    Works backwards: a position is *good* if it can reach the end of the
    phrase.  A backwards pass is exact (unlike a purely greedy forwards
    scan) and runs in ``O(total_positions * log total_positions)``.
    """

    k = len(word_positions)
    if k == 0:
        return None
    for positions in word_positions:
        if not positions:
            return None

    # good[i] maps each reachable position of word i to its successor for
    # word i+1 (or None for the last word).
    good: list[dict[int, int | None]] = [dict() for _ in range(k)]
    for position in word_positions[k - 1]:
        good[k - 1][position] = None

    for index in range(k - 2, -1, -1):
        successors = sorted(good[index + 1])
        if not successors:
            return None
        for position in word_positions[index]:
            lo = bisect_left(successors, position + 1)
            if lo >= len(successors):
                continue
            candidate = successors[lo]
            if candidate - position <= max_step:
                good[index][position] = candidate
        if not good[index]:
            return None

    starts = sorted(good[0])
    if not starts:
        return None
    chain = [starts[0]]
    for index in range(0, k - 1):
        successor = good[index][chain[-1]]
        assert successor is not None
        chain.append(successor)
    return tuple(chain)


def _group_by_segment(postings: Iterable[Posting], field: str) -> dict:
    grouped: dict[tuple[str, int, str, int], dict[str, list[int]]] = {}
    for p in postings:
        if p.field != field:
            continue
        key = (p.document_id, p.revision, p.field, p.segment)
        grouped.setdefault(key, {}).setdefault(p.token, []).append(p.position)
    return grouped


def _match_from_grouped(
    grouped: dict, phrase: Phrase
) -> list[Match]:
    matches: list[Match] = []
    for (document_id, revision, field, segment), by_word in grouped.items():
        if any(word not in by_word for word in phrase.words):
            continue
        word_positions = [sorted(by_word[word]) for word in phrase.words]
        found = _search_segment(word_positions, phrase.max_step)
        if found is not None:
            matches.append(Match(document_id, revision, field, segment, found))
    matches.sort(key=lambda m: (m.document_id, m.revision, m.segment, m.positions))
    return matches


def find_matches(catalogue: Catalogue, phrase: Phrase) -> list[Match]:
    """Return every phrase match in the whole catalogue (all revisions)."""

    if not phrase.words:
        return []
    postings: list[Posting] = []
    for word in dict.fromkeys(phrase.words):
        postings.extend(catalogue.postings_for_token(word))
    return _match_from_grouped(_group_by_segment(postings, phrase.field), phrase)


def find_matches_for_cut(
    catalogue: Catalogue,
    phrase: Phrase,
    cut: Iterable[tuple[str, int]],
) -> list[Match]:
    """Like :func:`find_matches` but restricted to an explicit revision cut."""

    if not phrase.words:
        return []
    cut_set = set(cut)
    if not cut_set:
        return []
    postings: list[Posting] = []
    for word in dict.fromkeys(phrase.words):
        postings.extend(catalogue.postings_for_token(word, documents=cut_set))
    return _match_from_grouped(_group_by_segment(postings, phrase.field), phrase)
