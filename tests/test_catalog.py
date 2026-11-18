import unittest

from cs import catalog


class CycleTests(unittest.TestCase):
    def test_diamond_is_acyclic(self):
        nodes = ["R", "A", "B", "L"]
        edges = [("A", "R"), ("B", "R"), ("L", "A"), ("L", "B")]
        self.assertTrue(catalog.is_acyclic(nodes, edges))
        self.assertIsNone(catalog.find_cycle(nodes, edges))

    def test_cycle_detected_after_batch(self):
        edges = [("A", "R"), ("B", "R"), ("L", "A"), ("L", "B"), ("R", "L")]
        cycle = catalog.find_cycle(["R", "A", "B", "L"], edges)
        self.assertIsNotNone(cycle)
        self.assertEqual(cycle[0], cycle[-1])

    def test_final_edge_set_judged_not_operation_order(self):
        # Adding L->A->R then removing R->... intermediate cycle is resolved by
        # the final set; the checker only ever sees the final set.
        initial = [("A", "R"), ("B", "R"), ("L", "A"), ("L", "B")]
        final = set(initial)
        final.discard(("L", "A"))
        final.add(("R", "L"))
        self.assertIsNotNone(catalog.find_cycle(["R", "A", "B", "L"], final))
        final.discard(("R", "L"))
        final.add(("L", "A"))
        self.assertTrue(catalog.is_acyclic(["R", "A", "B", "L"], final))

    def test_self_cycle(self):
        self.assertIsNotNone(catalog.find_cycle(["A"], [("A", "A")]))


class ReachabilityTests(unittest.TestCase):
    def setUp(self):
        self.edges = [("A", "R"), ("B", "R"), ("L", "A"), ("L", "B")]

    def test_ancestors(self):
        self.assertEqual(catalog.ancestors("L", self.edges), {"A", "B", "R"})
        self.assertEqual(catalog.ancestors("A", self.edges), {"R"})
        self.assertEqual(catalog.ancestors("R", self.edges), set())

    def test_descendants(self):
        self.assertEqual(catalog.descendants("R", self.edges), {"R", "A", "B", "L"})
        self.assertEqual(catalog.descendants("A", self.edges), {"A", "L"})

    def test_coverage_target_itself(self):
        result = catalog.explain_coverage("A", ["A"], self.edges)
        self.assertEqual(result["covered_classes"], ["A"])
        self.assertEqual(result["entries"][0]["path"], ["A"])

    def test_coverage_multiple_parents_not_multiplied(self):
        result = catalog.explain_coverage("R", ["L"], self.edges)
        self.assertTrue(result["entries"][0]["covered"])
        self.assertEqual(result["entries"][0]["path"], ["L", "A", "R"])

    def test_coverage_after_edge_removed(self):
        edges = [("A", "R"), ("B", "R"), ("L", "B")]
        result = catalog.explain_coverage("R", ["L"], edges)
        self.assertTrue(result["entries"][0]["covered"])
        self.assertEqual(result["entries"][0]["path"], ["L", "B", "R"])
        result = catalog.explain_coverage("A", ["L"], [("B", "R"), ("L", "B")])
        self.assertEqual(result["uncovered_classes"], ["L"])

    def test_path_choice_is_deterministic(self):
        dist, nxt = catalog.distances_to_target("R", self.edges)
        self.assertEqual(catalog.choose_path(["L"], "R", dist, nxt), ["L", "A", "R"])
        self.assertEqual(catalog.choose_path(["B", "L"], "R", dist, nxt), ["B", "R"])


if __name__ == "__main__":
    unittest.main()
