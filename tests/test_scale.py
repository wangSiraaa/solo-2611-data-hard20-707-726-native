"""规模上限与持久化集成测试（合成平面，不含实体门禁语义）。

构造 5x6 = 30 个矩形房间、最多 80 扇门、200 条路线（每条最多 100 段），
验证：
* 上限规模下核查在合理时间内完成；
* SQLite 重开后版本、报告、凭据均可读且内容一致；
* 同 report_id 复读返回 replayed=True，异文拒绝；
* 独立共边门查询与路线查询是两套独立入口。
"""
from __future__ import annotations

import json
import random
import tempfile
import time
import unittest
from pathlib import Path

from gate_audit.catalog import Catalog, build_version
from gate_audit.catalog import Gate
from gate_audit.reports import AuditStore, ReportError
from fractions import Fraction


def _build_grid_plan(rng: random.Random):
    cols, rows = 5, 6
    rooms, rects = {}, {}
    for r in range(rows):
        for c in range(cols):
            rid = f"R{r:02d}{c:02d}"
            rect = (c * 4, r * 4, c * 4 + 4, r * 4 + 4)
            rooms[rid] = rect
            rects[rid] = rect

    # 网格内部相邻对共 2*cols*rows-cols-rows = 49 对，每对放 1~2 扇门，
    # 总量控制在 80 以内
    pairs = []
    for r in range(rows):
        for c in range(cols):
            rid = f"R{r:02d}{c:02d}"
            if c + 1 < cols:
                pairs.append((rid, f"R{r:02d}{c + 1:02d}", "v"))
            if r + 1 < rows:
                pairs.append((rid, f"R{r + 1:02d}{c:02d}", "h"))
    rng.shuffle(pairs)
    gates, gate_no = {}, 0
    for a, b, ori in pairs:
        if gate_no >= 80:
            break
        # 共边是全局坐标：在边的内 1/4 与 3/4 处取开段，避开门框端点
        if ori == "v":
            fixed = Fraction(rooms[a][2])       # a 的右边界 x
            base = Fraction(rooms[a][1])        # 共边 y 的下界
        else:
            fixed = Fraction(rooms[a][3])       # a 的上边界 y
            base = Fraction(rooms[a][0])        # 共边 x 的下界
        lo, hi = base + 1, base + 3
        gates[f"G{gate_no:03d}"] = Gate(
            f"G{gate_no:03d}", a, b, ori, fixed, lo, hi)
        gate_no += 1
    return rooms, gates


def _random_legal_walk(rng: random.Random, rooms: dict, gates: dict):
    """沿“房间-门-房间”邻接随机游走生成一条保证合法的折线（结点在门中点）。"""
    room_ids = sorted(rooms)
    rid = rng.choice(room_ids)
    x0, y0, x1, y1 = rooms[rid]
    points = [[(x0 + x1) / 2, (y0 + y1) / 2]]
    # 每次过门追加“门点 + 新房间中心”两个点 = 两段；最多 100 段
    for _ in range(50):
        options = [g for g in gates.values()
                   if rid in (g.room_a, g.room_b)]
        if not options:
            break
        g = rng.choice(options)
        nxt = g.room_b if g.room_a == rid else g.room_a
        nx0, ny0, nx1, ny1 = rooms[nxt]
        mid = (g.lo + g.hi) / 2
        if g.orientation == "v":  # 竖边 x=fixed，沿 y
            door = [g.fixed, mid]
        else:
            door = [mid, g.fixed]
        points.append(door)                       # 门结点：前后段合并解释
        points.append([(nx0 + nx1) / 2, (ny0 + ny1) / 2])
        rid = nxt
    return points


class ScaleAndPersistenceTests(unittest.TestCase):
    def test_scale_persistence_replay_and_divergence(self):
        rng = random.Random(733)
        rooms_raw, gates = _build_grid_plan(rng)
        self.assertEqual(len(rooms_raw), 30)
        self.assertLessEqual(len(gates), 80)

        from gate_audit.geometry import Rect
        rooms = {rid: Rect.make(rect) for rid, rect in rooms_raw.items()}
        version = build_version("big", rooms, gates, None)

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "big.sqlite"
            cat = Catalog(db)
            cat._persist(version)
            store = AuditStore(cat)

            routes = []
            walk_rng = random.Random(2026)  # 独立随机源，避免与建平面的 rng 耦合
            for i in range(200):
                pts = _random_legal_walk(walk_rng, rooms_raw, gates)
                if len(pts) < 2:
                    continue
                routes.append({"route_id": f"W{i:03d}", "points": pts})
            self.assertEqual(len(routes), 200)
            self.assertTrue(all(
                len(r["points"]) <= 101 for r in routes))  # 100 段 = 101 点

            t0 = time.perf_counter()
            batch = store.audit_batch("big", routes, "BIG1")
            elapsed = time.perf_counter() - t0
            self.assertLess(elapsed, 30.0)
            # 生成器刻意只走合法门：全部路线应合法并签发凭据
            self.assertTrue(all(r["legal"] for r in batch["routes"]))
            self.assertEqual(len(store.list_credentials()), 200)

            # 复读：同内容
            again = store.audit_batch("big", routes, "BIG1")
            self.assertTrue(again["replayed"])
            self.assertEqual(len(store.list_credentials()), 200)

            # 异文拒绝
            tampered = [dict(r) for r in routes]
            tampered[0] = {"route_id": "W000",
                           "points": routes[0]["points"][:-1]}
            with self.assertRaises(ReportError):
                store.audit_batch("big", tampered, "BIG1")

            # 独立共边门查询
            g0 = next(iter(gates.values()))
            q = store.edge_query("big", g0.room_a, g0.room_b)
            self.assertTrue(q["has_positive_shared_edge"])
            self.assertTrue(q["gates"])

            # SQLite 重开：版本/报告/凭据完整可读
            cat.close()
            cat2 = Catalog(db)
            store2 = AuditStore(cat2)
            reloaded = store2.read_report("BIG1")
            self.assertEqual(reloaded["plan_content_hash"],
                             version.content_hash)
            self.assertEqual(len(reloaded["routes"]), 200)
            self.assertEqual(len(store2.list_credentials()), 200)
            cat2.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
