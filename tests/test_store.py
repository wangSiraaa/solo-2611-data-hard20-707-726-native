import json
import os
import tempfile
import threading
import unittest

from cs.store import (
    CatalogError,
    ConflictError,
    FactError,
    ProductError,
    Store,
)

CATALOG = {
    "format": "classification-catalog/1",
    "revision": "G1",
    "nodes": [
        {"id": "R", "name": "全部"},
        {"id": "A", "name": "办件"},
        {"id": "B", "name": "办件"},
        {"id": "L", "name": "交叉子类"},
    ],
    "edges": [["A", "R"], ["B", "R"], ["L", "A"], ["L", "B"]],
}

FACTS = [
    {"ticket_id": "T1", "revision": 1, "amount": 100, "classes": ["L"]},
    {"ticket_id": "T2", "revision": 1, "amount": 0, "classes": ["A"]},
    {"ticket_id": "T3", "revision": 1, "amount": 50, "classes": ["B"]},
]


def fresh_store(testcase) -> Store:
    path = tempfile.mktemp(suffix=".db")
    testcase.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
    store = Store.open(path)
    store.init_catalog(CATALOG)
    store.apply_fact_revisions(FACTS, request_id="seed")
    testcase.addCleanup(store.close)
    return store


def group(result, target):
    for g in result["groups"]:
        if g["target_id"] == target:
            return g
    raise AssertionError(f"target {target} missing")


class SeedAndQueryTests(unittest.TestCase):
    def setUp(self):
        self.store = fresh_store(self)

    def test_initial_overlap(self):
        result = self.store.query(["A", "B"], name="overlap")
        a, b = group(result, "A"), group(result, "B")
        self.assertEqual((a["count"], a["amount"]), (2, 100))
        self.assertEqual((b["count"], b["amount"]), (2, 150))
        self.assertEqual(
            result["union"]["count"], 3
        )
        self.assertEqual(result["union"]["amount"], 150)

    def test_zero_amount_ticket_still_counts(self):
        result = self.store.query(["A"])
        a = group(result, "A")
        self.assertIn("T2", [c["ticket_id"] for c in a["contributions"]])
        self.assertEqual(a["count"], 2)

    def test_multi_parent_not_multiplied(self):
        result = self.store.query(["R"])
        r = group(result, "R")
        self.assertEqual(r["count"], 3)
        self.assertEqual(r["amount"], 150)

    def test_parent_and_child_union(self):
        result = self.store.query(["R", "L"])
        self.assertEqual(group(result, "R")["count"], 3)
        self.assertEqual(group(result, "L")["count"], 1)
        self.assertEqual(result["union"]["count"], 3)
        self.assertEqual(result["union"]["amount"], 150)

    def test_path_present(self):
        result = self.store.query(["R"])
        r = group(result, "R")
        for c in r["contributions"]:
            self.assertTrue(c["path"])
            self.assertEqual(c["path"][-1], "R")

    def test_duplicate_target_rejected(self):
        with self.assertRaises(Exception):
            self.store.query(["A", "A"])

    def test_unknown_target(self):
        with self.assertRaises(CatalogError):
            self.store.query(["ZZ"])

    def test_same_name_different_identity(self):
        # A and B share the name "办件"; they must stay distinct via ids.
        state = self.store.load_catalog_state()
        self.assertEqual(state.nodes["A"], state.nodes["B"])
        result = self.store.query(["A", "B"])
        self.assertEqual([g["target_id"] for g in result["groups"]], ["A", "B"])


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.store = fresh_store(self)

    def test_b1_applies_atomically(self):
        committed = self.store.apply_bundle(
            {
                "add_edges": [],
                "remove_edges": [["L", "A"]],
                "fact_revisions": [
                    {"ticket_id": "T1", "revision": 2, "amount": 120, "classes": ["L"]}
                ],
            },
            request_id="B1",
        )
        self.assertEqual(committed.cut.catalog_revision, "G2")
        self.assertEqual(committed.cut.fact_revision, "F2")
        result = self.store.query(["A", "B"])
        a, b = group(result, "A"), group(result, "B")
        self.assertEqual((a["count"], a["amount"]), (1, 0))
        self.assertEqual([c["ticket_id"] for c in a["contributions"]], ["T2"])
        self.assertEqual((b["count"], b["amount"]), (2, 170))
        self.assertEqual(result["union"]["count"], 3)
        self.assertEqual(result["union"]["amount"], 170)

    def test_bad_bundle_rejected_wholesale(self):
        before = self.store.current_cut()
        with self.assertRaises(CatalogError):
            self.store.apply_bundle(
                {
                    "add_edges": [["R", "L"]],
                    "remove_edges": [],
                    "fact_revisions": [
                        {"ticket_id": "T2", "revision": 2, "amount": 999, "classes": ["A"]}
                    ],
                },
                request_id="B_BAD",
            )
        self.assertEqual(self.store.current_cut(), before)
        result = self.store.query(["A", "B"])
        self.assertEqual(group(result, "A")["amount"], 100)
        # 999 must never appear anywhere.
        amounts = {c["amount"] for g in result["groups"] for c in g["contributions"]}
        self.assertNotIn(999, amounts)
        self.assertNotIn(999, {c["amount"] for c in result["union"]["contributions"]})

    def test_cycle_alone_rejected_without_fact_change(self):
        with self.assertRaises(CatalogError):
            self.store.apply_bundle(
                {"add_edges": [["R", "L"]], "remove_edges": [], "fact_revisions": []},
                request_id="B_CYCLE",
            )
        self.assertEqual(self.store.catalog_revision(), "G1")

    def test_illegal_class_in_bundle_rejected(self):
        with self.assertRaises(FactError):
            self.store.apply_bundle(
                {
                    "add_edges": [],
                    "remove_edges": [],
                    "fact_revisions": [
                        {"ticket_id": "T1", "revision": 2, "amount": 1, "classes": ["NOPE"]}
                    ],
                },
                request_id="B_BADCLASS",
            )
        self.assertEqual(self.store.query(["A"])["groups"][0]["amount"], 100)

    def test_revision_conflict_rolls_back_catalog_too(self):
        # Removing L->A is legal, but the fact revision conflicts (same
        # revision number, different content): the whole bundle must be
        # rejected, including the catalog change.
        with self.assertRaises(FactError):
            self.store.apply_bundle(
                {
                    "add_edges": [],
                    "remove_edges": [["L", "A"]],
                    "fact_revisions": [
                        {"ticket_id": "T1", "revision": 1, "amount": 100, "classes": ["B"]}
                    ],
                },
                request_id="B_CONFLICT",
            )
        self.assertEqual(self.store.catalog_revision(), "G1")
        self.assertEqual(group(self.store.query(["A"]), "A")["amount"], 100)

    def test_noop_bundle_creates_no_revision(self):
        before = self.store.current_cut()
        committed = self.store.apply_bundle(
            {
                "remove_edges": [["L", "A"]],
                "add_edges": [["L", "A"]],
                "fact_revisions": [],
            },
            request_id="B_NOOP",
        )
        self.assertTrue(committed.noop)
        self.assertEqual(self.store.current_cut(), before)

    def test_bundle_idempotent_same_content(self):
        op = {
            "add_edges": [],
            "remove_edges": [["L", "A"]],
            "fact_revisions": [
                {"ticket_id": "T1", "revision": 2, "amount": 120, "classes": ["L"]}
            ],
        }
        first = self.store.apply_bundle(op, request_id="B1")
        second = self.store.apply_bundle(op, request_id="B1")
        self.assertEqual(first.cut, second.cut)

    def test_bundle_same_id_different_content_rejected(self):
        self.store.apply_bundle(
            {
                "remove_edges": [["L", "A"]],
                "add_edges": [],
                "fact_revisions": [
                    {"ticket_id": "T1", "revision": 2, "amount": 120, "classes": ["L"]}
                ],
            },
            request_id="B1",
        )
        with self.assertRaises(ConflictError):
            self.store.apply_bundle(
                {
                    "remove_edges": [["L", "B"]],
                    "add_edges": [],
                    "fact_revisions": [],
                },
                request_id="B1",
            )


class RevisionTests(unittest.TestCase):
    def setUp(self):
        self.store = fresh_store(self)

    def test_fact_revision_retry_same_content_ok(self):
        first = self.store.apply_fact_revision("T1", 2, 110, ["L"], request_id="R-A")
        # Same committed revision, same content, new request id: accepted, no-op.
        again = self.store.apply_fact_revision("T1", 2, 110, ["L"], request_id="R-B")
        self.assertTrue(again.noop)
        self.assertEqual(again.cut, first.cut)
        # Replaying the original request id is idempotent.
        replay = self.store.apply_fact_revision("T1", 2, 110, ["L"], request_id="R-A")
        self.assertEqual(replay.cut, first.cut)

    def test_fact_revision_retry_diff_content_rejected(self):
        self.store.apply_fact_revision("T1", 2, 110, ["L"])
        with self.assertRaises(FactError):
            self.store.apply_fact_revision("T1", 2, 999, ["L"])

    def test_old_revision_rejected(self):
        self.store.apply_fact_revision("T1", 2, 110, ["L"])
        with self.assertRaises(FactError):
            self.store.apply_fact_revision("T1", 1, 100, ["L"])

    def test_non_negative_integer_amount(self):
        with self.assertRaises(FactError):
            self.store.apply_fact_revision("T1", 2, -1, ["L"])
        with self.assertRaises(FactError):
            self.store.apply_fact_revision("T1", 2, 1.5, ["L"])

    def test_empty_classes_rejected(self):
        with self.assertRaises(FactError):
            self.store.apply_fact_revision("T1", 2, 5, [])

    def test_withdraw_removes_from_results(self):
        withdrawn = self.store.withdraw("T1", 1)
        self.assertEqual(withdrawn.cut.fact_revision, "F2")
        result = self.store.query(["A", "B", "R"])
        self.assertEqual([c["ticket_id"] for c in group(result, "A")["contributions"]], ["T2"])
        self.assertEqual(group(result, "B")["count"], 1)
        self.assertEqual(result["union"]["count"], 2)
        self.assertEqual(result["union"]["amount"], 50)

    def test_withdraw_wrong_revision(self):
        with self.assertRaises(FactError):
            self.store.withdraw("T1", 7)

    def test_reopen_after_withdraw_requires_higher_revision(self):
        self.store.withdraw("T1", 1)
        with self.assertRaises(FactError):
            self.store.apply_fact_revision("T1", 1, 100, ["L"])
        reopened = self.store.apply_fact_revision("T1", 2, 70, ["L"])
        self.assertFalse(reopened.noop)
        self.assertEqual(group(self.store.query(["A"]), "A")["amount"], 70)


class CatalogRevisionTests(unittest.TestCase):
    def setUp(self):
        self.store = fresh_store(self)

    def test_ancestors_descendants(self):
        self.assertEqual(self.store.ancestors("L"), ["A", "B", "R"])
        self.assertEqual(self.store.descendants("R"), ["A", "B", "L", "R"])

    def test_batch_judged_on_final_edge_set(self):
        # Replacing X->Y by Y->X is acyclic as a final set, although the
        # intermediate state after just adding Y->X would be cyclic.
        node_catalog = {
            "format": "classification-catalog/1",
            "revision": "G1",
            "nodes": [{"id": "X"}, {"id": "Y"}, {"id": "Z"}],
            "edges": [["X", "Y"]],
        }
        path = tempfile.mktemp(suffix=".db")
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        store = Store.open(path)
        self.addCleanup(store.close)
        store.init_catalog(node_catalog)
        committed = store.submit(
            {
                "operation": "catalog",
                "add_edges": [["Y", "X"]],
                "remove_edges": [["X", "Y"]],
            },
            request_id="swap",
        )
        self.assertFalse(committed.noop)
        self.assertEqual(sorted(store.load_catalog_state().edges), [("Y", "X")])
        with self.assertRaises(CatalogError):
            store.submit(
                {"operation": "catalog", "add_edges": [["X", "Y"]]},
                request_id="cyc",
            )
        self.assertEqual(store.catalog_revision(), "G2")

    def test_edge_removal_affects_summary(self):
        self.store.submit(
            {"operation": "catalog", "remove_edges": [["L", "A"]]},
            request_id="C1",
        )
        result = self.store.query(["A", "B"])
        self.assertEqual(group(result, "A")["count"], 1)
        self.assertEqual(group(result, "B")["count"], 2)

    def test_no_stale_closure_after_removal(self):
        # After removing both L parents the closure must not leak old results.
        self.store.submit(
            {
                "operation": "catalog",
                "remove_edges": [["L", "A"], ["L", "B"]],
            },
            request_id="C2",
        )
        result = self.store.query(["A", "B", "R"])
        self.assertEqual(group(result, "A")["amount"], 0)  # only T2
        self.assertEqual(group(result, "B")["amount"], 50)  # only T3
        self.assertEqual(group(result, "R")["amount"], 50)

    def test_explain_coverage(self):
        explanation = self.store.explain_target("A", ["L", "B", "A"])
        self.assertEqual(explanation["covered_classes"], ["A", "L"])
        self.assertEqual(explanation["uncovered_classes"], ["B"])
        entry_l = next(e for e in explanation["entries"] if e["class_id"] == "L")
        self.assertEqual(entry_l["path"], ["L", "A"])

    def test_readd_existing_node_is_noop(self):
        committed = self.store.add_nodes([{"id": "L", "name": "交叉子类"}], request_id="C4")
        self.assertTrue(committed.noop)
        self.assertEqual(committed.catalog_revision, "G1")

    def test_reopen_recovery_after_cycle_rejection(self):
        # A cyclic bundle is rejected; a corrected follow-up is accepted and
        # the summary reflects only the corrected shape.
        with self.assertRaises(CatalogError):
            self.store.apply_bundle(
                {
                    "add_edges": [["R", "L"]],
                    "remove_edges": [],
                    "fact_revisions": [],
                },
                request_id="C_BAD",
            )
        self.assertEqual(self.store.catalog_revision(), "G1")
        committed = self.store.apply_bundle(
            {
                "add_edges": [],
                "remove_edges": [["L", "A"]],
                "fact_revisions": [],
            },
            request_id="C_FIXED",
        )
        self.assertFalse(committed.noop)
        self.assertEqual(committed.catalog_revision, "G2")
        result = self.store.query(["A", "B"])
        self.assertEqual(group(result, "A")["count"], 1)
        self.assertEqual(group(result, "B")["count"], 2)


class ProductTests(unittest.TestCase):
    def setUp(self):
        self.store = fresh_store(self)

    def test_published_product_is_immutable(self):
        published = self.store.publish(["A", "B"], name="before", product_id="P1")
        payload = published["payload"]
        self.assertEqual(payload["cut"]["catalog_revision"], "G1")
        self.store.apply_bundle(
            {
                "remove_edges": [["L", "A"]],
                "fact_revisions": [
                    {"ticket_id": "T1", "revision": 2, "amount": 120, "classes": ["L"]}
                ],
            },
            request_id="B1",
        )
        reread = self.store.get_product("P1")
        self.assertEqual(reread["payload"], payload)
        # New publication reflects the new cut.
        published2 = self.store.publish(["A", "B"], name="after")
        self.assertEqual(published2["payload"]["cut"]["catalog_revision"], "G2")
        self.assertEqual(
            group(published2["payload"], "A")["amount"], 0
        )

    def test_cut_pinned_query_reads_old_snapshot(self):
        old_cut = self.store.current_cut()
        self.store.apply_bundle(
            {
                "remove_edges": [["L", "A"]],
                "fact_revisions": [
                    {"ticket_id": "T1", "revision": 2, "amount": 120, "classes": ["L"]}
                ],
            },
            request_id="B1",
        )
        old = self.store.query(["A", "B"], snapshot=old_cut)
        self.assertEqual(group(old, "A")["amount"], 100)
        self.assertEqual(group(old, "B")["amount"], 150)
        self.assertEqual(old["cut"]["catalog_revision"], "G1")

    def test_product_payload_is_classification_summary(self):
        payload = self.store.build_product(["A", "B"])
        self.assertEqual(payload["format"], "classification-summary/1")
        self.assertIn("catalog_revision", payload["cut"])
        self.assertIn("fact_revision", payload["cut"])
        for g in payload["groups"]:
            self.assertEqual(
                set(g), {"target_id", "count", "amount", "contributions"}
            )
        self.assertEqual(
            set(payload["union"]), {"count", "amount", "contributions"}
        )


class ConcurrencyTests(unittest.TestCase):
    def test_reader_sees_before_or_after_not_half(self):
        store = fresh_store(self)
        path = store.conn.execute("PRAGMA database_list").fetchone()[2]
        stop = threading.Event()
        observations = []
        errors = []

        def reader():
            other = Store.open(path)
            try:
                while not stop.is_set():
                    try:
                        result = other.query(["A", "B"])
                    except Exception as exc:  # pragma: no cover - surfaced below
                        errors.append(exc)
                        return
                    a = group(result, "A")
                    b = group(result, "B")
                    observations.append(
                        (
                            result["cut"]["catalog_revision"],
                            a["amount"],
                            b["amount"],
                        )
                    )
            finally:
                other.close()

        thread = threading.Thread(target=reader)
        thread.start()
        try:
            for i in range(20):
                store.apply_bundle(
                    {
                        "remove_edges": [["L", "A"]],
                        "fact_revisions": [
                            {
                                "ticket_id": "T1",
                                "revision": i + 2,
                                "amount": 120 + i,
                                "classes": ["L"],
                            }
                        ],
                    },
                    request_id=f"b-{i}",
                )
        finally:
            stop.set()
            thread.join()
        self.assertFalse(errors)
        self.assertTrue(observations)
        for _, a_amount, b_amount in observations:
            # Either the pre-B1 world (A=100, B=150) or the post-B1 world
            # (A empty -> 0, B contains T1+T3).  Never a mix such as A=120.
            if (a_amount, b_amount) == (100, 150):
                continue
            self.assertEqual(a_amount, 0)
            self.assertGreaterEqual(b_amount, 170)

    def test_shared_database_consistent_cut(self):
        store = fresh_store(self)
        result = store.query(["A", "B"])
        self.assertEqual(result["cut"], store.current_cut().as_dict())


if __name__ == "__main__":
    unittest.main()
