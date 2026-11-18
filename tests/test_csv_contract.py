"""CSV structural rejection, the address-input/v1 contract, and the client."""
from __future__ import annotations

import json
import subprocess
import sys
import unittest

from support import MATERIALS, MaterialServiceCase

from address_delivery.errors import InputFormatError
from address_delivery.input import (
    address_input_document,
    parse_address_input_document,
    parse_csv_text,
    read_csv_addresses,
)

HEADER = "parcel_id,city_id,district_text,street_text,house_no\n"


class CsvStructuralTests(unittest.TestCase):
    def assertRejected(self, text: str):
        with self.assertRaises(InputFormatError):
            parse_csv_text(text)

    def test_wrong_columns_rejected(self):
        self.assertRejected("parcel_id,city_id,street_text,house_no\nP1,C1,S,2\n")
        self.assertRejected("parcel_id,city_id,district_text,street_text,house_no,extra\nP1,C1,D,S,2,x\n")
        self.assertRejected("")

    def test_wrong_field_count_rejected(self):
        self.assertRejected(HEADER + "P1,C1,D\n")
        self.assertRejected(HEADER + "P1,C1,D,S,2,extra\n")

    def test_bad_house_numbers_rejected(self):
        for raw in ["0", "-1", "2.0", "two", " 2", "2 ", "２", "+2"]:
            with self.subTest(raw=raw):
                self.assertRejected(HEADER + f"P1,C1,D,S,{raw}\n")

    def test_empty_parcel_id_rejected(self):
        self.assertRejected(HEADER + ",C1,D,S,2\n")

    def test_whole_file_rejected_before_any_return(self):
        # A defect on the last row must reject the entire file.
        text = HEADER + "P1,C1,D,S,2\nP2,C1,D,S,oops\n"
        with self.assertRaises(InputFormatError):
            parse_csv_text(text)

    def test_quoted_comma_and_leading_space_preserved(self):
        items = parse_csv_text(HEADER + 'P1,C1, 东 ,"学,园",2\n')
        self.assertEqual(items[0].district_text, " 东 ")
        self.assertEqual(items[0].street_text, "学,园")
        self.assertEqual(items[0].source_row, 2)

    def test_source_rows_account_for_header(self):
        items = parse_csv_text(HEADER + "P1,C1,D,S,1\nP2,C1,D,S,2\n")
        self.assertEqual([i.source_row for i in items], [2, 3])


class AddressInputContractTests(unittest.TestCase):
    def _client_output(self) -> dict:
        proc = subprocess.run(
            [sys.executable, str(MATERIALS / "csv_client.py"), str(MATERIALS / "addresses.csv")],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(proc.stdout)

    def test_client_output_is_accepted(self):
        document = self._client_output()
        self.assertEqual(document["schema"], "address-input/v1")
        items = parse_address_input_document(document)
        self.assertEqual([i.parcel_id for i in items], ["P1", "P2", "P3", "P4", "P5", "P6"])
        self.assertEqual(items[0].source_row, 2)
        self.assertEqual(items[4].street_text, "学,园")

    def test_wrong_schema_rejected(self):
        with self.assertRaises(InputFormatError):
            parse_address_input_document({"schema": "other", "rows": []})

    def test_missing_address_field_rejected(self):
        document = {
            "schema": "address-input/v1",
            "rows": [{"source_row": 2, "address": {"parcel_id": "P1", "city_id": "C1",
                                                    "district_text": "D", "house_no": 2}}],
        }
        with self.assertRaises(InputFormatError):
            parse_address_input_document(document)

    def test_bad_house_number_in_document_rejected(self):
        document = {
            "schema": "address-input/v1",
            "rows": [{"source_row": 2, "address": {
                "parcel_id": "P1", "city_id": "C1", "district_text": "D",
                "street_text": "S", "house_no": 0}}],
        }
        with self.assertRaises(InputFormatError):
            parse_address_input_document(document)

    def test_roundtrip_document_preserves_field_meanings(self):
        items = parse_csv_text(HEADER + 'P1,C1,东,"学,园",2\n')
        doc = address_input_document(items)
        self.assertEqual(doc["schema"], "address-input/v1")
        self.assertEqual(doc["rows"][0]["address"]["street_text"], "学,园")
        reparsed = parse_address_input_document(doc)
        self.assertEqual(reparsed[0], items[0])

    def test_extra_fields_tolerated(self):
        document = {
            "schema": "address-input/v1",
            "rows": [{"source_row": 2, "kind": "address", "note": "x", "address": {
                "parcel_id": "P1", "city_id": "C1", "district_text": "D",
                "street_text": "S", "house_no": 2, "extra": True}}],
        }
        items = parse_address_input_document(document)
        self.assertEqual(items[0].house_no, 2)


class ClientIntegrationTests(MaterialServiceCase):
    def test_piped_client_output_imports(self):
        proc = subprocess.run(
            [sys.executable, str(MATERIALS / "csv_client.py"), str(MATERIALS / "addresses.csv")],
            capture_output=True,
            text=True,
            check=True,
        )
        document = json.loads(proc.stdout)
        batch = self.svc.import_address_document(document)
        outcomes = {r.parcel_id: r.outcome for r in batch.rows}
        self.assertEqual(outcomes["P2"], "submitted")
        self.assertEqual(outcomes["P1"], "failed_ambiguous_address")
        # Quotes/Unicode round-trip through the client unchanged.
        self.assertEqual(outcomes["P5"], "submitted")
        self.assertEqual(outcomes["P6"], "submitted")


if __name__ == "__main__":
    unittest.main()
