"""事实库核查规则的单元 / 集成测试。

覆盖任务书要求的补充情形：
门框端点、四角相遇、折线结点在门上、重叠门开集并、沿墙/擦墙/等待、
门修订后重开结论变化、报告复读与异文拒绝、独立共边门查询、有理坐标。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from gate_audit.catalog import Catalog, CatalogError
from gate_audit.geometry import Point, Rect, merge_open_union, shared_edge
from gate_audit.reports import AuditStore, ReportError
from gate_audit.verifier import (GATE_FRAME_ENDPOINT, NO_GATE_ON_EDGE,
                                 OUTSIDE_PLAN, POINT_CONTACT_NO_SHARED_EDGE,
                                 WALK_ALONG_WALL, WALL_CONTACT,
                                 ZERO_LENGTH_SEGMENT, verify_route)

ROOT = Path(__file__).resolve().parent.parent
MATERIALS = ROOT / "materials" / "733"


def load_ver(catalog: Catalog, path: str = "plan.json"):
    return catalog.load_plan_file(MATERIALS / path)


class GeometryTests(unittest.TestCase):
    def test_open_union_does_not_fill_touch_point(self):
        # (1,2) 与 (2,3) 只在端点 2 相接：开集并不得把 2 填成可通行
        u = merge_open_union([(Fraction(1), Fraction(2)),
                              (Fraction(2), Fraction(3))])
        self.assertEqual(u, [(Fraction(1), Fraction(2)),
                             (Fraction(2), Fraction(3))])
        # 真正重叠才合并
        u2 = merge_open_union([(Fraction(1), Fraction(2.5)),
                               (Fraction(2), Fraction(3))])
        self.assertEqual(u2, [(Fraction(1), Fraction(3))])

    def test_point_contact_is_not_shared_edge(self):
        a = Rect.make([0, 0, 4, 4])
        d = Rect.make([4, 4, 8, 8])
        self.assertIsNone(shared_edge(a, d))  # 仅角点 (4,4) 接触
        b = Rect.make([4, 0, 8, 4])
        edge = shared_edge(a, b)
        self.assertEqual(edge.orientation, "v")
        self.assertEqual(edge.length, 4)

    def test_rational_parse(self):
        p = Point.make(["1/3", "0.25"])
        self.assertEqual(p.x, Fraction(1, 3))
        self.assertEqual(p.y, Fraction(1, 4))


class CatalogValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cat = Catalog(Path(self.tmp.name) / "t.sqlite")

    def tearDown(self):
        self.cat.close()
        self.tmp.cleanup()

    def test_interior_overlap_rejected(self):
        rooms = {"A": Rect.make([0, 0, 4, 4]),
                 "B": Rect.make([2, 2, 6, 6])}
        with self.assertRaises(CatalogError):
            from gate_audit.catalog import build_version
            build_version("x", rooms, {}, None)

    def test_gate_on_point_contact_rejected(self):
        rooms = {"A": Rect.make([0, 0, 4, 4]),
                 "D": Rect.make([4, 4, 8, 8])}
        from gate_audit.catalog import Gate, build_version
        bad = Gate("AD", "A", "D", "v", Fraction(4),
                   Fraction(4), Fraction(5))
        with self.assertRaises(CatalogError):
            build_version("x", rooms, {"AD": bad}, None)

    def test_gate_exceeding_edge_rejected(self):
        rooms = {"A": Rect.make([0, 0, 4, 4]),
                 "B": Rect.make([4, 0, 8, 4])}
        from gate_audit.catalog import Gate, build_version
        bad = Gate("AB", "A", "B", "v", Fraction(4),
                   Fraction(2), Fraction(5))  # 共边 y∈[0,4]，5 越界
        with self.assertRaises(CatalogError):
            build_version("x", rooms, {"AB": bad}, None)


class MaterialPipelineTests(unittest.TestCase):
    """对 materials/733 的端到端事实核查。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cat = Catalog(Path(self.tmp.name) / "t.sqlite")
        self.store = AuditStore(self.cat)
        self.v1 = load_ver(self.cat)
        self.routes = json.loads(
            (MATERIALS / "routes.json").read_text("utf-8"))["routes"]

    def tearDown(self):
        self.cat.close()
        self.tmp.cleanup()

    def _audit(self, rev, report_id):
        return self.store.audit_batch(rev, self.routes, report_id)

    def test_r1_verdicts_match_readme(self):
        r1 = self._audit("r1", "R1")
        via_b, corner = r1["routes"]
        self.assertTrue(via_b["legal"])
        self.assertEqual(via_b["room_chain"], ["A", "B", "D"])
        self.assertEqual([c["gate_id"] for c in via_b["crossings"]],
                         ["AB", "BD"])
        self.assertIsNotNone(via_b["credential_id"])
        self.assertFalse(corner["legal"])
        self.assertIsNone(corner["credential_id"])
        self.assertEqual(corner["first_violation"]["reason"],
                         POINT_CONTACT_NO_SHARED_EDGE)
        self.assertEqual(corner["first_violation"]["point"], ["4", "4"])

    def test_r2_frame_endpoint_changes_verdict_but_r1_frozen(self):
        self._audit("r1", "R1")
        self.cat.apply_revision_file(MATERIALS / "revision.json", self.v1)
        r2 = self._audit("r2", "R2")
        via_b = r2["routes"][0]
        self.assertFalse(via_b["legal"])
        self.assertEqual(via_b["first_violation"]["reason"],
                         GATE_FRAME_ENDPOINT)
        self.assertEqual(via_b["first_violation"]["point"], ["4", "2"])
        self.assertIsNone(via_b["credential_id"])
        # 旧报告保持原门证据：AB 仍是 (1,3)
        frozen = self.store.read_report("R1")
        ab = next(g for g in frozen["gate_evidence"] if g["gate_id"] == "AB")
        self.assertEqual(ab["open_segment"], ["1", "3"])
        self.assertTrue(frozen["routes"][0]["legal"])

    def test_replay_same_content_and_reject_divergent_text(self):
        self._audit("r1", "R1")
        again = self._audit("r1", "R1")
        self.assertTrue(again["replayed"])
        # 同一 report_id 承载不同折线 -> 异文拒绝
        tampered = [{"route_id": "VIA_B",
                     "points": [[2, 2], [6, 2], [6, 7]]},
                    self.routes[1]]
        with self.assertRaises(ReportError):
            self.store.audit_batch("r1", tampered, "R1")

    def test_no_credential_for_failed_batch_routes(self):
        self._audit("r1", "R1")
        creds = self.store.list_credentials()
        self.assertEqual([c["route_id"] for c in creds], ["VIA_B"])

    def test_independent_edge_query(self):
        q = self.store.edge_query("r1", "A", "D")
        self.assertFalse(q["has_positive_shared_edge"])
        q2 = self.store.edge_query("r1", "B", "A")
        self.assertTrue(q2["has_positive_shared_edge"])
        self.assertEqual([g["gate_id"] for g in q2["gates"]], ["AB"])
        self.assertEqual(q2["open_union"], [["1", "3"]])


class RouteRuleTests(unittest.TestCase):
    """自定义平面上的逐规则核查。

    平面：A=[0,4]x[0,4]  B=[4,8]x[0,4]  C=[4,8]x[4,8]
    门：G1 竖边 x=4, y∈(1,3)；G2/G3 重叠开段 y∈(5,7) 与 y∈(6,8)。
    """

    def setUp(self):
        from gate_audit.catalog import Gate, build_version
        rooms = {"A": Rect.make([0, 0, 4, 4]),
                 "B": Rect.make([4, 0, 8, 4]),
                 "C": Rect.make([4, 4, 8, 8])}
        gates = {
            "G1": Gate("G1", "A", "B", "v", Fraction(4),
                       Fraction(1), Fraction(3)),
            "G2": Gate("G2", "B", "C", "h", Fraction(4),
                       Fraction(5), Fraction(7)),
            "G3": Gate("G3", "B", "C", "h", Fraction(4),
                       Fraction(6), Fraction(8)),
        }
        self.version = build_version("t", rooms, gates, None)

    def test_node_exactly_on_gate_counts_once(self):
        # 折线结点 (4,2) 恰在 G1 开段上：合并为一次穿越
        r = verify_route(self.version, "N",
                         [[2, 2], [4, 2], [6, 2]])
        self.assertTrue(r.legal, r.violations)
        self.assertEqual(len(r.crossings), 1)
        self.assertTrue(r.crossings[0].at_node)
        self.assertEqual(r.crossings[0].gate_id, "G1")
        self.assertEqual(r.room_chain, ["A", "B"])

    def test_overlapping_gates_open_union(self):
        # BC 共边为 y=4 的横边，门的变化坐标是 x：
        # G2=(5,7)、G3=(6,8)，真实重叠开集为 (6,7)
        r_both = verify_route(self.version, "O1",
                              [[6, 2], ["13/2", 4], [6, 6]])  # 穿越点 x=6.5
        self.assertTrue(r_both.legal)
        c = next(c for c in r_both.crossings if set(c.all_gate_ids) >= {"G3"})
        self.assertEqual(c.point, ["13/2", "4"])
        self.assertEqual(set(c.all_gate_ids), {"G2", "G3"})
        self.assertEqual(c.gate_id, "G2")  # 主门取最小 id
        r_one = verify_route(self.version, "O2",
                             [[6, 2], ["15/2", 4], [6, 6]])  # x=7.5 仅 G3
        self.assertTrue(r_one.legal)
        c3 = next(c for c in r_one.crossings if c.gate_id == "G3")
        self.assertEqual(c3.all_gate_ids, ("G3",))

    def test_frame_endpoint_is_not_passable(self):
        # G2 门框点 (5,4) 恰在 BC 共边上：开放段不含端点，穿越即拒
        r_through = verify_route(self.version, "O4",
                                 [[6, 2], [5, 4], [5, 6]])
        self.assertFalse(r_through.legal)
        self.assertEqual(r_through.first_violation.reason,
                         GATE_FRAME_ENDPOINT)
        self.assertEqual(r_through.first_violation.point, ["5", "4"])
        self.assertEqual(r_through.crossings, [])
        # 路线终止在门框点上：同样拒绝（终点在墙上 + 门框语义）
        r_stop = verify_route(self.version, "O5",
                              [[6, 2], [5, 4]])
        self.assertFalse(r_stop.legal)
        self.assertIn(GATE_FRAME_ENDPOINT,
                      {v.reason for v in r_stop.violations})

    def test_walk_along_wall_rejected(self):
        # 沿 x=4 竖墙走正长度：区间中点在墙上而非任何房间内部
        r = verify_route(self.version, "W",
                         [[2, 1], [4, 1], [4, 2], [2, 2]])
        self.assertFalse(r.legal)
        self.assertIn(WALK_ALONG_WALL, {v.reason for v in r.violations})

    def test_wall_contact_and_bounce_back_rejected(self):
        # 走到墙上非门点 (4, 3.5) 再折回 A：擦墙返回
        r = verify_route(self.version, "B",
                         [[2, 2], [4, "7/2"], [2, 3]])
        self.assertFalse(r.legal)
        self.assertEqual(r.first_violation.reason, WALL_CONTACT)

    def test_zero_length_wait_rejected(self):
        r = verify_route(self.version, "Z",
                         [[2, 2], [2, 2], [3, 3]])
        self.assertFalse(r.legal)
        self.assertEqual(r.first_violation.reason, ZERO_LENGTH_SEGMENT)

    def test_outside_plan_rejected(self):
        # 从 A 内部走向平面外的点 (10,2)：先越界，终点也在外部
        r = verify_route(self.version, "X",
                         [[2, 2], [10, 2]])
        self.assertFalse(r.legal)
        self.assertIn(OUTSIDE_PLAN, {v.reason for v in r.violations})

    def test_diagonal_corner_jump_rejected(self):
        # A 直入对角 C：公共角点 (4,4) 仅点接触
        r = verify_route(self.version, "D",
                         [[2, 2], [6, 6]])
        self.assertFalse(r.legal)
        self.assertEqual(r.first_violation.reason,
                         POINT_CONTACT_NO_SHARED_EDGE)

    def test_endpoint_must_be_strict_interior(self):
        r = verify_route(self.version, "E",
                         [[4, 2], [6, 2]])  # 起点在墙上
        self.assertFalse(r.legal)
        self.assertEqual(r.first_violation.reason, "ENDPOINT_NOT_INTERIOR")

    def test_rational_thirds_crossing(self):
        # 有理坐标：x 从 1/3 到 19/3，x=4 对应 t=(4-1/3)/(19/3-1/3)=11/18
        r = verify_route(self.version, "Q",
                         [["1/3", "2/1"], ["19/3", "2"]])
        self.assertTrue(r.legal, r.violations)
        self.assertEqual(r.crossings[0].point, ["4", "2"])
        self.assertEqual(r.crossings[0].t, "11/18")


if __name__ == "__main__":
    unittest.main(verbosity=2)
