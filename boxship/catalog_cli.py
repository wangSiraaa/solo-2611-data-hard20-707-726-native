"""相容目录独立入口.

只做规则判定, 不接触箱子/发运状态。可用于对任意多重集合做冲突检查,
或对某个规则文件与另一规则文件做对比。

用法:
  python -m boxship.catalog_cli --catalog ../../materials/711/initial_store.json --pairs
  python -m boxship.catalog_cli --catalog ../../materials/711/initial_store.json \
      --items I2:A I4:A
  python -m boxship.catalog_cli --catalog ../../materials/711/revision.json \
      --manifest ../../materials/711/handoff/a.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .catalog import Catalog


def load_catalog(path: str) -> Catalog:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if "catalog" in doc:  # packing-seed 中嵌套 catalog
        doc = doc["catalog"]
    return Catalog.from_document(doc)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="boxship.catalog_cli")
    p.add_argument("--catalog", required=True,
                   help="catalog JSON 或 packing-seed JSON (取其中的 catalog)")
    p.add_argument("--items", nargs="*", default=[],
                   help="类别多重集合, 形如 I2:A I4:A (item_id:category)")
    p.add_argument("--manifest", help="从 sealed-box/v1 工件读取 contents")
    p.add_argument("--pairs", action="store_true", help="只打印规则")
    args = p.parse_args(argv)

    cat = load_catalog(args.catalog)
    if args.pairs:
        print(json.dumps({"revision": cat.revision, "incompatible_pairs": cat.pairs()},
                         ensure_ascii=False, indent=2))
        return 0

    entries = []
    for token in args.items:
        item_id, sep, category = token.partition(":")
        if not sep or not category:
            raise SystemExit(f"--items entries must be item_id:category: {token}")
        entries.append((item_id or category, category))
    if args.manifest:
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        entries.extend((c["item_id"], c["category"]) for c in manifest["contents"])

    counts: dict[str, int] = {}
    for _, category in entries:
        counts[category] = counts.get(category, 0) + 1
    conflicts = cat.check(entries)
    print(json.dumps({
        "revision": cat.revision,
        "item_count": len(entries),
        "category_counts": dict(sorted(counts.items())),
        "compatible": not conflicts,
        "conflicts": [c.as_dict() for c in conflicts],
    }, ensure_ascii=False, indent=2))
    return 0 if not conflicts else 2


if __name__ == "__main__":
    raise SystemExit(main())
