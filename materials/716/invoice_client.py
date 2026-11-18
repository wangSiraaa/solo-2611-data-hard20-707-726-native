"""Read a published order receipt; this client does not choose prices."""
import csv
import json
import sys
from collections import Counter
from pathlib import Path


def read_invoice(path):
    receipt = json.loads(Path(path).read_text(encoding="utf-8"))
    if receipt.get("format") != "order-receipt/v1":
        raise ValueError("unsupported receipt format")
    lines = receipt["lines"]
    seen = set()
    totals = Counter()
    for line in lines:
        if line["line_id"] in seen:
            raise ValueError("duplicate line identity")
        seen.add(line["line_id"])
        for field in ("quantity", "total_sku_quantity", "unit_price", "amount"):
            if type(line[field]) is not int or line[field] < 0:
                raise ValueError("invalid integer amount or quantity")
        if not line["quantity"] or not line["price_ids"]:
            raise ValueError("empty quantity or price evidence")
        if line["quantity"] * line["unit_price"] != line["amount"]:
            raise ValueError("line amount mismatch")
        totals[line["sku"]] += line["quantity"]
    if any(x["total_sku_quantity"] != totals[x["sku"]] for x in lines):
        raise ValueError("SKU quantity evidence mismatch")
    if sum(x["amount"] for x in lines) != receipt["total_amount"]:
        raise ValueError("order total mismatch")
    return receipt


def main():
    receipt = read_invoice(sys.argv[1])
    writer = csv.writer(sys.stdout)
    writer.writerow(["order_id", "catalog_version", "line_id", "sku", "quantity", "unit_price", "amount"])
    for line in sorted(receipt["lines"], key=lambda x: x["line_id"]):
        writer.writerow([receipt["order_id"], receipt["catalog_version"], line["line_id"], line["sku"], line["quantity"], line["unit_price"], line["amount"]])


if __name__ == "__main__":
    main()
