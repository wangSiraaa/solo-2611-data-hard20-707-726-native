import copy
import json
import tempfile
import unittest
from pathlib import Path

from index_service.runner import run_materials
from index_service.service import Service


MATERIAL_775 = {
    "documents": [
        {"id": "D1", "revision": 1, "title": ["Amber bolt gear"], "body": ["stop blue tag"]},
        {"id": "D2", "revision": 1, "title": ["amber", "gear"], "body": ["clean"]},
        {"id": "D3", "revision": 1, "title": ["amber gear"], "body": ["stop-tag"]},
        {"id": "D4", "revision": 1, "title": ["amber gear stop tag"], "body": ["clean"]},
    ],
    "revision": {
        "id": "D4",
        "revision": 2,
        "title": ["amber gear stop tag"],
        "body": ["stop tag"],
    },
    "query": {
        "include": {"field": "title", "words": ["amber", "gear"], "g": 1},
        "exclude": {"field": "body", "words": ["stop", "tag"], "g": 0},
        "minimum_documents": 2,
    },
    "operations": [
        {"id": "P1", "op": "publish", "version": "v1"},
        {"id": "P2", "op": "publish", "version": "v2"},
        {"op": "read", "id": "P1"},
    ],
}


class Scenario775Tests(unittest.TestCase):
    def test_worked_example_end_to_end(self):
        report = run_materials(copy.deepcopy(MATERIAL_775))
        ops = report["operations"]
        self.assertEqual(ops[0]["status"], "published")
        self.assertEqual(ops[0]["selected_ids"], ["D1", "D4"])
        self.assertEqual(ops[1]["status"], "rejected")
        self.assertEqual(
            [s["document_id"] for s in ops[1]["selected"]], ["D1"]
        )
        self.assertEqual(ops[2]["status"], "ok")
        reread = ops[2]["artifact"]
        self.assertEqual(
            [s["document_id"] for s in reread["selected"]], ["D1", "D4"]
        )
        self.assertEqual(reread, report["publications"]["P1"])

    def test_read_old_cut_reflects_old_revision_for_d4(self):
        report = run_materials(copy.deepcopy(MATERIAL_775))
        artifact = report["publications"]["P1"]
        by_id = {s["document_id"]: s for s in artifact["selected"]}
        self.assertEqual(by_id["D4"]["revision"], 1)
        self.assertIn(["D4", 1], artifact["cut"])

    def test_explicit_apply_operation_order(self):
        material = copy.deepcopy(MATERIAL_775)
        material["operations"] = [
            {"id": "P1", "op": "publish", "version": "v1"},
            {"op": "apply_revision", "id": "R2", "revision": material["revision"]},
            {"id": "P2", "op": "publish", "version": "v2"},
            {"op": "read", "id": "P1"},
        ]
        material.pop("revision")
        report = run_materials(material)
        ops = report["operations"]
        self.assertEqual(ops[0]["selected_ids"], ["D1", "D4"])
        self.assertEqual(ops[1]["op"], "apply_revision")
        self.assertEqual(ops[2]["status"], "rejected")
        self.assertEqual(ops[3]["status"], "ok")
        self.assertEqual(
            [s["document_id"] for s in ops[3]["artifact"]["selected"]],
            ["D1", "D4"],
        )


class FixtureFileTests(unittest.TestCase):
    def test_repository_fixture_runs(self):
        path = Path(__file__).resolve().parents[1] / "materials" / "775" / "input.json"
        if not path.exists():
            self.skipTest("repository fixture not present")
        material = json.loads(path.read_text(encoding="utf-8"))
        report = run_materials(material)
        self.assertEqual(report["operations"][0]["selected_ids"], ["D1", "D4"])
        self.assertEqual(report["operations"][1]["status"], "rejected")


class ServiceTests(unittest.TestCase):
    def test_independent_position_query(self):
        with Service() as service:
            service.load_documents(
                [
                    {"id": "D1", "revision": 1, "title": ["Amber bolt gear"], "body": []},
                ]
            )
            matches = service.query_positions(
                {"field": "title", "words": ["amber", "gear"], "g": 1}
            )
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["document_id"], "D1")
        self.assertEqual(matches[0]["segment"], 0)
        self.assertEqual(matches[0]["positions"], [0, 2])

    def test_query_rejects_multitoken_word(self):
        with Service() as service:
            with self.assertRaises(ValueError):
                service.query_positions(
                    {"field": "title", "words": ["amber gear"], "g": 1}
                )

    def test_persisted_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "service.sqlite")
            cat_path = str(Path(tmp) / "cat.sqlite")
            from index_service import Catalogue, Publisher

            cat = Catalogue(cat_path)
            pub = Publisher(db)
            service = Service(cat, pub)
            try:
                service.load_documents(
                    [
                        {"id": "D1", "revision": 1, "title": ["amber gear"], "body": []},
                        {"id": "D2", "revision": 1, "title": ["amber gear"], "body": []},
                    ]
                )
                artifact = service.publish(
                    publication_id="P1",
                    version="v1",
                    query={
                        "include": {"field": "title", "words": ["amber", "gear"], "g": 0},
                        "exclude": {"field": "body", "words": ["stop", "tag"], "g": 0},
                        "minimum_documents": 2,
                    },
                )
            finally:
                service.close()

            cat2 = Catalogue(cat_path)
            pub2 = Publisher(db)
            service2 = Service(cat2, pub2)
            try:
                reread = service2.read("P1")
            finally:
                service2.close()
            self.assertEqual(artifact, reread)


if __name__ == "__main__":
    unittest.main()
