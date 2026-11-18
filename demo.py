#!/usr/bin/env python3
"""End-to-end demo using materials/721 and the read-only consumer.

Produces ``demo/out/before.json`` and ``demo/out/after.json`` as
classification-summary/1 products, then shows that ``consumer.py`` reads both.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MATERIALS = ROOT / "materials" / "721"
OUT = ROOT / "demo" / "out"

sys.path.insert(0, str(ROOT))

from cs.store import Store  # noqa: E402


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def dump(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    db_path = OUT / "demo.db"
    if db_path.exists():
        db_path.unlink()

    queries = load_json(MATERIALS / "queries.json")["requests"]
    targets = []
    for request in queries:
        for target in request["targets"]:
            if target not in targets:
                targets.append(target)

    with Store.open(str(db_path)) as store:
        store.init_catalog(load_json(MATERIALS / "catalog.json"))
        facts = [
            {
                "ticket_id": row["ticket_id"],
                "revision": row["revision"],
                "amount": row["amount"],
                "classes": row["classes"],
            }
            for row in load_jsonl(MATERIALS / "facts.jsonl")
        ]
        store.apply_fact_revisions(facts, request_id="seed-facts")

        before = store.publish(list(targets), name="demo-before", product_id="demo-before")
        dump(OUT / "before.json", before["payload"])

        accepted = []
        rejected = []
        for operation in load_jsonl(MATERIALS / "operations.jsonl"):
            try:
                result = store.submit(operation)
                accepted.append({"request_id": operation.get("request_id"), **result.as_dict()})
            except Exception as exc:  # noqa: BLE001 - demo reporting
                rejected.append(
                    {
                        "request_id": operation.get("request_id"),
                        "error": type(exc).__name__,
                        "message": str(exc),
                    }
                )

        after = store.publish(list(targets), name="demo-after", product_id="demo-after")
        dump(OUT / "after.json", after["payload"])

        # Re-read the before product to prove it was not rewritten.
        dump(OUT / "before_reread.json", store.get_product("demo-before")["payload"])

    report = {
        "before_cut": before["payload"]["cut"],
        "after_cut": after["payload"]["cut"],
        "accepted": accepted,
        "rejected": rejected,
        "products_unchanged": before["payload"] == load_json(OUT / "before_reread.json"),
    }
    dump(OUT / "demo_report.json", report)

    print("== demo report ==")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("\n== consumer.py before.json ==")
    subprocess.run(
        [sys.executable, str(MATERIALS / "consumer.py"), str(OUT / "before.json")],
        check=True,
    )
    print("\n== consumer.py after.json ==")
    subprocess.run(
        [sys.executable, str(MATERIALS / "consumer.py"), str(OUT / "after.json")],
        check=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
