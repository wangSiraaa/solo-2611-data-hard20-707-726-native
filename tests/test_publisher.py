import unittest

from index_service import (
    Catalogue,
    PublicationConflict,
    PublicationRejected,
    PublicationRequest,
    Publisher,
    Phrase,
)


def add(catalogue, document_id, revision, title, body):
    catalogue.add_revision(
        {"id": document_id, "revision": revision, "title": title, "body": body}
    )


def request(minimum=2):
    return PublicationRequest(
        include=Phrase("title", ("amber", "gear"), 1),
        exclude=Phrase("body", ("stop", "tag"), 0),
        minimum_documents=minimum,
    )


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.catalogue = Catalogue()
        self.publisher = Publisher()

    def tearDown(self):
        self.catalogue.close()
        self.publisher.close()

    def _scenario_v1(self):
        add(self.catalogue, "D1", 1, ["Amber bolt gear"], ["stop blue tag"])
        add(self.catalogue, "D2", 1, ["amber", "gear"], ["clean"])
        add(self.catalogue, "D3", 1, ["amber gear"], ["stop-tag"])
        add(self.catalogue, "D4", 1, ["amber gear stop tag"], ["clean"])

    def test_worked_example_v1_selects_d1_d4(self):
        self._scenario_v1()
        artifact = self.publisher.publish(
            self.catalogue, publication_id="P1", version="v1", request=request()
        )
        self.assertEqual(
            [s["document_id"] for s in artifact["selected"]], ["D1", "D4"]
        )

    def test_only_single_exclusion_word_does_not_exclude(self):
        self._scenario_v1()
        artifact = self.publisher.publish(
            self.catalogue, publication_id="P1", version="v1", request=request()
        )
        # D1 body is "stop blue tag": contains both words but not adjacent.
        self.assertIn("D1", [s["document_id"] for s in artifact["selected"]])

    def test_exclusion_in_other_field_does_not_exclude(self):
        self._scenario_v1()
        artifact = self.publisher.publish(
            self.catalogue, publication_id="P1", version="v1", request=request()
        )
        # D4 title has "stop tag" but exclusion is only checked in body.
        self.assertIn("D4", [s["document_id"] for s in artifact["selected"]])

    def test_cross_segment_title_does_not_include(self):
        self._scenario_v1()
        artifact = self.publisher.publish(
            self.catalogue, publication_id="P1", version="v1", request=request()
        )
        self.assertNotIn("D2", [s["document_id"] for s in artifact["selected"]])

    def test_genuine_body_exclusion_removes_document(self):
        self._scenario_v1()
        artifact = self.publisher.publish(
            self.catalogue, publication_id="P1", version="v1", request=request()
        )
        self.assertNotIn("D3", [s["document_id"] for s in artifact["selected"]])
        excluded_ids = [e["document_id"] for e in artifact["excluded"]]
        self.assertEqual(excluded_ids, ["D3"])

    def test_artifact_carries_title_positions(self):
        self._scenario_v1()
        artifact = self.publisher.publish(
            self.catalogue, publication_id="P1", version="v1", request=request()
        )
        by_id = {s["document_id"]: s for s in artifact["selected"]}
        self.assertEqual(by_id["D1"]["title_positions"][0]["positions"], [0, 2])
        self.assertEqual(by_id["D4"]["title_positions"][0]["positions"], [0, 1])

    def test_exclusion_has_independent_query(self):
        self._scenario_v1()
        artifact = self.publisher.publish(
            self.catalogue, publication_id="P1", version="v1", request=request()
        )
        self.assertEqual(len(artifact["excluded"]), 1)
        excluded = artifact["excluded"][0]
        self.assertEqual(
            excluded["exclude_query"],
            {"field": "body", "words": ["stop", "tag"], "g": 0},
        )

    def test_correction_shrinks_selection_and_v2_rejected(self):
        self._scenario_v1()
        v1 = self.publisher.publish(
            self.catalogue, publication_id="P1", version="v1", request=request()
        )
        # Correction: D4 body now has stop tag.
        add(self.catalogue, "D4", 2, ["amber gear stop tag"], ["stop tag"])
        with self.assertRaises(PublicationRejected) as ctx:
            self.publisher.publish(
                self.catalogue, publication_id="P2", version="v2", request=request()
            )
        self.assertEqual(
            [s["document_id"] for s in ctx.exception.selected], ["D1"]
        )
        # Rejected publication leaves no partial artifact.
        with self.assertRaises(Exception):
            self.publisher.read("P2")
        # Original v1 still readable and unchanged.
        reread = self.publisher.read("P1")
        self.assertEqual(
            [s["document_id"] for s in reread["selected"]], ["D1", "D4"]
        )
        self.assertEqual(reread, v1)

    def test_v1_cut_pins_old_revisions(self):
        self._scenario_v1()
        self.publisher.publish(
            self.catalogue, publication_id="P1", version="v1", request=request()
        )
        add(self.catalogue, "D4", 2, ["amber gear stop tag"], ["stop tag"])
        # Reading old publication must not be affected by the new revision.
        artifact = self.publisher.read("P1")
        self.assertIn(["D4", 1], artifact["cut"])
        # And explicit position query against the old cut still works.
        old_matches = self.catalogue.postings_for_token("gear", documents=[("D4", 1)])
        self.assertEqual(len(old_matches), 1)

    def test_replay_returns_original_artifact(self):
        self._scenario_v1()
        first = self.publisher.publish(
            self.catalogue, publication_id="P1", version="v1", request=request()
        )
        add(self.catalogue, "D4", 2, ["amber gear stop tag"], ["stop tag"])
        replay = self.publisher.publish(
            self.catalogue, publication_id="P1", version="v1", request=request()
        )
        self.assertEqual(first, replay)
        self.assertIn(["D4", 1], replay["cut"])

    def test_minimum_boundary_exactly_met(self):
        add(self.catalogue, "D1", 1, ["amber gear"], [])
        add(self.catalogue, "D2", 1, ["amber gear"], [])
        artifact = self.publisher.publish(
            self.catalogue, publication_id="P1", version="v1", request=request(2)
        )
        self.assertEqual(len(artifact["selected"]), 2)

    def test_no_partial_list_on_rejection(self):
        add(self.catalogue, "D1", 1, ["amber gear"], [])
        with self.assertRaises(PublicationRejected):
            self.publisher.publish(
                self.catalogue, publication_id="P1", version="v1", request=request(2)
            )
        self.assertEqual(self.publisher.list_publications(), [])

    def test_conflicting_id_is_rejected(self):
        self._scenario_v1()
        self.publisher.publish(
            self.catalogue, publication_id="P1", version="v1", request=request()
        )
        other = PublicationRequest(
            include=Phrase("title", ("amber",), 0),
            exclude=Phrase("body", ("stop", "tag"), 0),
            minimum_documents=1,
        )
        with self.assertRaises(PublicationConflict):
            self.publisher.publish(
                self.catalogue, publication_id="P1", version="v1", request=other
            )

    def test_explicit_cut_overrides_current(self):
        self._scenario_v1()
        add(self.catalogue, "D4", 2, ["amber gear stop tag"], ["stop tag"])
        old_cut = [("D1", 1), ("D2", 1), ("D3", 1), ("D4", 1)]
        artifact = self.publisher.publish(
            self.catalogue,
            publication_id="P1",
            version="v1",
            request=request(),
            cut=old_cut,
        )
        self.assertEqual(
            [s["document_id"] for s in artifact["selected"]], ["D1", "D4"]
        )

    def test_current_cut_is_used_for_new_publication(self):
        self._scenario_v1()
        add(self.catalogue, "D4", 2, ["amber gear stop tag"], ["stop tag"])
        with self.assertRaises(PublicationRejected):
            self.publisher.publish(
                self.catalogue, publication_id="P2", version="v2", request=request()
            )


if __name__ == "__main__":
    unittest.main()
