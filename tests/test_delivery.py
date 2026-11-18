"""Delivery-job behaviour: atomic commits, identity freezing, revisions, retries."""
from __future__ import annotations

import unittest

from support import MATERIALS, MaterialServiceCase, load_json

from address_delivery.delivery import (
    FAILED_AMBIGUOUS_ADDRESS,
    FAILED_STATION_AMBIGUOUS,
    FAILED_STATION_NONE,
    FAILED_STREET_INACTIVE,
    FAILED_STREET_UNKNOWN,
    REPLAYED,
    REJECTED_IDENTITY,
    SUBMITTED,
)
from address_delivery.models import AddressInput


class InitialImportTests(MaterialServiceCase):
    def test_r1_outcomes_match_hand_worked_notes(self):
        batch = self.svc.import_csv(MATERIALS / "addresses.csv")
        outcomes = {r.parcel_id: r.outcome for r in batch.rows}
        self.assertEqual(outcomes["P1"], FAILED_AMBIGUOUS_ADDRESS)
        self.assertEqual(outcomes["P2"], SUBMITTED)
        self.assertEqual(outcomes["P3"], FAILED_STATION_AMBIGUOUS)
        self.assertEqual(outcomes["P4"], FAILED_STREET_UNKNOWN)
        self.assertEqual(outcomes["P5"], SUBMITTED)
        self.assertEqual(outcomes["P6"], SUBMITTED)

    def test_only_successful_rows_create_jobs(self):
        self.svc.import_csv(MATERIALS / "addresses.csv")
        self.assertEqual(
            {j["parcel_id"] for j in self.svc.delivery.list_jobs()}, {"P2", "P5", "P6"}
        )

    def test_station_selection_requires_unique_station(self):
        batch = self.svc.import_csv(MATERIALS / "addresses.csv")
        p3 = next(r for r in batch.rows if r.parcel_id == "P3")
        self.assertEqual(p3.station_id, None)
        self.assertEqual({h["station_id"] for h in p3.coverage_hits}, {"A", "X"})

    def test_job_records_version_and_interpretation(self):
        self.svc.import_csv(MATERIALS / "addresses.csv")
        job = self.svc.delivery.get_job("P2")
        self.assertEqual(job["catalog_version"], 1)
        self.assertEqual(job["catalog_revision"], "r1")
        self.assertEqual(job["street_id"], "S3")
        self.assertEqual(job["district_id"], "D1")
        self.assertEqual(job["station_id"], "A")
        # Original text preserved verbatim, plus stable interpretation.
        interp = job["interpretation"]["resolution"]
        self.assertEqual(interp["candidates"][0]["street_id"], "S3")
        self.assertEqual(job["district_text_original"], "同区")


class QuotedAndUnicodeTests(MaterialServiceCase):
    def test_quoted_comma_alias_matches(self):
        items = self.svc.import_csv(MATERIALS / "addresses.csv").rows
        p5 = next(r for r in items if r.parcel_id == "P5")
        self.assertEqual(p5.outcome, SUBMITTED)
        self.assertEqual(p5.job["street_id"], "S3")

    def test_decomposed_accent_matches_nfc(self):
        rows = self.svc.import_csv(MATERIALS / "addresses.csv").rows
        p6 = next(r for r in rows if r.parcel_id == "P6")
        self.assertEqual(p6.outcome, SUBMITTED)
        self.assertEqual(p6.job["street_id"], "S3")
        # Original decomposed text is preserved, normalised text is NFC.
        self.assertNotEqual(p6.job["street_text_original"], p6.job["street_text"])

    def test_internal_whitespace_is_not_collapsed(self):
        # The alias is "学  园" (two spaces); a single space must not match.
        ok = self.svc.submit_new("W1", "C1", "东", "学  园", 2)
        self.assertEqual(ok.outcome, SUBMITTED)
        bad = self.svc.submit_new("W2", "C1", "东", "学 园", 2)
        self.assertEqual(bad.outcome, FAILED_STREET_UNKNOWN)


class RevisionFlowTests(MaterialServiceCase):
    def setUp(self):
        super().setUp()
        self.svc.import_csv(MATERIALS / "addresses.csv")
        self.version = self.apply_revision()

    def test_revision_bumps_version(self):
        self.assertEqual(self.version, 2)
        self.assertEqual(self.svc.catalog.revision, "r2")

    def test_retry_failed_parcels_reflect_current_catalog(self):
        results = {r.parcel_id: r for r in self.svc.retry(["P1", "P3", "P4"])}
        # P3: K3 removed -> only station A remains on S1 house 9.
        self.assertEqual(results["P3"].outcome, SUBMITTED)
        self.assertEqual(results["P3"].station_id, "A")
        self.assertEqual(results["P3"].job["catalog_version"], 2)
        # P4: S1 gained alias "东部支路" in r2.
        self.assertEqual(results["P4"].outcome, SUBMITTED)
        self.assertEqual(results["P4"].job["street_id"], "S1")
        # P1: still limited to two address candidates; deactivation/coverage
        # must not pick a district for us.
        self.assertEqual(results["P1"].outcome, FAILED_AMBIGUOUS_ADDRESS)

    def test_new_parcel_cannot_use_deactivated_street(self):
        result = self.svc.submit_new("P7", "C1", "西", "东路", 2)
        self.assertEqual(result.outcome, FAILED_STREET_INACTIVE)
        self.assertIsNone(self.svc.delivery.get_job("P7"))

    def test_replay_of_committed_parcel_ignores_revision(self):
        result = self.svc.replay_submitted("P2")
        self.assertEqual(result.outcome, REPLAYED)
        self.assertEqual(result.job["catalog_revision"], "r1")
        self.assertEqual(result.job["station_id"], "A")

    def test_repeated_csv_after_revision_replays_successes_and_retries_failures(self):
        batch = self.svc.import_csv(MATERIALS / "addresses.csv")
        outcomes = {r.parcel_id: r.outcome for r in batch.rows}
        # Committed parcels replay their frozen r1 fact...
        self.assertEqual(outcomes["P2"], REPLAYED)
        self.assertEqual(outcomes["P5"], REPLAYED)
        self.assertEqual(outcomes["P6"], REPLAYED)
        # ...while previously failed rows are re-attempted against r2.
        self.assertEqual(outcomes["P3"], SUBMITTED)
        self.assertEqual(outcomes["P4"], SUBMITTED)
        self.assertEqual(outcomes["P1"], FAILED_AMBIGUOUS_ADDRESS)
        # No job's version changed for the replayed parcels.
        self.assertEqual(self.svc.delivery.get_job("P2")["catalog_version"], 1)
        self.assertEqual(self.svc.delivery.get_job("P3")["catalog_version"], 2)


class IdentityFreezeTests(MaterialServiceCase):
    def setUp(self):
        super().setUp()
        self.svc.import_csv(MATERIALS / "addresses.csv")

    def test_identical_content_replays_even_with_different_source_row(self):
        item = AddressInput(
            "P2", "C1", "同区", "学园路", 2, source_file="elsewhere.csv", source_row=99
        )
        result = self.svc.delivery.attempt(item, self.svc.catalog)
        self.assertEqual(result.outcome, REPLAYED)
        # Frozen job keeps its original provenance.
        self.assertEqual(result.job["source_file"], str(MATERIALS / "addresses.csv"))
        self.assertEqual(result.job["source_row"], 3)

    def test_equivalent_unicode_and_trimming_replays(self):
        import unicodedata

        decomposed = unicodedata.normalize("NFD", "Café路")
        # city_id is a stable id (not normalised); district/street are.
        item = AddressInput("P6", "C1", " 东 ", decomposed, 2)
        result = self.svc.delivery.attempt(item, self.svc.catalog)
        self.assertEqual(result.outcome, REPLAYED)

    def test_different_house_number_rejected(self):
        result = self.svc.submit_new("P2", "C1", "同区", "学园路", 3)
        self.assertEqual(result.outcome, REJECTED_IDENTITY)
        # The stored job is untouched.
        job = self.svc.delivery.get_job("P2")
        self.assertEqual(job["house_no"], 2)

    def test_different_street_rejected(self):
        result = self.svc.submit_new("P2", "C1", "东", "东路", 2)
        self.assertEqual(result.outcome, REJECTED_IDENTITY)
        self.assertEqual(self.svc.delivery.get_job("P2")["street_id"], "S3")

    def test_different_city_rejected(self):
        result = self.svc.submit_new("P2", "C2", "同区", "学园路", 2)
        self.assertEqual(result.outcome, REJECTED_IDENTITY)

    def test_rename_and_deactivation_do_not_change_frozen_fact(self):
        self.apply_revision()  # adds S1 alias, deactivates S2, removes K3
        result = self.svc.replay_submitted("P2")
        self.assertEqual(result.outcome, REPLAYED)
        self.assertEqual(result.job["street_id"], "S3")
        self.assertEqual(result.job["station_id"], "A")

    def test_failure_never_overwrites_success(self):
        # P2 is committed. A retry must replay, not replace, even post-revision.
        self.apply_revision()
        result = self.svc.retry(["P2"])[0]
        self.assertEqual(result.outcome, REPLAYED)
        self.assertEqual(self.svc.delivery.get_job("P2")["catalog_version"], 1)

    def test_identity_excludes_source(self):
        # Same content from a different file/row replays; source is not identity.
        from address_delivery.normalization import make_identity

        self.assertEqual(
            make_identity("C1", " 东 ", "Café", 2),
            make_identity("C1", "东", "Café", 2),
        )
        self.assertNotEqual(make_identity("C1", "东", "学  园", 2), make_identity("C1", "东", "学 园", 2))


class AttemptAuditTests(MaterialServiceCase):
    def test_attempts_preserve_source_rows_and_business_results(self):
        self.svc.import_csv(MATERIALS / "addresses.csv")
        attempts = self.svc.delivery.get_attempts("P1")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["source_row"], 2)
        self.assertEqual(attempts[0]["outcome"], FAILED_AMBIGUOUS_ADDRESS)
        self.assertFalse(attempts[0]["job_created"])
        self.assertEqual(
            {c["street_id"] for c in attempts[0]["detail"]["resolution"]["candidates"]},
            {"S1", "S2"},
        )

    def test_failed_retry_appends_attempt_without_job(self):
        self.svc.import_csv(MATERIALS / "addresses.csv")
        self.svc.retry(["P1"])
        attempts = self.svc.delivery.get_attempts("P1")
        self.assertEqual(len(attempts), 2)
        self.assertIsNone(self.svc.delivery.get_job("P1"))

    def test_retry_only_successful_failed_then_replay(self):
        self.svc.import_csv(MATERIALS / "addresses.csv")
        self.apply_revision()
        self.svc.retry(["P4"])
        self.assertIsNotNone(self.svc.delivery.get_job("P4"))
        # A second retry now replays instead of re-resolving.
        result = self.svc.retry(["P4"])[0]
        self.assertEqual(result.outcome, REPLAYED)


if __name__ == "__main__":
    unittest.main()
