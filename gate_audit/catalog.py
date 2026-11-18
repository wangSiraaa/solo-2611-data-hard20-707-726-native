"""独立展馆的分区 / 门段目录事实库。

职责：
1. 载入并核验平面版本：房间正面积、内部互不重叠；
2. 独立核验每扇门——其声明的两个房间必须真正共享一段 **正长度边**，
   门必须落在该共边之内；仅点接触的两室不能设门；
3. 维护“带所有权的边段索引”：(房间对, 共边) 与其上的开放门段；
4. 支持门段修订（replace_gate），旧版本完整保留、永不就地覆盖；
5. 提供独立的共边门查询（不依赖任何路线）。

SQLite 只做持久化；所有几何判定在内存中用精确分数完成。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .geometry import Point, Rect, SharedEdge, merge_open_union, shared_edge

SCHEMA = """
CREATE TABLE IF NOT EXISTS plan_versions (
    revision     TEXT PRIMARY KEY,
    base_revision TEXT,
    content_hash TEXT NOT NULL,
    created_seq  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS rooms (
    revision TEXT NOT NULL,
    room_id  TEXT NOT NULL,
    x0 TEXT NOT NULL, y0 TEXT NOT NULL,
    x1 TEXT NOT NULL, y1 TEXT NOT NULL,
    PRIMARY KEY (revision, room_id)
);
CREATE TABLE IF NOT EXISTS gates (
    revision TEXT NOT NULL,
    gate_id  TEXT NOT NULL,
    room_a   TEXT NOT NULL,
    room_b   TEXT NOT NULL,
    orientation TEXT NOT NULL,
    fixed TEXT NOT NULL,
    lo TEXT NOT NULL,
    hi TEXT NOT NULL,
    PRIMARY KEY (revision, gate_id)
);
CREATE TABLE IF NOT EXISTS shared_edges (
    revision TEXT NOT NULL,
    room_a TEXT NOT NULL,
    room_b TEXT NOT NULL,
    orientation TEXT NOT NULL,
    fixed TEXT NOT NULL,
    lo TEXT NOT NULL,
    hi TEXT NOT NULL,
    PRIMARY KEY (revision, room_a, room_b)
);
CREATE TABLE IF NOT EXISTS edge_gate_index (
    revision TEXT NOT NULL,
    room_a TEXT NOT NULL,
    room_b TEXT NOT NULL,
    gate_id TEXT NOT NULL,
    lo TEXT NOT NULL,
    hi TEXT NOT NULL,
    PRIMARY KEY (revision, room_a, room_b, gate_id)
);
CREATE TABLE IF NOT EXISTS reports (
    report_id TEXT PRIMARY KEY,
    revision  TEXT NOT NULL,
    content_hash TEXT NOT NULL,   -- 归一化请求内容的哈希，异文据此拒绝
    payload_json TEXT NOT NULL,   -- 完整报告（含门证据快照）
    created_seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS credentials (
    credential_id TEXT PRIMARY KEY,
    revision TEXT NOT NULL,
    report_id TEXT NOT NULL,
    route_id TEXT NOT NULL,
    room_chain_json TEXT NOT NULL,
    gate_chain_json TEXT NOT NULL
);
"""


class CatalogError(ValueError):
    """平面或门段目录本身不合法（与路线无关）。"""


@dataclass(frozen=True, slots=True)
class Gate:
    gate_id: str
    room_a: str
    room_b: str
    orientation: str  # "v"/"h"
    fixed: Fraction
    lo: Fraction
    hi: Fraction

    def __post_init__(self) -> None:
        if self.hi <= self.lo:
            raise CatalogError(f"门 {self.gate_id} 必须是正长度开放线段")

    def open_contains(self, v: Fraction) -> bool:
        """变化坐标 v 是否落在门的 **开段** (lo,hi) 内；门框端点返回 False。"""
        return self.lo < v < self.hi


@dataclass(frozen=True, slots=True)
class PlanVersion:
    revision: str
    rooms: dict[str, Rect]
    gates: dict[str, Gate]
    edges: dict[tuple[str, str], SharedEdge]
    content_hash: str
    base_revision: str | None


def _norm_gate(raw: dict) -> Gate:
    rooms = list(raw["rooms"])
    if len(rooms) != 2 or rooms[0] == rooms[1]:
        raise CatalogError(f"门 {raw.get('gate_id')} 必须声明两个不同房间")
    axis = raw["axis"]
    if axis == "y":
        orientation = "v"  # 沿 y 变化 -> 竖直边
    elif axis == "x":
        orientation = "h"  # 沿 x 变化 -> 水平边
    else:
        raise CatalogError(f"门 {raw.get('gate_id')} 的 axis 只能是 x 或 y")
    lo, hi = Fraction(raw["lo"]), Fraction(raw["hi"])
    return Gate(str(raw["gate_id"]), rooms[0], rooms[1], orientation,
                Fraction(raw["fixed"]), lo, hi)


def _canonical_rooms(rooms: dict[str, Rect]) -> list:
    return [[rid, [str(r.x0), str(r.y0), str(r.x1), str(r.y1)]]
            for rid, r in sorted(rooms.items())]


def _canonical_gates(gates: dict[str, Gate]) -> list:
    return [[g.gate_id, [g.room_a, g.room_b], g.orientation,
             str(g.fixed), str(g.lo), str(g.hi)]
            for g in sorted(gates.values(), key=lambda g: g.gate_id)]


def plan_content_hash(rooms: dict[str, Rect], gates: dict[str, Gate]) -> str:
    body = json.dumps([_canonical_rooms(rooms), _canonical_gates(gates)],
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def build_version(revision: str, rooms: dict[str, Rect], gates: dict[str, Gate],
                  base_revision: str | None) -> PlanVersion:
    """对一组房间+门做全部目录级核验并构建不可变版本。"""
    if not rooms:
        raise CatalogError("平面至少需要一个房间")
    # 1) 内部不得重叠（边界重合合法）
    ids = sorted(rooms)
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = rooms[ids[i]], rooms[ids[j]]
            if a.interior_overlaps(b):
                raise CatalogError(
                    f"房间 {ids[i]} 与 {ids[j]} 的内部发生正面积重叠")
    # 2) 预编译所有正长度共边（带所有权的房间对）
    edges: dict[tuple[str, str], SharedEdge] = {}
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            edge = shared_edge(rooms[ids[i]], rooms[ids[j]])
            if edge is not None:
                edges[(ids[i], ids[j])] = edge
    # 3) 逐门独立核验：必须落在声明两室的真实共边上
    for gate in gates.values():
        if gate.room_a not in rooms or gate.room_b not in rooms:
            raise CatalogError(f"门 {gate.gate_id} 引用了不存在的房间")
        key = tuple(sorted((gate.room_a, gate.room_b)))
        edge = edges.get(key)
        if edge is None:
            # 仅点接触 / 无接触：明确报“无正长度共边”
            raise CatalogError(
                f"门 {gate.gate_id} 的两室 {gate.room_a}/{gate.room_b} "
                    "只有点接触或不相邻，不存在正长度共边，不能设门")
        if gate.orientation != edge.orientation or gate.fixed != edge.fixed:
            raise CatalogError(
                f"门 {gate.gate_id} 不在 {gate.room_a}/{gate.room_b} 的共享边上")
        if not (edge.lo <= gate.lo and gate.hi <= edge.hi):
            raise CatalogError(
                f"门 {gate.gate_id} 开段 {gate.lo}..{gate.hi} 超出共边 "
                f"{edge.lo}..{edge.hi}")
    return PlanVersion(revision, dict(rooms), dict(gates), edges,
                       plan_content_hash(rooms, gates), base_revision)


class Catalog:
    """加载、修订并持久化平面版本的事实库。"""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._cache: dict[str, PlanVersion] = {}

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Catalog":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 载入与修订
    # ------------------------------------------------------------------
    def load_plan_file(self, path: str | Path) -> PlanVersion:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        revision = str(data["revision"])
        rooms = {str(r["room_id"]): Rect.make(r["rect"]) for r in data["rooms"]}
        gates = {g["gate_id"]: _norm_gate(g) for g in data["gates"]}
        version = build_version(revision, rooms, gates, None)
        self._persist(version)
        self._cache[revision] = version
        return version

    def apply_revision_file(self, path: str | Path,
                            base: PlanVersion) -> PlanVersion:
        """门段更正：替换同 gate_id 的门，生成新版本；旧版本不动。"""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        revision = str(data["revision"])
        if revision == base.revision:
            raise CatalogError("修订版号不得与被修订版本相同")
        new_gate = _norm_gate(data["replace_gate"])
        gates = dict(base.gates)
        if new_gate.gate_id not in gates:
            raise CatalogError(
                f"修订试图替换不存在的门 {new_gate.gate_id}")
        gates[new_gate.gate_id] = new_gate
        version = build_version(revision, dict(base.rooms), gates,
                                base.revision)
        self._persist(version)
        self._cache[revision] = version
        return version

    def get_version(self, revision: str) -> PlanVersion:
        if revision in self._cache:
            return self._cache[revision]
        cur = self.conn.execute(
            "SELECT revision, base_revision, content_hash FROM plan_versions "
            "WHERE revision=?", (revision,))
        row = cur.fetchone()
        if row is None:
            raise KeyError(f"未知平面版本: {revision}")
        rooms = {rid: Rect(Fraction(x0), Fraction(y0), Fraction(x1), Fraction(y1))
                 for rid, x0, y0, x1, y1 in self.conn.execute(
                     "SELECT room_id,x0,y0,x1,y1 FROM rooms WHERE revision=?",
                     (revision,))}
        gates = {}
        for gid, ra, rb, ori, fx, lo, hi in self.conn.execute(
                "SELECT gate_id,room_a,room_b,orientation,fixed,lo,hi "
                "FROM gates WHERE revision=?", (revision,)):
            gates[gid] = Gate(gid, ra, rb, ori, Fraction(fx),
                              Fraction(lo), Fraction(hi))
        version = build_version(revision, rooms, gates, row[1])
        if version.content_hash != row[2]:
            raise CatalogError(f"版本 {revision} 持久化内容校验失败")
        self._cache[revision] = version
        return version

    def _persist(self, v: PlanVersion) -> None:
        if self.conn.execute("SELECT 1 FROM plan_versions WHERE revision=?",
                             (v.revision,)).fetchone():
            raise CatalogError(f"平面版本 {v.revision} 已存在且不可变")
        seq = self.conn.execute(
            "SELECT COALESCE(MAX(created_seq),0)+1 FROM plan_versions").fetchone()[0]
        self.conn.execute(
            "INSERT INTO plan_versions VALUES (?,?,?,?)",
            (v.revision, v.base_revision, v.content_hash, seq))
        self.conn.executemany(
            "INSERT INTO rooms VALUES (?,?,?,?,?,?)",
            [(v.revision, rid, str(r.x0), str(r.y0), str(r.x1), str(r.y1))
             for rid, r in sorted(v.rooms.items())])
        self.conn.executemany(
            "INSERT INTO gates VALUES (?,?,?,?,?,?,?,?)",
            [(v.revision, g.gate_id, g.room_a, g.room_b, g.orientation,
              str(g.fixed), str(g.lo), str(g.hi))
             for g in sorted(v.gates.values(), key=lambda g: g.gate_id)])
        self.conn.executemany(
            "INSERT INTO shared_edges VALUES (?,?,?,?,?,?,?)",
            [(v.revision, a, b, e.orientation, str(e.fixed),
              str(e.lo), str(e.hi))
             for (a, b), e in sorted(v.edges.items())])
        self.conn.executemany(
            "INSERT INTO edge_gate_index VALUES (?,?,?,?,?,?)",
            [(v.revision, *sorted((g.room_a, g.room_b)), g.gate_id,
              str(g.lo), str(g.hi))
             for g in v.gates.values()])
        self.conn.commit()

    # ------------------------------------------------------------------
    # 独立共边 / 门查询（路线核查之外可单独调用）
    # ------------------------------------------------------------------
    def shared_edge_of(self, version: PlanVersion, room_a: str,
                       room_b: str) -> SharedEdge | None:
        return version.edges.get(tuple(sorted((room_a, room_b))))

    def gates_on_edge(self, version: PlanVersion, room_a: str,
                      room_b: str) -> list[Gate]:
        """返回两室共边上的全部门（按 gate_id 排序）。"""
        key = tuple(sorted((room_a, room_b)))
        if key not in version.edges:
            return []
        return sorted((g for g in version.gates.values()
                       if tuple(sorted((g.room_a, g.room_b))) == key),
                      key=lambda g: g.gate_id)

    def edge_query(self, version: PlanVersion, room_a: str,
                   room_b: str) -> dict:
        """对外的独立共边门查询：共边几何 + 门开段 + 开集并。"""
        key = tuple(sorted((room_a, room_b)))
        edge = version.edges.get(key)
        if edge is None:
            # 区分“点接触”与“完全不相邻”，便于事实核查
            ra = version.rooms.get(room_a)
            rb = version.rooms.get(room_b)
            reason = "UNKNOWN_ROOM"
            if ra is not None and rb is not None:
                reason = "POINT_CONTACT_OR_NO_CONTACT"
            return {"room_a": room_a, "room_b": room_b,
                    "has_positive_shared_edge": False, "reason": reason,
                    "gates": [], "open_union": []}
        gates = self.gates_on_edge(version, room_a, room_b)
        union = merge_open_union((g.lo, g.hi) for g in gates)
        return {
            "room_a": key[0], "room_b": key[1],
            "has_positive_shared_edge": True,
            "edge": {"orientation": edge.orientation, "fixed": str(edge.fixed),
                     "lo": str(edge.lo), "hi": str(edge.hi),
                     "length": str(edge.length)},
            "gates": [{"gate_id": g.gate_id, "lo": str(g.lo),
                       "hi": str(g.hi),
                       "open_segment": f"({g.lo},{g.hi})"} for g in gates],
            "open_union": [[str(a), str(b)] for a, b in union],
        }
