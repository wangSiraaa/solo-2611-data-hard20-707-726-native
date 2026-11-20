"""ASCII-only tokenization used by the position index.

A token is a maximal run of ASCII letters/digits.  Everything else
(including underscores and non-ASCII characters) is a separator.  Tokens
are lower-cased.  Positions are zero based within a segment.
"""

from __future__ import annotations

from typing import Iterable, Sequence

ASCII_ALNUM = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
)


def tokenize_segment(text: str) -> list[str]:
    """Split one segment into normalised tokens.

    >>> tokenize_segment("Amber__BOLT 9-x")
    ['amber', 'bolt', '9', 'x']
    >>> tokenize_segment("___")
    []
    """

    tokens: list[str] = []
    buf: list[str] = []
    for ch in text:
        if ch in ASCII_ALNUM:
            buf.append(ch)
        elif buf:
            tokens.append("".join(buf).lower())
            buf.clear()
    if buf:
        tokens.append("".join(buf).lower())
    return tokens


def tokenize_segments(segments: Sequence[str]) -> list[tuple[int, list[str]]]:
    """Tokenize an ordered list of segments.

    Returns ``(segment_index, tokens)`` pairs so callers never lose the
    segment boundary that is essential for phrase matching.
    """

    return [(idx, tokenize_segment(seg)) for idx, seg in enumerate(segments)]


def iter_positions(
    segments: Sequence[str],
) -> Iterable[tuple[int, int, str]]:
    """Yield ``(segment_index, position, token)`` for every valid token."""

    for seg_idx, tokens in tokenize_segments(segments):
        for pos, token in enumerate(tokens):
            yield seg_idx, pos, token


def normalize_query_word(word: object) -> str:
    """Normalise a word supplied in a query.

    A query word must describe exactly one token; otherwise the caller is
    almost certainly confusing a phrase with a single word.
    """

    if not isinstance(word, str):
        raise ValueError(f"query word must be a string, got {type(word).__name__}")
    tokens = tokenize_segment(word)
    if len(tokens) != 1:
        raise ValueError(
            f"query word {word!r} is not exactly one ASCII alphanumeric token"
        )
    return tokens[0]
