"""路线事实核查器（不寻找路线，只核查给定折线）。

核查规则（对应任务书）：
* 起终点必须在某个房间的 **严格内部**；
* 除孤立的穿门点外，折线每一段的每一点都必须位于某房严格内部；
* 沿墙走一段 / 在墙上等待 / 擦墙返回 / 越过未定义空间，一律拒绝；
* 每个穿越点逐一核对：两侧房间、正长度共同边、门的 **开放** 段；
  只查房间邻接图不够——对角公共角点没有共边门，门框端点不可通行；
* 折线结点恰在门上时，把前后两段合并解释为一次穿越，不重复计两次；
* 不能借折线转折从无门角点跳入对角房；
* 一条失败路线不生成合法通行凭据。

实现取舍：对每段路径与所有房间做精确 t 区间求交，取事件点把 [0,1] 切开，
每个开区间中点必须落在 **恰好一个** 房间严格内部；事件点再按门开集裁决。
规模上限（30 室 / 每路线 100 段）下，朴素求交即足够，且不依赖任何
预计算网格，语义最直接可审计。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from fractions import Fraction
from typing import Literal

from .catalog import Gate, PlanVersion
from .geometry import Point, closed_t_span, point_at

# ---------------------------------------------------------------------------
# 违规原因码（稳定字符串，供报告与测试引用）
# ---------------------------------------------------------------------------
ZERO_LENGTH_SEGMENT = "ZERO_LENGTH_SEGMENT"            # 在墙上等待 / 退化段
ENDPOINT_NOT_INTERIOR = "ENDPOINT_NOT_INTERIOR"        # 起终点不在严格内部
WALK_ALONG_WALL = "WALK_ALONG_WALL"                    # 正长度区间贴墙
OUTSIDE_PLAN = "OUTSIDE_PLAN"                          # 越过未定义空间
WALL_CONTACT = "WALL_CONTACT"                          # 孤立触墙但未穿门（含擦墙返回）
POINT_CONTACT_NO_SHARED_EDGE = "POINT_CONTACT_NO_SHARED_EDGE"  # 四角对角穿越
GATE_FRAME_ENDPOINT = "GATE_FRAME_ENDPOINT"            # 恰在门框端点
NO_GATE_ON_EDGE = "NO_GATE_ON_EDGE"                    # 共边存在但该点无开放门段
ROOM_OVERLAP_ANOMALY = "ROOM_OVERLAP_ANOMALY"          # 目录不该出现的内部重叠


class NodeKind(Enum):
    INTERIOR = "interior"   # 某房间严格内部
    CROSSING = "crossing"   # 门开段上的合法穿越点
    ILLEGAL = "illegal"     # 其余一切边界/外部情况


@dataclass(frozen=True, slots=True)
class Violation:
    reason: str
    point: list[str]
    detail: str
    segment_index: int | None = None  # 所属折线线段；结点违规给前一段
    t: str | None = None


@dataclass(frozen=True, slots=True)
class Crossing:
    gate_id: str
    point: list[str]
    from_room: str
    to_room: str
    segment_index: int
    t: str
    all_gate_ids: tuple[str, ...] = ()  # 重叠门：开集并覆盖该点的全部门
    at_node: bool = False  # 穿越点恰为折线结点（前后段合并解释，只计一次）


@dataclass(frozen=True, slots=True)
class Stay:
    """按路线顺序的房间驻段（开区间；穿越点本身不属于驻段）。"""
    room_id: str
    segment_index: int
    t_lo: str
    t_hi: str


@dataclass(slots=True)
class SegmentAnalysis:
    index: int
    p0: Point
    p1: Point
    interior_rooms: list[tuple[Fraction, Fraction, str]]  # 严格内部开区间
    events: list[Fraction]                                 # 切分事件 t
    violations: list[Violation] = field(default_factory=list)


@dataclass(slots=True)
class RouteResult:
    route_id: str
    legal: bool
    original_points: list[list[str]]
    first_violation: Violation | None
    violations: list[Violation]
    stays: list[Stay]
    crossings: list[Crossing]
    room_chain: list[str]
    credential_id: str | None = None


def analyze_segment(version: PlanVersion, index: int,
                    p0: Point, p1: Point) -> SegmentAnalysis:
    """单段几何分析：内部开区间、事件点、区间级违规（门裁决在全局做）。"""
    analysis = SegmentAnalysis(index, p0, p1, [], [])
    if p0 == p1:
        analysis.violations.append(Violation(
            ZERO_LENGTH_SEGMENT, p0.as_pair(),
            "折线段长度为零（在墙上/室内等待同样不产生驻段）",
            index, "0"))
        analysis.events = [Fraction(0), Fraction(1)]
        return analysis

    spans: list[tuple[Fraction, Fraction, str]] = []
    for rid, rect in version.rooms.items():
        span = closed_t_span(rect, p0, p1)
        if span is not None:
            spans.append((span[0], span[1], rid))

    events = {Fraction(0), Fraction(1)}
    for lo, hi, _ in spans:
        events.add(lo)
        events.add(hi)
    ts = sorted(events)
    analysis.events = ts

    # 每个开区间中点必须位于恰好一个房间的严格内部
    for k in range(len(ts) - 1):
        lo, hi = ts[k], ts[k + 1]
        mid = (lo + hi) / 2
        pm = point_at(p0, p1, mid)
        inside = [rid for rid, rect in version.rooms.items()
                  if rect.strict_contains(pm)]
        if len(inside) == 1:
            analysis.interior_rooms.append((lo, hi, inside[0]))
            continue
        detail_p = point_at(p0, p1, lo)
        if not inside:
            boundary = [rid for lo2, hi2, rid in spans
                        if lo2 <= mid <= hi2]
            reason = WALK_ALONG_WALL if boundary else OUTSIDE_PLAN
            detail = ("正长度路径与墙重合（沿墙行走）" if boundary
                      else "正长度路径位于任何房间之外（越过未定义空间）")
            analysis.violations.append(
                Violation(reason, detail_p.as_pair(), detail,
                          index, str(lo)))
        else:
            analysis.violations.append(Violation(
                ROOM_OVERLAP_ANOMALY, detail_p.as_pair(),
                f"同一位置位于 {len(inside)} 个房间内部: {sorted(inside)}",
                index, str(lo)))
    return analysis


def _edge_gates_at(version: PlanVersion, room_l: str, room_r: str,
                   point: Point) -> tuple[list[Gate], object, str | None]:
    """裁决边界点 point 在 room_l/room_r 之间的门状态。

    返回 (覆盖该点的开放门列表, 共边, 违规原因)；
    原因仅在没有开放门覆盖时给出。
    """
    key = tuple(sorted((room_l, room_r)))
    edge = version.edges.get(key)
    if edge is None:
        return [], None, POINT_CONTACT_NO_SHARED_EDGE
    # 竖边沿 y 变化、横边沿 x 变化；变化坐标必须落在边区间与门开段内
    v = point.y if edge.orientation == "v" else point.x
    if not edge.contains_axis_value(v):
        return [], edge, POINT_CONTACT_NO_SHARED_EDGE
    gates = [g for g in version.gates.values()
             if tuple(sorted((g.room_a, g.room_b))) == key]
    open_gates = [g for g in gates if g.open_contains(v)]
    if open_gates:
        return sorted(open_gates, key=lambda g: g.gate_id), edge, None
    # 点在共边上但不在任何开放门段内：区分“恰为门框端点”与“墙上无门”
    if any(g.lo == v or g.hi == v for g in gates):
        return [], edge, GATE_FRAME_ENDPOINT
    return [], edge, NO_GATE_ON_EDGE


def verify_route(version: PlanVersion, route_id: str,
                 raw_points: list) -> RouteResult:
    """核查整条折线，返回完整事实结果；非法时无凭据。"""
    points = [Point.make(p) for p in raw_points]
    original = [p.as_pair() for p in points]
    if len(points) < 2:
        return RouteResult(
            route_id, False, original,
            Violation(ZERO_LENGTH_SEGMENT,
                      points[0].as_pair() if points else ["-", "-"],
                      "折线至少需要两个点", None, None),
            [], [], [], [])

    analyses = [analyze_segment(version, i, points[i], points[i + 1])
                for i in range(len(points) - 1)]
    # 段自身违规（零长段、沿墙、越界）先入列，保证首个非法点按几何顺序给出
    violations: list[Violation] = [v for a in analyses for v in a.violations]
    crossings: list[Crossing] = []
    stays: list[Stay] = []
    degenerate = any(p0 == p1 for p0, p1 in zip(points, points[1:]))

    if not degenerate:
        # ---- 段内事件点（t∈(0,1)）：门裁决 ----------------------------
        for ana in analyses:
            for t in ana.events:
                if t in (Fraction(0), Fraction(1)):
                    continue
                p = point_at(ana.p0, ana.p1, t)
                left = _room_of_interval(ana, t, side="left")
                right = _room_of_interval(ana, t, side="right")
                v = _judge_event(version, ana.index, t, p, left, right,
                                 violations, crossings)
                if v is not None:
                    violations.append(v)

        # ---- 内部折线结点：合并前后段解释，绝不重复算两次 --------------
        for k in range(1, len(points) - 1):
            rl = _endpoint_rooms(analyses[k - 1], side=1)
            rr = _endpoint_rooms(analyses[k], side=0)
            _classify_node(version, k, points[k], rl, rr,
                           violations, crossings)

    # ---- 起点 / 终点必须严格内部；边界点还要给出具体门/墙语义 ----------
    for end_idx in (0, len(points) - 1):
        ana = analyses[0] if end_idx == 0 else analyses[-1]
        p = points[end_idx]
        inside = [rid for rid, rect in version.rooms.items()
                  if rect.strict_contains(p)]
        if len(inside) == 1:
            continue
        neighbor = _endpoint_rooms(
            ana, side=0 if end_idx == 0 else 1)
        if neighbor:
            # 端点落在某房间边界上：尝试按门裁决，给出门框/无门/角点原因
            other = _other_room_at_boundary(version, p, neighbor[0])
            if other is not None:
                v = _judge_event(version, ana.index,
                                 Fraction(0 if end_idx == 0 else 1),
                                 p, neighbor[0], other,
                                 violations, crossings)
                if v is not None:
                    violations.append(v)
        violations.append(Violation(
            ENDPOINT_NOT_INTERIOR, p.as_pair(),
            f"{'起点' if end_idx == 0 else '终点'}必须位于一个房间的严格内部",
            ana.index, "0" if end_idx == 0 else "1"))

    # ---- 按路线顺序汇总驻段 -------------------------------------------
    for ana in analyses:
        for lo, hi, rid in sorted(ana.interior_rooms):
            stays.append(Stay(rid, ana.index, str(lo), str(hi)))

    # 按路线的实际几何顺序（段号, 参数 t）排序，分数按数值而非字符串比较；
    # 结点穿越 t=1 自然排在同段段内事件之后
    violations.sort(key=lambda x: (
        x.segment_index if x.segment_index is not None else 0,
        Fraction(x.t) if x.t is not None else Fraction(0)))
    crossings.sort(key=lambda c: (c.segment_index, Fraction(c.t)))
    legal = not violations
    first = violations[0] if violations else None

    room_chain: list[str] = []
    for stay in stays:
        if not room_chain or room_chain[-1] != stay.room_id:
            room_chain.append(stay.room_id)

    return RouteResult(
        route_id=route_id, legal=legal, original_points=original,
        first_violation=first, violations=violations,
        stays=stays, crossings=crossings, room_chain=room_chain)


def _room_of_interval(ana: SegmentAnalysis, t: Fraction,
                      side: Literal["left", "right"]) -> str | None:
    """事件 t 左/右相邻开区间所在房间。"""
    for lo, hi, rid in ana.interior_rooms:
        if side == "left" and hi == t:
            return rid
        if side == "right" and lo == t:
            return rid
    return None


def _endpoint_rooms(ana: SegmentAnalysis, side: int) -> list[str]:
    """线段端点（t=0 或 t=1）紧邻的内部房间（贴墙/外部时为空）。

    驻段是开区间：t=0 端点只能与某区间的 lo 相接，t=1 端点只能与 hi 相接。
    """
    t = Fraction(side)
    out = []
    for lo, hi, rid in ana.interior_rooms:
        if (side == 0 and lo == t) or (side == 1 and hi == t):
            out.append(rid)
    return out


def _other_room_at_boundary(version: PlanVersion, p: Point,
                            room_id: str) -> str | None:
    """边界点 p（已知在 room_id 边界上）若也在另一房间的边界上，返回该房间。"""
    for rid, rect in version.rooms.items():
        if rid != room_id and rect.contains(p):
            return rid
    return None


def _judge_event(version: PlanVersion, seg_idx: int, t: Fraction, p: Point,
                 room_l: str | None, room_r: str | None,
                 violations: list[Violation],
                 crossings: list[Crossing],
                 at_node: bool = False) -> Violation | None:
    """裁决事件点：合法则登记穿越，否则返回违规。"""
    if room_l is None or room_r is None:
        return Violation(WALL_CONTACT, p.as_pair(),
                         "路径孤立接触边界但未穿越到另一房间（擦墙/触墙）",
                         seg_idx, str(t))
    if room_l == room_r:
        return Violation(WALL_CONTACT, p.as_pair(),
                         f"路径在房间 {room_l} 的墙上接触后返回原房间，未穿过任何门",
                         seg_idx, str(t))
    gates, edge, reason = _edge_gates_at(version, room_l, room_r, p)
    if reason is not None:
        detail = {
            POINT_CONTACT_NO_SHARED_EDGE:
                f"{room_l} 与 {room_r} 之间无正长度共边（四角相遇的点接触不能设门）",
            GATE_FRAME_ENDPOINT:
                "命中门框端点，开放门段不含端点，不可通行",
            NO_GATE_ON_EDGE:
                f"{room_l}/{room_r} 共边上该坐标没有任何开放门段",
        }[reason]
        return Violation(reason, p.as_pair(), detail, seg_idx, str(t))
    primary = gates[0]
    crossings.append(Crossing(
        primary.gate_id, p.as_pair(), room_l, room_r, seg_idx, str(t),
        tuple(g.gate_id for g in gates), at_node))
    return None


def _classify_node(version: PlanVersion, node_idx: int, p: Point,
                   rooms_l: list[str], rooms_r: list[str],
                   violations: list[Violation],
                   crossings: list[Crossing]) -> NodeKind:
    """内部折线结点分类（起终点不在此处理）。

    先看点本身的几何位置，再看前后段相邻驻段，避免把墙上的点
    误当成“同房内部”。结点在门上时，前后两段在此合并成一次穿越。
    """
    seg_idx = node_idx - 1
    strict_here = [rid for rid, rect in version.rooms.items()
                   if rect.strict_contains(p)]
    rl = rooms_l[0] if rooms_l else None
    rr = rooms_r[0] if rooms_r else None

    # 1) 点本身在某房间严格内部：只能是室内转折，两侧必须同为该房间
    if strict_here:
        if len(strict_here) == 1 and rl == rr == strict_here[0]:
            return NodeKind.INTERIOR
        violations.append(Violation(
            ROOM_OVERLAP_ANOMALY, p.as_pair(),
            f"结点位于多个房间内部或与相邻驻段矛盾: {strict_here}",
            seg_idx, "1"))
        return NodeKind.ILLEGAL

    # 2) 点不在任何房间内部 => 墙上或外部。两侧必须是不同房间，
    #    且点必须落在两室共边的开放门段上，否则一律拒绝
    if rl is None or rr is None:
        violations.append(Violation(
            WALL_CONTACT, p.as_pair(),
            "折线结点位于墙上或房间外，且至少一侧没有房间严格内部"
            "（沿墙/擦墙返回/越界）", seg_idx, "1"))
        return NodeKind.ILLEGAL
    if rl == rr:
        violations.append(Violation(
            WALL_CONTACT, p.as_pair(),
            f"折线结点在房间 {rl} 的边界上转折后返回同一房间（擦墙返回）",
            seg_idx, "1"))
        return NodeKind.ILLEGAL

    v = _judge_event(version, seg_idx, Fraction(1), p, rl, rr,
                     violations, crossings, at_node=True)
    if v is not None:
        violations.append(v)
        return NodeKind.ILLEGAL
    return NodeKind.CROSSING
