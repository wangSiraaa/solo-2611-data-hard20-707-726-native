"""相容目录: 无向不相容类别对, 完全由输入声明.

规则约定:
- 配对无向, (A,B) 同时覆盖 B/A。
- 允许 (X,X): 同箱至少两件 X 即为冲突 (按实际件数, 不去重)。
- 未声明的类别对相容。
- 不做集合去重; 返回实际冲突类别及贡献件数。
- 与真实危险品法规无关。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class Conflict:
    """一类实际冲突: 触发它的类别对及计入本次判定的件数."""

    pair: tuple[str, str]          # 规范化后的类别对, 如 ("A", "A") / ("A", "B")
    counts: tuple[tuple[str, int], ...]  # 该对中每个参与类别实际件数 (贡献件数)

    def as_dict(self) -> dict:
        return {
            "categories": list(self.pair),
            "counts": dict(self.counts),
        }

    def __str__(self) -> str:  # 便于日志
        return "/".join(self.pair) + ":" + ",".join(f"{c}={n}" for c, n in self.counts)


class Catalog:
    """某个规则修订下的相容目录 (不可变)."""

    def __init__(self, revision: str, pairs: Iterable[Iterable[str]]):
        if not isinstance(revision, str) or not revision:
            raise ValueError("revision must be a non-empty string")
        normalized: set[tuple[str, str]] = set()
        for p in pairs:
            pair = tuple(p)
            if len(pair) != 2:
                raise ValueError(f"incompatible pair must have 2 categories: {pair!r}")
            x, y = pair
            if not isinstance(x, str) or not isinstance(y, str) or not x or not y:
                raise ValueError(f"categories must be non-empty strings: {pair!r}")
            normalized.add((x, y) if x <= y else (y, x))
        self.revision = revision
        self._pairs = frozenset(normalized)

    @classmethod
    def from_document(cls, doc: dict) -> "Catalog":
        if not isinstance(doc, dict):
            raise ValueError("catalog document must be an object")
        if "revision" not in doc or "incompatible_pairs" not in doc:
            raise ValueError("catalog needs revision and incompatible_pairs")
        return cls(doc["revision"], doc["incompatible_pairs"])

    def pairs(self) -> list[list[str]]:
        return [list(p) for p in sorted(self._pairs)]

    def is_incompatible(self, a: str, b: str) -> bool:
        key = (a, b) if a <= b else (b, a)
        return key in self._pairs

    def check(self, contents: Iterable[tuple[str, str]]) -> list[Conflict]:
        """检查多重集合.

        contents: 可迭代的 (item_id, category); item_id 仅用于允许重复而无需去重,
        判定只依据类别件数。返回实际冲突 (可能多条), 无冲突返回 []。
        """
        counts: dict[str, int] = {}
        for _item_id, category in contents:
            if not isinstance(category, str) or not category:
                raise ValueError("category must be a non-empty string")
            counts[category] = counts.get(category, 0) + 1

        conflicts: list[Conflict] = []
        for a, b in sorted(self._pairs):
            ca = counts.get(a, 0)
            cb = counts.get(b, 0)
            if a == b:
                if ca >= 2:  # 同箱至少两件 A 即冲突
                    conflicts.append(Conflict((a, a), ((a, ca),)))
            elif ca >= 1 and cb >= 1:  # 两个不同类别各至少一件
                conflicts.append(Conflict((a, b), ((a, ca), (b, cb))))
        return conflicts
