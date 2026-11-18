"""合成展馆的平面几何内核。

全部坐标使用 :class:`fractions.Fraction`，保证门开集端点、门框点、
折线上的有理穿越点都能精确比较，不引入浮点误差。

约定：
* 矩形为闭集合 ``[x0,x1] x [y0,y1]``；“严格内部”不含四条边。
* 门是落在两室共享正长度边上的 **开放线段** (lo, hi)，门框端点不可通行。
* 房间内部两两不相交，边界可以重合（共边）；仅点接触（四角相遇）不构成共边。
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable, Sequence

# 有理类型别名。JSON 里可能出现整数、有限小数或 "p/q" 字符串，统一精确化。
Rational = Fraction


def to_rational(value: object) -> Fraction:
    """把 JSON 中的 int/float/str 转成精确分数；不接受 NaN/Inf。"""
    if isinstance(value, Fraction):
        return value
    if isinstance(value, bool):  # bool 是 int 的子类，显式拒绝以免混入坐标
        raise TypeError(f"坐标不能是布尔值: {value!r}")
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float):
        return Fraction(value)  # 有限小数会被精确转成分数
    if isinstance(value, str):
        return Fraction(value.strip())
    raise TypeError(f"无法解释为有理坐标: {value!r}")


@dataclass(frozen=True, slots=True)
class Point:
    """有理点。"""

    x: Fraction
    y: Fraction

    @classmethod
    def make(cls, raw: Sequence[object]) -> "Point":
        if len(raw) != 2:
            raise ValueError(f"点必须恰好有两个坐标: {raw!r}")
        return cls(to_rational(raw[0]), to_rational(raw[1]))

    def as_pair(self) -> list[str]:
        return [str(self.x), str(self.y)]


@dataclass(frozen=True, slots=True)
class Rect:
    """轴对齐闭矩形 [x0,x1] x [y0,y1]，要求正面积。"""

    x0: Fraction
    y0: Fraction
    x1: Fraction
    y1: Fraction

    @classmethod
    def make(cls, raw: Sequence[object]) -> "Rect":
        if len(raw) != 4:
            raise ValueError(f"矩形必须是 [x0,y0,x1,y1]: {raw!r}")
        x0, y0, x1, y1 = (to_rational(v) for v in raw)
        if not (x1 > x0 and y1 > y0):
            raise ValueError(f"矩形必须具有正面积: {raw!r}")
        return cls(x0, y0, x1, y1)

    def strict_contains(self, p: Point) -> bool:
        """点是否在矩形严格内部（不含边界）。"""
        return self.x0 < p.x < self.x1 and self.y0 < p.y < self.y1

    def contains(self, p: Point) -> bool:
        """点是否在闭矩形内（含边界）。"""
        return self.x0 <= p.x <= self.x1 and self.y0 <= p.y <= self.y1

    def interior_overlaps(self, other: "Rect") -> bool:
        """两个矩形的 **开内部** 是否有正面积交集。

        边重合、角点接触时返回 False，符合“内部不得重叠、边界可重合”。
        """
        ox = min(self.x1, other.x1) - max(self.x0, other.x0)
        oy = min(self.y1, other.y1) - max(self.y0, other.y0)
        return ox > 0 and oy > 0


# ---------------------------------------------------------------------------
# 共享边：两段正长度、坐标轴对齐的闭区间。
# 竖边 orientation="v"：fixed=x，沿 y 的区间 [lo,hi]；
# 横边 orientation="h"：fixed=y，沿 x 的区间 [lo,hi]。
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SharedEdge:
    orientation: str  # "v" 或 "h"
    fixed: Fraction
    lo: Fraction
    hi: Fraction

    @property
    def length(self) -> Fraction:
        return self.hi - self.lo

    def contains_axis_value(self, v: Fraction) -> bool:
        """变化坐标 v 是否落在边的闭区间内。"""
        return self.lo <= v <= self.hi


def shared_edge(a: Rect, b: Rect) -> SharedEdge | None:
    """求两矩形共享的正长度边；仅点接触或无接触时返回 None。"""
    # 竖直共边：一条 x 边界相等，且 y 投影交叠为正长度
    if a.x1 == b.x0 or b.x1 == a.x0:
        fixed = a.x1 if a.x1 == b.x0 else b.x1
        lo = max(a.y0, b.y0)
        hi = min(a.y1, b.y1)
        if hi > lo:
            return SharedEdge("v", fixed, lo, hi)
    # 水平共边：一条 y 边界相等，且 x 投影交叠为正长度
    if a.y1 == b.y0 or b.y1 == a.y0:
        fixed = a.y1 if a.y1 == b.y0 else b.y1
        lo = max(a.x0, b.x0)
        hi = min(a.x1, b.x1)
        if hi > lo:
            return SharedEdge("h", fixed, lo, hi)
    return None


# ---------------------------------------------------------------------------
# 折线段在闭矩形内的参数区间，以及事件点切分。
# 线段 P(t)=p0+(p1-p0)*t, t∈[0,1]。
# ---------------------------------------------------------------------------
def _clip_axis(lo: Fraction, hi: Fraction, c0: Fraction, c1: Fraction
               ) -> tuple[Fraction, Fraction] | None:
    """一维裁剪：求使 lo <= c0+(c1-c0)t <= hi 的 t 闭区间。"""
    if c1 == c0:
        # 常数坐标：完全在带内则不产生 t 约束，否则该矩形不包含线段任何点
        return (Fraction(0), Fraction(1)) if lo <= c0 <= hi else None
    t_lo = (lo - c0) / (c1 - c0)
    t_hi = (hi - c0) / (c1 - c0)
    if t_lo > t_hi:
        t_lo, t_hi = t_hi, t_lo
    t_lo = max(t_lo, Fraction(0))
    t_hi = min(t_hi, Fraction(1))
    if t_lo > t_hi:
        return None
    return t_lo, t_hi


def closed_t_span(rect: Rect, p0: Point, p1: Point) -> tuple[Fraction, Fraction] | None:
    """线段（含端点）落在闭矩形内的 t 区间；完全不相交返回 None。"""
    tx = _clip_axis(rect.x0, rect.x1, p0.x, p1.x)
    if tx is None:
        return None
    ty = _clip_axis(rect.y0, rect.y1, p0.y, p1.y)
    if ty is None:
        return None
    lo = max(tx[0], ty[0])
    hi = min(tx[1], ty[1])
    return (lo, hi) if lo <= hi else None


def point_at(p0: Point, p1: Point, t: Fraction) -> Point:
    """线段参数 t 处的点（t 为精确分数）。"""
    return Point(p0.x + (p1.x - p0.x) * t, p0.y + (p1.y - p0.y) * t)


def merge_open_union(intervals: Iterable[tuple[Fraction, Fraction]]
                     ) -> list[tuple[Fraction, Fraction]]:
    """把开放区间 (lo,hi) 按“开集并”合并。

    仅在真正交叠时合并；两个只在端点相接的区间 **不** 合并——
    端点本身不属于任何开集，不能被自动填成可通行点。
    """
    items = sorted((lo, hi) for lo, hi in intervals if hi > lo)
    merged: list[tuple[Fraction, Fraction]] = []
    for lo, hi in items:
        if not merged or lo >= merged[-1][1]:
            merged.append((lo, hi))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
    return merged
