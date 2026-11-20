"""装箱库命令行入口 (独立于目录入口与接收入口).

用法示例:
  python -m boxship.store_cli --db run/store.db init --seed ../../materials/711/initial_store.json
  python -m boxship.store_cli --db run/store.db seal --box BOX_M
  python -m boxship.store_cli --db run/store.db move --item I3 --to BOX_M
  python -m boxship.store_cli --db run/store.db receive --input input.json
  python -m boxship.store_cli --db run/store.db export --box BOX_M --out box_m.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .core import PackError, PackStore


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="boxship.store_cli")
    p.add_argument("--db", required=True, help="SQLite 数据库路径")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="导入合成初始事实 (仅一次)")
    s.add_argument("--seed", required=True)

    sub.add_parser("summary", help="显示当前目录、箱子、发运")
    sub.add_parser("catalog", help="显示当前目录规则与冲突检查")

    s = sub.add_parser("create-box", help="创建开放箱")
    s.add_argument("--box", required=True)
    s.add_argument("--capacity", required=True, type=int)

    s = sub.add_parser("move", help="原子移动货物到开放箱")
    s.add_argument("--item", required=True)
    s.add_argument("--to", required=True)

    s = sub.add_parser("seal", help="按当前规则与实际内容封箱")
    s.add_argument("--box", required=True)

    s = sub.add_parser("unseal", help="拆封 (旧封箱身份失效)")
    s.add_argument("--box", required=True)

    s = sub.add_parser("apply-revision", help="切换规则修订; 只影响之后的封箱")
    s.add_argument("--file", required=True)

    s = sub.add_parser("export", help="导出某箱当前有效 sealed-box/v1 工件")
    s.add_argument("--box", required=True)
    s.add_argument("--out")

    s = sub.add_parser("export-bundle", help="导出所有 SEALED 箱的工件集合")
    s.add_argument("--shipment-id", default="PREVIEW")
    s.add_argument("--out")

    s = sub.add_parser("receive", help="接收外部 shipment-input/v1 文件并与库核对")
    s.add_argument("--input", required=True)

    s = sub.add_parser("replay", help="读取已成功发运记录")
    s.add_argument("--shipment-id", required=True)

    s = sub.add_parser("diagnose", help="显示箱子的当前状态/有效封箱/按当时规则复核")
    s.add_argument("--box", required=True)

    s = sub.add_parser("items", help="列出货物及位置")
    return p


def cmd_catalog(store: PackStore) -> dict:
    cat = store.current_catalog()
    out = {"revision": cat.revision, "incompatible_pairs": cat.pairs(), "boxes": []}
    for b in store.summary()["boxes"]:
        contents = store.box_contents(b["box_id"])
        conflicts = cat.check([(c["item_id"], c["category"]) for c in contents])
        out["boxes"].append({
            "box_id": b["box_id"],
            "status": b["status"],
            "counts": _counts(contents),
            "current_rule_conflicts": [c.as_dict() for c in conflicts],
        })
    return out


def _counts(contents: list[dict]) -> dict:
    out: dict[str, int] = {}
    for c in contents:
        out[c["category"]] = out.get(c["category"], 0) + 1
    return out


def cmd_items(store: PackStore) -> list[dict]:
    rows = store.conn.execute(
        "SELECT i.item_id,i.category,i.weight,l.box_id,b.status FROM items i "
        "LEFT JOIN item_location l ON l.item_id=i.item_id "
        "LEFT JOIN boxes b ON b.box_id=l.box_id ORDER BY i.item_id"
    ).fetchall()
    return [dict(r) for r in rows]


def run(store: PackStore, args) -> object:
    if args.cmd == "init":
        return store.import_seed(_read_json(args.seed))
    if args.cmd == "summary":
        return store.summary()
    if args.cmd == "catalog":
        return cmd_catalog(store)
    if args.cmd == "create-box":
        return store.create_box(args.box, args.capacity)
    if args.cmd == "move":
        return store.move_item(args.item, args.to)
    if args.cmd == "seal":
        return store.seal_box(args.box)
    if args.cmd == "unseal":
        return store.unseal_box(args.box)
    if args.cmd == "apply-revision":
        return store.apply_revision(_read_json(args.file))
    if args.cmd == "export":
        artifact = store.export_seal(args.box)
        if args.out:
            Path(args.out).write_text(
                json.dumps(artifact, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            return {"ok": True, "written": args.out, "seal_id": artifact["seal_id"]}
        return artifact
    if args.cmd == "export-bundle":
        bundle = store.export_manifests(args.shipment_id)
        if args.out:
            Path(args.out).write_text(
                json.dumps(bundle, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            return {"ok": True, "written": args.out,
                    "boxes": [m["box_id"] for m in bundle["manifests"]]}
        return bundle
    if args.cmd == "receive":
        return store.receive_shipment_input(_read_json(args.input))
    if args.cmd == "replay":
        return store.replay_shipment(args.shipment_id)
    if args.cmd == "diagnose":
        return store.diagnose_box(args.box)
    if args.cmd == "items":
        return cmd_items(store)
    raise PackError("unknown_command", args.cmd)


def _read_json(path: str):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        with PackStore(args.db) as store:
            result = run(store, args)
    except PackError as e:
        _print(e.as_dict())
        return 1
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as e:
        _print({"ok": False, "error": "bad_input", "message": str(e)})
        return 1
    _print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
