from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from boxship.catalog import Catalog
from boxship.core import PackError, PackStore, canonical_sealed_box

MATERIALS = Path(__file__).resolve().parents[1] / "materials" / "711"


def seed_doc(**overrides) -> dict:
    doc = json.loads((MATERIALS / "initial_store.json").read_text(encoding="utf-8"))
    doc.update(overrides)
    return doc


def manifest(seal_id: str, box_id: str, contents: list[dict], *,
             revision: str = "r1", capacity: int = 10) -> dict:
    return {
        "schema": "sealed-box/v1",
        "seal_id": seal_id,
        "box_id": box_id,
        "rule_revision": revision,
        "capacity": capacity,
        "contents": contents,
        "total_weight": sum(c["weight"] for c in contents),
    }


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "store.db"
        self.store = PackStore(self.db)
        self.addCleanup(self.store.close)
        self.store.import_seed(seed_doc())


class CatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cat = Catalog("r1", [["A", "B"], ["A", "A"]])

    def test_single_a_is_compatible(self):
        self.assertEqual(self.cat.check([("I1", "A")]), [])

    def test_two_a_conflict_with_actual_count(self):
        conflicts = self.cat.check([("I1", "A"), ("I2", "A")])
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].as_dict(),
                         {"categories": ["A", "A"], "counts": {"A": 2}})

    def test_counts_are_not_deduplicated(self):
        conflicts = self.cat.check([("x", "A"), ("y", "A"), ("z", "A")])
        self.assertEqual(conflicts[0].as_dict()["counts"], {"A": 3})

    def test_ab_pair_both_counts(self):
        conflicts = self.cat.check([("x", "A"), ("y", "A"), ("z", "B"), ("w", "B")])
        by_pair = {tuple(c.pair): c.as_dict() for c in conflicts}
        self.assertEqual(by_pair[("A", "B")]["counts"], {"A": 2, "B": 2})
        self.assertEqual(by_pair[("A", "A")]["counts"], {"A": 2})

    def test_unlisted_pair_compatible(self):
        cat = Catalog("r", [["A", "B"]])
        self.assertEqual(cat.check([("x", "C"), ("y", "C"), ("z", "A")]), [])

    def test_pair_order_is_undirected(self):
        cat = Catalog("r", [["B", "A"]])
        self.assertTrue(cat.check([("x", "A"), ("y", "B")]))


class SeedTests(StoreCase):
    def test_import_facts(self):
        s = self.store.summary()
        self.assertEqual(s["catalog"], "r1")
        self.assertEqual(s["items"], 4)
        self.assertEqual({b["box_id"]: b["status"] for b in s["boxes"]},
                         {"BOX_A": "SEALED", "BOX_B": "SEALED", "BOX_M": "OPEN"})
        self.assertEqual(self.store.box("BOX_A")["current_seal_id"], "SEAL_A_1")
        self.assertEqual(self.store.box_contents("BOX_M"),
                         [{"item_id": "I2", "category": "A", "weight": 3},
                          {"item_id": "I4", "category": "A", "weight": 1}])

    def test_second_import_rejected(self):
        with self.assertRaises(PackError) as cm:
            self.store.import_seed(seed_doc())
        self.assertEqual(cm.exception.code, "already_imported")

    def test_seed_seal_must_match_actual_contents(self):
        doc = seed_doc()
        doc["trusted_seals"][0]["contents"][0]["weight"] = 1
        doc["trusted_seals"][0]["total_weight"] = 1
        with tempfile.TemporaryDirectory() as d:
            store = PackStore(Path(d) / "x.db")
            with self.assertRaises(PackError) as cm:
                store.import_seed(doc)
            self.assertEqual(cm.exception.code, "seed_seal_mismatch")
            store.close()


class SealTests(StoreCase):
    def test_seal_fails_under_r1_and_stays_open(self):
        with self.assertRaises(PackError) as cm:
            self.store.seal_box("BOX_M")
        self.assertEqual(cm.exception.code, "conflict")
        self.assertEqual(self.store.box("BOX_M")["status"], "OPEN")
        self.assertEqual(len(self.store.box_contents("BOX_M")), 2)

    def test_revision_changes_next_seal_only(self):
        old = self.store.export_seal("BOX_A")
        self.store.apply_revision({"revision": "r2", "incompatible_pairs": [["A", "B"]]})
        self.assertEqual(self.store.seal_box("BOX_M")["rule_revision"], "r2")
        # 旧封箱记录与当前规则修订都还在, 且旧事实未被改写
        self.assertEqual(self.store.export_seal("BOX_A"), old)
        self.assertEqual(self.store.box("BOX_A")["current_seal_id"], "SEAL_A_1")

    def test_seal_artifact_fields(self):
        self.store.apply_revision({"revision": "r2", "incompatible_pairs": [["A", "B"]]})
        result = self.store.seal_box("BOX_M")
        art = self.store.export_seal("BOX_M")
        self.assertEqual(art["schema"], "sealed-box/v1")
        self.assertEqual(art["seal_id"], result["seal_id"])
        self.assertEqual([c["item_id"] for c in art["contents"]], ["I2", "I4"])
        self.assertEqual(art["total_weight"], 4)
        self.assertEqual(art["capacity"], 6)
        self.assertEqual(canonical_sealed_box(art), art)

    def test_new_seal_id_never_reuses_old(self):
        self.store.apply_revision({"revision": "r2", "incompatible_pairs": [["A", "B"]]})
        first = self.store.seal_box("BOX_M")["seal_id"]
        self.store.unseal_box("BOX_M")
        second = self.store.seal_box("BOX_M")["seal_id"]
        self.assertNotEqual(first, second)

    def test_unseal_invalidates_and_export_fails(self):
        self.store.unseal_box("BOX_A")
        self.assertIsNone(self.store.box("BOX_A")["current_seal_id"])
        with self.assertRaises(PackError):
            self.store.export_seal("BOX_A")
        # 记录仍保留 (不可变历史), 但不再 live
        row = self.store.conn.execute(
            "SELECT live FROM sealed_boxes WHERE seal_id='SEAL_A_1'").fetchone()
        self.assertEqual(row["live"], 0)

    def test_reseal_uses_current_rule(self):
        self.store.unseal_box("BOX_A")
        self.store.apply_revision({"revision": "r2", "incompatible_pairs": []})
        res = self.store.seal_box("BOX_A")
        self.assertEqual(res["rule_revision"], "r2")

    def test_cannot_redefine_existing_revision(self):
        with self.assertRaises(PackError) as cm:
            self.store.apply_revision({"revision": "r1", "incompatible_pairs": []})
        self.assertEqual(cm.exception.code, "revision_redefined")


class MoveTests(StoreCase):
    def test_capacity_failure_is_atomic(self):
        with self.assertRaises(PackError) as cm:
            self.store.move_item("I2", "BOX_B")  # 已封 -> 先拒绝
        self.assertEqual(cm.exception.code, "box_not_open")
        self.store.create_box("BOX_T", 1)
        with self.assertRaises(PackError) as cm:
            self.store.move_item("I2", "BOX_T")  # 3 > 1
        self.assertEqual(cm.exception.code, "capacity_exceeded")
        self.assertEqual(self.store.item_location("I2")["box_id"], "BOX_M")
        self.assertEqual(self.store.box_contents("BOX_T"), [])

    def test_move_into_sealed_box_rejected(self):
        with self.assertRaises(PackError):
            self.store.move_item("I2", "BOX_A")
        self.assertEqual(self.store.item_location("I2")["box_id"], "BOX_M")

    def test_move_out_of_sealed_box_rejected(self):
        with self.assertRaises(PackError) as cm:
            self.store.move_item("I1", "BOX_M")
        self.assertEqual(cm.exception.code, "source_not_open")
        self.assertEqual(self.store.item_location("I1")["box_id"], "BOX_A")

    def test_successful_cross_box_move(self):
        self.store.create_box("BOX_N", 10)
        self.store.move_item("I2", "BOX_N")
        self.assertEqual(self.store.item_location("I2")["box_id"], "BOX_N")
        self.assertEqual([c["item_id"] for c in self.store.box_contents("BOX_M")], ["I4"])


class ReceiveTests(StoreCase):
    def payload(self, shipment_id: str, *manifests: dict) -> dict:
        return {"schema": "shipment-input/v1", "shipment_id": shipment_id,
                "manifests": list(manifests)}

    def a(self) -> dict:
        return json.loads((MATERIALS / "handoff/a.json").read_text(encoding="utf-8"))

    def b(self) -> dict:
        return json.loads((MATERIALS / "handoff/b.json").read_text(encoding="utf-8"))

    def test_split_shipment_is_accepted_even_though_categories_would_clash(self):
        # 整单类别并集 A+B 在 r1 下冲突, 但分属两个箱子, 应合法
        res = self.store.receive_shipment_input(self.payload("S1", self.a(), self.b()))
        self.assertTrue(res["ok"])
        self.assertEqual(self.store.box("BOX_A")["status"], "SHIPPED")
        self.assertEqual(self.store.box("BOX_B")["status"], "SHIPPED")

    def test_changed_content_rejected_and_box_not_claimed(self):
        changed = json.loads(
            (MATERIALS / "handoff/a_changed.json").read_text(encoding="utf-8"))
        with self.assertRaises(PackError) as cm:
            self.store.receive_shipment_input(self.payload("SBAD", changed))
        self.assertEqual(cm.exception.code, "manifest_mismatch")
        self.assertEqual(self.store.box("BOX_A")["status"], "SEALED")
        self.assertEqual(self.store.summary()["shipments"], [])

    def test_artifact_must_still_be_structurally_sound(self):
        bad = self.a()
        bad["total_weight"] = 999
        with self.assertRaises(PackError) as cm:
            self.store.receive_shipment_input(self.payload("SX", bad))
        self.assertEqual(cm.exception.code, "manifest_not_self_consistent")

    def test_wrong_schema_version_rejected(self):
        bad = self.a()
        bad["schema"] = "sealed-box/v2"
        with self.assertRaises(PackError):
            self.store.receive_shipment_input(self.payload("SX", bad))

    def test_duplicate_box_in_one_application_rejected(self):
        with self.assertRaises(PackError) as cm:
            self.store.receive_shipment_input(self.payload("SD", self.a(), self.a()))
        self.assertEqual(cm.exception.code, "duplicate_box_in_shipment")

    def test_duplicate_item_inside_manifest_rejected(self):
        bad = self.a()
        bad["contents"] = [bad["contents"][0], dict(bad["contents"][0])]
        bad["total_weight"] = bad["contents"][0]["weight"] * 2
        with self.assertRaises(PackError) as cm:
            self.store.receive_shipment_input(self.payload("SX", bad))
        self.assertEqual(cm.exception.code, "duplicate_content_item")

    def test_one_bad_manifest_aborts_entire_shipment(self):
        changed = json.loads(
            (MATERIALS / "handoff/a_changed.json").read_text(encoding="utf-8"))
        with self.assertRaises(PackError):
            self.store.receive_shipment_input(
                self.payload("SMIX", self.b(), changed))
        self.assertEqual(self.store.box("BOX_B")["status"], "SEALED")
        self.assertEqual(self.store.box("BOX_A")["status"], "SEALED")
        self.assertEqual(self.store.summary()["shipments"], [])

    def test_replay_is_canonical_and_path_independent(self):
        p = self.payload("S1", self.a(), self.b())
        first = self.store.receive_shipment_input(p)
        reordered = self.payload("S1", self.b(), self.a())  # 顺序不同
        again = self.store.receive_shipment_input(reordered)
        self.assertFalse(first["replayed"])
        self.assertTrue(again["replayed"])
        self.assertEqual(first["boxes"], again["boxes"])

    def test_same_id_different_content_rejected(self):
        self.store.receive_shipment_input(self.payload("S1", self.a(), self.b()))
        changed = self.a()
        changed["capacity"] = 99
        with self.assertRaises(PackError) as cm:
            self.store.receive_shipment_input(self.payload("S1", changed))
        self.assertEqual(cm.exception.code, "shipment_conflict")

    def test_failed_id_is_not_occupied(self):
        bad = self.a()
        bad["total_weight"] = 999
        with self.assertRaises(PackError):
            self.store.receive_shipment_input(self.payload("SX", bad))
        ok = self.store.receive_shipment_input(self.payload("SX", self.a()))
        self.assertFalse(ok["replayed"])

    def test_box_cannot_belong_to_two_shipments(self):
        self.store.receive_shipment_input(self.payload("S1", self.a()))
        with self.assertRaises(PackError) as cm:
            self.store.receive_shipment_input(self.payload("S2", self.a(), self.b()))
        self.assertEqual(cm.exception.code, "box_already_shipped")
        self.assertEqual(self.store.box("BOX_B")["status"], "SEALED")

    def test_old_rule_not_retroactively_applied(self):
        # 先在 r2 (无 A-A) 下封 BOX_M, 再切到 r3 (恢复 A-A), 旧封箱仍可发运
        self.store.apply_revision({"revision": "r2", "incompatible_pairs": [["A", "B"]]})
        self.store.seal_box("BOX_M")
        self.store.apply_revision(
            {"revision": "r3", "incompatible_pairs": [["A", "B"], ["A", "A"]]})
        art = self.store.export_seal("BOX_M")
        res = self.store.receive_shipment_input(self.payload("SM", art))
        self.assertTrue(res["ok"])
        self.assertEqual(art["rule_revision"], "r2")

    def test_stale_seal_after_unseal_and_reseal_rejected(self):
        old = self.store.export_seal("BOX_A")
        self.store.unseal_box("BOX_A")
        self.store.seal_box("BOX_A")
        with self.assertRaises(PackError) as cm:
            self.store.receive_shipment_input(self.payload("S1", old))
        self.assertEqual(cm.exception.code, "stale_or_unknown_seal")

    def test_unknown_box_and_unknown_seal(self):
        ghost = manifest("SEAL_GHOST", "BOX_NOPE",
                         [{"item_id": "I1", "category": "A", "weight": 2}])
        with self.assertRaises(PackError) as cm:
            self.store.receive_shipment_input(self.payload("S", ghost))
        self.assertEqual(cm.exception.code, "unknown_box_manifest")
        wrong = self.a()
        wrong["seal_id"] = "SEAL_FAKE"
        with self.assertRaises(PackError) as cm:
            self.store.receive_shipment_input(self.payload("S", wrong))
        self.assertEqual(cm.exception.code, "stale_or_unknown_seal")

    def test_shipped_box_cannot_be_unsealed(self):
        self.store.receive_shipment_input(self.payload("S1", self.a()))
        with self.assertRaises(PackError) as cm:
            self.store.unseal_box("BOX_A")
        self.assertEqual(cm.exception.code, "shipped_box")


class ScaleTests(unittest.TestCase):
    def test_100_items_20_boxes(self):
        with tempfile.TemporaryDirectory() as d:
            store = PackStore(Path(d) / "scale.db")
            try:
                items = [{"item_id": f"I{n:03d}", "category": "A", "weight": 1}
                         for n in range(100)]
                boxes = [{"box_id": f"B{n:02d}", "capacity": 10, "status": "OPEN",
                          "item_ids": [f"I{m:03d}" for m in range(n * 5, n * 5 + 5)]}
                         for n in range(20)]
                store.import_seed({
                    "schema": "packing-seed/v1",
                    "catalog": {"revision": "r1", "incompatible_pairs": [["A", "A"]]},
                    "items": items,
                    "boxes": boxes,
                    "trusted_seals": [],
                    "shipments": [],
                })
                # 100 件分到 20 箱, 每箱 5 件 A -> r1 下 A-A 冲突, 不能封
                with self.assertRaises(PackError):
                    store.seal_box("B00")
                store.apply_revision({"revision": "r2", "incompatible_pairs": []})
                sealed = [store.seal_box(f"B{n:02d}") for n in range(20)]
                self.assertEqual(len({s["seal_id"] for s in sealed}), 20)
                payload = {"schema": "shipment-input/v1", "shipment_id": "S",
                           "manifests": [store.export_seal(f"B{n:02d}")
                                         for n in range(20)]}
                res = store.receive_shipment_input(payload)
                self.assertEqual(len(res["boxes"]), 20)
                self.assertEqual(store._total_weight("B00"), 5)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
