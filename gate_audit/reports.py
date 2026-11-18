"""核查报告与通行凭据的事实库。

版本 / 复读语义：
* 每份 **批次报告** 冻结其平面门版本、门证据快照、原路线文件中的全部折线；
  之后门修订不影响旧报告（read_report 永远取出当时的门证据）；
* 同一 report_id：归一化批次内容一致 => 原样复读（replayed=True，不重发凭据）；
  内容不一致 => 拒绝（异文拒绝），不得覆盖既有报告；
* 门修订产生新版本后重新核查会得到新结论，必须使用新的 report_id；
* 只有整条路线合法时才写入 credentials；失败路线不产生任何合法通行凭据。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict
from typing import Any

from .catalog import Catalog, PlanVersion
from .geometry import Point
from .verifier import RouteResult, verify_route

# 逐路线结果（批次报告的组成部分，单独建表便于按路线查询）
_EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS route_results (
    report_id TEXT NOT NULL,
    route_id  TEXT NOT NULL,
    revision  TEXT NOT NULL,
    legal     INTEGER NOT NULL,
    result_json TEXT NOT NULL,
    PRIMARY KEY (report_id, route_id)
);
"""


class ReportError(ValueError):
    """报告编号冲突 / 异文拒绝等。"""


def _canonical_routes(routes: list[tuple[str, list[list[str]]]]) -> str:
    return json.dumps(routes, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def batch_hash(revision: str,
               routes: list[tuple[str, list[list[str]]]]) -> str:
    return hashlib.sha256(
        _canonical_routes([[revision, routes]]).encode("utf-8")
    ).hexdigest()


def _gate_snapshot(version: PlanVersion) -> list[dict[str, Any]]:
    """冻结报告所依据的门证据：每扇门的开段与所属共边。"""
    snap = []
    for g in sorted(version.gates.values(), key=lambda g: g.gate_id):
        key = tuple(sorted((g.room_a, g.room_b)))
        edge = version.edges[key]
        snap.append({
            "gate_id": g.gate_id,
            "rooms": [g.room_a, g.room_b],
            "orientation": g.orientation,
            "fixed": str(g.fixed),
            "open_segment": [str(g.lo), str(g.hi)],
            "frame_endpoints": [str(g.lo), str(g.hi)],
            "shared_edge": {
                "orientation": edge.orientation,
                "fixed": str(edge.fixed),
                "lo": str(edge.lo), "hi": str(edge.hi)},
        })
    return snap


def serialize_result(result: RouteResult) -> dict[str, Any]:
    return {
        "route_id": result.route_id,
        "legal": result.legal,
        "original_polyline": result.original_points,
        "first_violation": (asdict(result.first_violation)
                            if result.first_violation else None),
        "violations": [asdict(v) for v in result.violations],
        "room_stays": [asdict(s) for s in result.stays],
        "crossings": [asdict(c) for c in result.crossings],
        "room_chain": result.room_chain,
        "credential_id": result.credential_id,
    }


class AuditStore:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        self.conn = catalog.conn
        self.conn.executescript(_EXTRA_SCHEMA)
        self.conn.commit()

    # ------------------------------------------------------------------
    def audit_batch(self, revision: str, raw_routes: list[dict],
                    report_id: str) -> dict[str, Any]:
        """整条路线批次查询：逐线核查，整体落库；处理复读与异文拒绝。"""
        version = self.catalog.get_version(revision)
        # 归一化原折线（精确分数的规范字符串），复读判定与输入表示无关
        canon: list[tuple[str, list[list[str]]]] = []
        for route in raw_routes:
            pts = [Point.make(p).as_pair() for p in route["points"]]
            canon.append((str(route["route_id"]), pts))
        chash = batch_hash(revision, canon)

        row = self.conn.execute(
            "SELECT payload_json FROM reports WHERE report_id=?",
            (report_id,)).fetchone()
        if row is not None:
            stored = json.loads(row[0])
            if stored["request_hash"] != chash:
                raise ReportError(
                    f"report_id={report_id} 已绑定另一份请求内容，"
                    "异文拒绝：同一编号不得承载不同平面版本或折线")
            stored["replayed"] = True
            return stored

        route_payloads: list[dict[str, Any]] = []
        for route_id, points in canon:
            result = verify_route(version, route_id, points)
            if result.legal:
                result.credential_id = "CRED-" + hashlib.sha256(
                    f"{revision}|{report_id}|{route_id}|{chash}".encode()
                ).hexdigest()[:16]
                self.conn.execute(
                    "INSERT INTO credentials VALUES (?,?,?,?,?,?)",
                    (result.credential_id, revision, report_id, route_id,
                     json.dumps(result.room_chain, ensure_ascii=False),
                     json.dumps([asdict(c) for c in result.crossings],
                                ensure_ascii=False)))
            payload = serialize_result(result)
            route_payloads.append(payload)
            self.conn.execute(
                "INSERT INTO route_results VALUES (?,?,?,?,?)",
                (report_id, route_id, revision, int(result.legal),
                 json.dumps(payload, ensure_ascii=False)))

        batch = {
            "replayed": False,
            "report_id": report_id,
            "plan_revision": revision,
            "plan_content_hash": version.content_hash,
            "request_hash": chash,
            "routes": route_payloads,
            "gate_evidence": _gate_snapshot(version),
        }
        seq = self.conn.execute(
            "SELECT COALESCE(MAX(created_seq),0)+1 FROM reports").fetchone()[0]
        self.conn.execute(
            "INSERT INTO reports (report_id,revision,content_hash,payload_json,created_seq)"
            " VALUES (?,?,?,?,?)",
            (report_id, revision, chash,
             json.dumps(batch, ensure_ascii=False), seq))
        self.conn.commit()
        return batch

    def audit_route(self, revision: str, route_id: str, raw_points: list,
                    report_id: str) -> dict[str, Any]:
        """单条路线查询（外部 API）：包装成单元素批次。"""
        return self.audit_batch(
            revision, [{"route_id": route_id, "points": raw_points}],
            report_id)

    def read_report(self, report_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT payload_json FROM reports WHERE report_id=?",
            (report_id,)).fetchone()
        if row is None:
            raise ReportError(f"报告不存在: {report_id}")
        payload = json.loads(row[0])
        payload["replayed"] = True
        return payload

    def edge_query(self, revision: str, room_a: str, room_b: str) -> dict:
        """独立共边门查询（与路线核查完全解耦）。"""
        version = self.catalog.get_version(revision)
        return self.catalog.edge_query(version, room_a, room_b)

    def list_credentials(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT credential_id,revision,report_id,route_id,"
            "room_chain_json,gate_chain_json FROM credentials ORDER BY credential_id")]
