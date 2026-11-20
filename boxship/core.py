"""持久化装箱库 + 独立发运接收.

设计要点:
- 箱子与货物位置是唯一权威状态 (boxes.current_seal_id / shipments)。
- sealed_boxes 是可信封箱记录: 只追加, 每行是封箱当时的完整事实快照
  (规则修订、容量、完整身份/类别/重量、总重)。
- 接收只读根目录下的外部交接文件, 再与持久库比对; 不信任工件自洽性。
- 同一申请的所有凭据核验与归属在同一事务内完成, 全成或全不成。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .catalog import Catalog, Conflict

SCHEMAS = {
    "sealed": "sealed-box/v1",
    "shipment_input": "shipment-input/v1",
    "handoff": "handoff-request/v1",
    "seed": "packing-seed/v1",
}

MAX_ITEMS = 100
MAX_BOXES = 20


class PackError(Exception):
    """业务拒绝: 携带稳定错误码, 供 CLI 输出并以非零退出."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> dict:
        return {"ok": False, "error": self.code, "message": self.message}


class PackStore:
    def __init__(self, db_path: str | Path):
        self.path = str(db_path)
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    # ---------- 基础事务 ----------
    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "PackStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS catalogs (
                revision TEXT PRIMARY KEY,
                pairs_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS items (
                item_id TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                weight INTEGER NOT NULL CHECK (weight > 0)
            );
            CREATE TABLE IF NOT EXISTS boxes (
                box_id TEXT PRIMARY KEY,
                capacity INTEGER NOT NULL CHECK (capacity > 0),
                status TEXT NOT NULL CHECK (status IN ('OPEN','SEALED','SHIPPED')),
                current_seal_id TEXT
            );
            CREATE TABLE IF NOT EXISTS item_location (
                item_id TEXT PRIMARY KEY REFERENCES items(item_id),
                box_id  TEXT NOT NULL REFERENCES boxes(box_id),
                UNIQUE (item_id, box_id)
            );
            CREATE TABLE IF NOT EXISTS sealed_boxes (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                seal_id TEXT NOT NULL UNIQUE,
                box_id TEXT NOT NULL REFERENCES boxes(box_id),
                rule_revision TEXT NOT NULL,
                capacity INTEGER NOT NULL,
                contents_json TEXT NOT NULL,
                total_weight INTEGER NOT NULL,
                record_json TEXT NOT NULL,
                live INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_live_seal_per_box
                ON sealed_boxes(box_id) WHERE live = 1;
            """
        )
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS shipments (
                shipment_id TEXT PRIMARY KEY,
                canonical_json TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS shipment_boxes (
                shipment_id TEXT NOT NULL REFERENCES shipments(shipment_id),
                box_id TEXT NOT NULL,
                seal_id TEXT NOT NULL,
                PRIMARY KEY (shipment_id, box_id)
            );
            -- 一个封箱在同一时刻至多归属一个发运; 拆封会使行残留但状态失配,
            -- 发运使用历史归属由 boxes.status='SHIPPED' 把关。
            CREATE UNIQUE INDEX IF NOT EXISTS one_shipment_per_box
                ON shipment_boxes(box_id);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                detail_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )

    def _tx(self):
        return _Tx(self.conn)

    def _event(self, kind: str, detail: dict) -> None:
        self.conn.execute(
            "INSERT INTO events(kind, detail_json) VALUES (?, ?)",
            (kind, _canonical(detail)),
        )

    def _get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # ---------- 初始事实导入 ----------
    def import_seed(self, seed: dict) -> dict:
        """导入合成初始状态; 只允许一次 (已有 meta.items_loaded 则拒绝)."""
        if not isinstance(seed, dict) or seed.get("schema") != SCHEMAS["seed"]:
            raise PackError("invalid_seed", "seed.schema must be packing-seed/v1")

        items = seed.get("items") or []
        boxes = seed.get("boxes") or []
        seals = seed.get("trusted_seals") or []
        catalog_doc = seed.get("catalog") or {}
        shipments = seed.get("shipments") or []
        if len(items) > MAX_ITEMS:
            raise PackError("too_many_items", f"at most {MAX_ITEMS} items")
        if len(boxes) > MAX_BOXES:
            raise PackError("too_many_boxes", f"at most {MAX_BOXES} boxes")

        with self._tx():
            if self._get_meta("items_loaded") is not None:
                raise PackError("already_imported", "seed already imported into this store")
            catalog = Catalog.from_document(catalog_doc)
            self._save_catalog(catalog)
            self._set_meta("catalog_current", catalog.revision)

            seen_items: set[str] = set()
            for it in items:
                if not _valid_item(it):
                    raise PackError("invalid_item", f"bad item: {it!r}")
                if it["item_id"] in seen_items:
                    raise PackError("duplicate_item", f"duplicate item_id {it['item_id']}")
                seen_items.add(it["item_id"])
                self.conn.execute(
                    "INSERT INTO items(item_id,category,weight) VALUES (?,?,?)",
                    (it["item_id"], it["category"], int(it["weight"])),
                )
            self._set_meta("items_loaded", "1")
            self._set_meta("seal_counter", "0")

            seen_boxes: set[str] = set()
            placed: set[str] = set()
            for b in boxes:
                if not isinstance(b, dict):
                    raise PackError("invalid_box", f"bad box: {b!r}")
                box_id = b.get("box_id")
                cap = b.get("capacity")
                status = b.get("status")
                if not isinstance(box_id, str) or not box_id:
                    raise PackError("invalid_box", "box_id required")
                if box_id in seen_boxes:
                    raise PackError("duplicate_box", f"duplicate box {box_id}")
                if not _positive_int(cap):
                    raise PackError("invalid_capacity", f"bad capacity for {box_id}")
                if status not in ("OPEN", "SEALED"):
                    raise PackError("invalid_status", f"bad seed status for {box_id}")
                seen_boxes.add(box_id)
                self.conn.execute(
                    "INSERT INTO boxes(box_id,capacity,status,current_seal_id) "
                    "VALUES (?,?,?,NULL)",
                    (box_id, cap, status),
                )
                ids = b.get("item_ids") or []
                if not isinstance(ids, list):
                    raise PackError("invalid_box", f"item_ids must be a list: {box_id}")
                if len(ids) != len(set(ids)):
                    raise PackError("duplicate_item_in_box", f"{box_id} repeats an item")
                for item_id in ids:
                    if item_id not in seen_items:
                        raise PackError("unknown_item", f"{box_id} has unknown {item_id}")
                    if item_id in placed:
                        raise PackError("item_in_two_boxes", f"{item_id} appears twice")
                    placed.add(item_id)
                    self.conn.execute(
                        "INSERT INTO item_location(item_id,box_id) VALUES (?,?)",
                        (item_id, box_id),
                    )
                if status == "SEALED":
                    seal_id = b.get("seal_id")
                    if not isinstance(seal_id, str) or not seal_id:
                        raise PackError("sealed_without_seal", f"{box_id} needs seal_id")
                    # 该封箱必须以可信记录形式存在于 seed 中
                    record = next((s for s in seals if s.get("seal_id") == seal_id), None)
                    if record is None:
                        raise PackError(
                            "missing_trusted_seal",
                            f"no trusted_seal for {box_id}/{seal_id}",
                        )
                    self._import_trusted_seal(record, expected_box=box_id)
                    # 可信记录必须与初始实际内容一致, 否则导入的“事实”自相矛盾
                    actual = sorted(self.box_contents(box_id), key=lambda c: c["item_id"])
                    if actual != record_expected_contents(record):
                        raise PackError(
                            "seed_seal_mismatch",
                            f"{box_id} actual contents differ from trusted seal {seal_id}",
                        )
                    if int(record.get("capacity", -1)) != int(cap):
                        raise PackError(
                            "seed_seal_mismatch",
                            f"{box_id} capacity differs from trusted seal {seal_id}",
                        )
                    self.conn.execute(
                        "UPDATE boxes SET current_seal_id=? WHERE box_id=?",
                        (seal_id, box_id),
                    )

            known_seals = {b.get("seal_id") for b in boxes if b.get("status") == "SEALED"}
            for s in seals:
                if s.get("seal_id") not in known_seals:
                    raise PackError(
                        "orphan_trusted_seal",
                        f"trusted_seal {s.get('seal_id')} has no sealed box in seed",
                    )

            for sh in shipments:
                self._import_shipment(sh)
            self._event("import_seed", {"catalog": catalog.revision,
                                        "items": len(items), "boxes": len(boxes)})
        return self.summary()

    def _import_trusted_seal(self, record: dict, expected_box: str) -> None:
        if not isinstance(record, dict) or record.get("schema") != SCHEMAS["sealed"]:
            raise PackError("invalid_trusted_seal", "trusted seal schema mismatch")
        seal_id = record.get("seal_id")
        box_id = record.get("box_id")
        if not isinstance(seal_id, str) or not seal_id or box_id != expected_box:
            raise PackError("invalid_trusted_seal", "seal/box mismatch in seed")
        canonical = canonical_sealed_box(record)
        self._insert_sealed_row(canonical, live=True)
        self._bump_seal_counter(seal_id)

    def _insert_sealed_row(self, artifact: dict, live: bool) -> None:
        self.conn.execute(
            "INSERT INTO sealed_boxes"
            "(seal_id,box_id,rule_revision,capacity,contents_json,total_weight,"
            " record_json,live) VALUES (?,?,?,?,?,?,?,?)",
            (
                artifact["seal_id"], artifact["box_id"], artifact["rule_revision"],
                artifact["capacity"], _canonical(artifact["contents"]),
                artifact["total_weight"], _canonical(artifact), 1 if live else 0,
            ),
        )

    def _bump_seal_counter(self, seal_id: str) -> None:
        # 计数器保证新 seal_id 永不与任何已导入/已生成者相同
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES ('seen_seal:'||?, '1') "
            "ON CONFLICT(key) DO NOTHING",
            (seal_id,),
        )

    def _save_catalog(self, catalog: Catalog) -> None:
        row = self.conn.execute(
            "SELECT pairs_json FROM catalogs WHERE revision=?", (catalog.revision,)
        ).fetchone()
        if row is not None:
            if json.loads(row["pairs_json"]) != catalog.pairs():
                # 同一修订号不得指向不同规则, 否则“当时规则”复核会含糊
                raise PackError(
                    "revision_redefined",
                    f"revision {catalog.revision} already exists with different pairs",
                )
            return
        self.conn.execute(
            "INSERT INTO catalogs(revision,pairs_json) VALUES (?,?)",
            (catalog.revision, _canonical(catalog.pairs())),
        )

    # ---------- 查询 ----------
    def current_catalog(self) -> Catalog:
        revision = self._get_meta("catalog_current")
        if revision is None:
            raise PackError("no_catalog", "catalog not imported")
        row = self.conn.execute(
            "SELECT pairs_json FROM catalogs WHERE revision=?", (revision,)
        ).fetchone()
        return Catalog(revision, json.loads(row["pairs_json"]))

    def get_catalog(self, revision: str) -> Catalog | None:
        row = self.conn.execute(
            "SELECT pairs_json FROM catalogs WHERE revision=?", (revision,)
        ).fetchone()
        return None if row is None else Catalog(revision, json.loads(row["pairs_json"]))

    def box(self, box_id: str) -> dict:
        row = self.conn.execute("SELECT * FROM boxes WHERE box_id=?", (box_id,)).fetchone()
        if row is None:
            raise PackError("unknown_box", f"no box {box_id}")
        return dict(row)

    def box_contents(self, box_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT i.item_id,i.category,i.weight FROM item_location l "
            "JOIN items i ON i.item_id=l.item_id WHERE l.box_id=? ORDER BY i.item_id",
            (box_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def item(self, item_id: str) -> dict:
        row = self.conn.execute("SELECT * FROM items WHERE item_id=?", (item_id,)).fetchone()
        if row is None:
            raise PackError("unknown_item", f"no item {item_id}")
        return dict(row)

    def item_location(self, item_id: str) -> dict:
        row = self.conn.execute(
            "SELECT l.box_id,b.status,b.current_seal_id FROM item_location l "
            "JOIN boxes b ON b.box_id=l.box_id WHERE l.item_id=?",
            (item_id,),
        ).fetchone()
        if row is None:
            raise PackError("unanchored_item", f"{item_id} is not in any box")
        return dict(row)

    def _total_weight(self, box_id: str) -> int:
        r = self.conn.execute(
            "SELECT COALESCE(SUM(i.weight),0) AS w FROM item_location l "
            "JOIN items i ON i.item_id=l.item_id WHERE l.box_id=?",
            (box_id,),
        ).fetchone()
        return int(r["w"])

    def summary(self) -> dict:
        boxes = [dict(r) for r in self.conn.execute(
            "SELECT box_id,capacity,status,current_seal_id FROM boxes ORDER BY box_id"
        )]
        shipments = [dict(r) for r in self.conn.execute(
            "SELECT shipment_id FROM shipments ORDER BY shipment_id"
        )]
        return {
            "catalog": self._get_meta("catalog_current"),
            "items": self.conn.execute("SELECT COUNT(*) c FROM items").fetchone()["c"],
            "boxes": boxes,
            "shipments": shipments,
        }

    # ---------- 目录修订 ----------
    def apply_revision(self, doc: dict) -> dict:
        """新增/切换当前规则修订; 只影响之后的封箱, 不改已封箱/已发运事实."""
        if not isinstance(doc, dict):
            raise PackError("invalid_revision", "revision must be an object")
        catalog = Catalog.from_document(doc)
        with self._tx():
            self._save_catalog(catalog)
            previous = self._get_meta("catalog_current")
            self._set_meta("catalog_current", catalog.revision)
            self._event("apply_revision",
                        {"from": previous, "to": catalog.revision,
                         "pairs": catalog.pairs()})
        return {"ok": True, "revision": catalog.revision, "pairs": catalog.pairs()}

    # ---------- 装箱 ----------
    def create_box(self, box_id: str, capacity: int) -> dict:
        if not isinstance(box_id, str) or not box_id:
            raise PackError("invalid_box_id", "box_id required")
        if not _positive_int(capacity):
            raise PackError("invalid_capacity", "capacity must be a positive integer")
        with self._tx():
            count = self.conn.execute("SELECT COUNT(*) c FROM boxes").fetchone()["c"]
            if count >= MAX_BOXES:
                raise PackError("too_many_boxes", f"at most {MAX_BOXES} boxes")
            if self.conn.execute(
                "SELECT 1 FROM boxes WHERE box_id=?", (box_id,)
            ).fetchone():
                raise PackError("box_exists", f"{box_id} already exists")
            self.conn.execute(
                "INSERT INTO boxes(box_id,capacity,status) VALUES (?,?,'OPEN')",
                (box_id, capacity),
            )
            self._event("create_box", {"box_id": box_id, "capacity": capacity})
        return {"ok": True, "box_id": box_id, "capacity": capacity, "status": "OPEN"}

    def move_item(self, item_id: str, target_box: str) -> dict:
        """原子移动: 先移出原箱再移入目标; 任何不合法整体不变.

        目标为 OPEN 时允许暂存规则冲突 (封箱时再校验);
        目标不能是 SEALED / SHIPPED 箱。跨箱总重超容量 -> 拒绝且原位置不变。
        """
        with self._tx():
            item = self.conn.execute(
                "SELECT item_id,category,weight FROM items WHERE item_id=?", (item_id,)
            ).fetchone()
            if item is None:
                raise PackError("unknown_item", f"no item {item_id}")
            src = self.conn.execute(
                "SELECT * FROM boxes WHERE box_id=(SELECT box_id FROM item_location "
                "WHERE item_id=?)", (item_id,)
            ).fetchone()
            if src is None:
                raise PackError("unanchored_item", f"{item_id} is not in any box")
            dst = self.conn.execute(
                "SELECT * FROM boxes WHERE box_id=?", (target_box,)
            ).fetchone()
            if dst is None:
                raise PackError("unknown_box", f"no box {target_box}")
            if dst["status"] != "OPEN":
                raise PackError(
                    "box_not_open",
                    f"{target_box} is {dst['status']}; only OPEN boxes accept moves",
                )
            if src["box_id"] == dst["box_id"]:
                raise PackError("same_box", f"{item_id} already in {target_box}")
            if src["status"] != "OPEN":
                # 已封箱/已发运的内容不能绕过拆封直接搬走
                raise PackError(
                    "source_not_open",
                    f"{src['box_id']} is {src['status']}; unseal it first",
                )
            projected = self._total_weight(target_box) + int(item["weight"])
            if projected > dst["capacity"]:
                raise PackError(
                    "capacity_exceeded",
                    f"{target_box} would weigh {projected} > capacity {dst['capacity']}",
                )
            # 原子: 删除 + 插入在同一事务内
            self.conn.execute("DELETE FROM item_location WHERE item_id=?", (item_id,))
            self.conn.execute(
                "INSERT INTO item_location(item_id,box_id) VALUES (?,?)",
                (item_id, target_box),
            )
            self._event("move_item",
                        {"item_id": item_id, "from": src["box_id"], "to": target_box})
        return {"ok": True, "item_id": item_id, "from": src["box_id"], "to": target_box}

    def seal_box(self, box_id: str) -> dict:
        """按当前规则与实际内容封箱; 失败保持 OPEN; 成功生成不可复用 seal_id."""
        with self._tx():
            box = self.conn.execute(
                "SELECT * FROM boxes WHERE box_id=?", (box_id,)
            ).fetchone()
            if box is None:
                raise PackError("unknown_box", f"no box {box_id}")
            if box["status"] != "OPEN":
                raise PackError(
                    "box_not_open", f"{box_id} is {box['status']}, cannot seal"
                )
            contents = self.box_contents(box_id)
            if not contents:
                raise PackError("empty_box", f"{box_id} has no contents")
            total = sum(int(c["weight"]) for c in contents)
            if total > int(box["capacity"]):
                raise PackError(
                    "capacity_exceeded",
                    f"{box_id} weighs {total} > capacity {box['capacity']}",
                )
            catalog = self.current_catalog()
            conflicts = catalog.check([(c["item_id"], c["category"]) for c in contents])
            if conflicts:
                detail = [c.as_dict() for c in conflicts]
                self._event("seal_rejected",
                            {"box_id": box_id, "revision": catalog.revision,
                             "conflicts": detail})
                raise PackError(
                    "conflict",
                    f"{box_id} violates {catalog.revision}: "
                    + ", ".join(str(c) for c in conflicts),
                )
            seal_id = self._new_seal_id(catalog.revision, box_id)
            artifact = {
                "schema": SCHEMAS["sealed"],
                "seal_id": seal_id,
                "box_id": box_id,
                "rule_revision": catalog.revision,
                "capacity": int(box["capacity"]),
                "contents": sorted(contents, key=lambda c: c["item_id"]),
                "total_weight": total,
            }
            canonical = canonical_sealed_box(artifact)
            if self.conn.execute(
                "SELECT 1 FROM sealed_boxes WHERE seal_id=?", (seal_id,)
            ).fetchone():
                raise PackError("seal_id_collision", f"{seal_id} already exists")
            self._insert_sealed_row(canonical, live=True)
            self._bump_seal_counter(seal_id)
            self.conn.execute(
                "UPDATE boxes SET status='SEALED', current_seal_id=? WHERE box_id=?",
                (seal_id, box_id),
            )
            self._event("seal_box",
                        {"box_id": box_id, "seal_id": seal_id,
                         "revision": catalog.revision, "total_weight": total})
        return {"ok": True, "box_id": box_id, "seal_id": seal_id,
                "rule_revision": catalog.revision, "total_weight": total}

    def _new_seal_id(self, revision: str, box_id: str) -> str:
        # 递增序号 + 名称, 并确保从未使用 (导入/生成的历史 seal 均已登记)
        n = int(self._get_meta("seal_counter") or "0")
        while True:
            n += 1
            candidate = f"SEAL_{box_id}_{revision}_{n}"
            exists = self.conn.execute(
                "SELECT 1 FROM sealed_boxes WHERE seal_id=? "
                "UNION SELECT 1 FROM meta WHERE key=?",
                (candidate, f"seen_seal:{candidate}"),
            ).fetchone()
            if not exists:
                self._set_meta("seal_counter", str(n))
                return candidate

    def unseal_box(self, box_id: str) -> dict:
        """拆封: 旧封箱身份失效; 已发运箱不可拆改."""
        with self._tx():
            box = self.conn.execute(
                "SELECT * FROM boxes WHERE box_id=?", (box_id,)
            ).fetchone()
            if box is None:
                raise PackError("unknown_box", f"no box {box_id}")
            if box["status"] == "SHIPPED":
                raise PackError("shipped_box", f"{box_id} is shipped; cannot unseal")
            if box["status"] != "SEALED":
                raise PackError("box_not_sealed", f"{box_id} is {box['status']}")
            old_seal = box["current_seal_id"]
            self.conn.execute(
                "UPDATE sealed_boxes SET live=0 WHERE seal_id=?", (old_seal,)
            )
            self.conn.execute(
                "UPDATE boxes SET status='OPEN', current_seal_id=NULL WHERE box_id=?",
                (box_id,),
            )
            self._event("unseal_box", {"box_id": box_id, "invalidated": old_seal})
        return {"ok": True, "box_id": box_id, "invalidated_seal": old_seal,
                "status": "OPEN"}

    # ---------- 导出 ----------
    def export_seal(self, box_id: str) -> dict:
        """导出当前有效封箱工件, 供独立接收方读取 (导出不改变状态)."""
        row = self.conn.execute(
            "SELECT s.record_json FROM boxes b JOIN sealed_boxes s "
            "ON s.seal_id=b.current_seal_id WHERE b.box_id=? AND b.status='SEALED' "
            "AND s.live=1",
            (box_id,),
        ).fetchone()
        if row is None:
            raise PackError("no_live_seal", f"{box_id} has no effective seal to export")
        return json.loads(row["record_json"])

    def export_manifests(self, shipment_id: str) -> dict:
        boxes = self.conn.execute(
            "SELECT box_id FROM boxes WHERE status='SEALED' ORDER BY box_id"
        ).fetchall()
        return {
            "schema": "export-bundle/v1",
            "shipment_id": shipment_id,
            "manifests": [self.export_seal(r["box_id"]) for r in boxes],
        }

    # ---------- 发运接收 ----------
    def receive_shipment_input(self, payload: Any) -> dict:
        """核验 shipment-input/v1 (外部不可信输入) 并归属箱子.

        同 shipment_id 规范内容复读原结果; 异文拒绝。
        所有箱子凭据核验 + 归属在同一事务内; 任一失败则全部不占用。
        """
        if not isinstance(payload, dict) or payload.get("schema") != SCHEMAS["shipment_input"]:
            raise PackError("bad_schema", "expected shipment-input/v1")
        shipment_id, canonical_manifests, canonical = _canonicalize_shipment_input(payload)

        with self._tx():
            prior = self.conn.execute(
                "SELECT canonical_json,result_json FROM shipments WHERE shipment_id=?",
                (shipment_id,),
            ).fetchone()
            if prior is not None:
                if prior["canonical_json"] == canonical:
                    return {"ok": True, "replayed": True,
                            **json.loads(prior["result_json"])}
                raise PackError(
                    "shipment_conflict",
                    f"{shipment_id} already committed with different content",
                )

            for m in canonical_manifests:
                self._verify_manifest_against_store(m)

            # 到这里所有凭据已核验; 在同一事务内归属
            result = {
                "shipment_id": shipment_id,
                "boxes": [m["box_id"] for m in canonical_manifests],
                "seals": {m["box_id"]: m["seal_id"] for m in canonical_manifests},
            }
            self.conn.execute(
                "INSERT INTO shipments(shipment_id,canonical_json,result_json) "
                "VALUES (?,?,?)",
                (shipment_id, canonical, _canonical(result)),
            )
            for m in canonical_manifests:
                self.conn.execute(
                    "UPDATE boxes SET status='SHIPPED' WHERE box_id=?",
                    (m["box_id"],),
                )
                self.conn.execute(
                    "INSERT INTO shipment_boxes(shipment_id,box_id,seal_id) "
                    "VALUES (?,?,?)",
                    (shipment_id, m["box_id"], m["seal_id"]),
                )
            self._event("receive_shipment", result)
        return {"ok": True, "replayed": False, **result}

    def _verify_manifest_against_store(self, m: dict) -> None:
        box_id = m["box_id"]
        seal_id = m["seal_id"]
        box = self.conn.execute(
            "SELECT * FROM boxes WHERE box_id=?", (box_id,)
        ).fetchone()
        if box is None:
            raise PackError("unknown_box_manifest", f"unknown box {box_id}")
        if box["status"] == "SHIPPED":
            raise PackError("box_already_shipped", f"{box_id} already shipped")
        if box["status"] != "SEALED" or not box["current_seal_id"]:
            raise PackError("box_not_sealed", f"{box_id} has no effective seal")
        if box["current_seal_id"] != seal_id:
            raise PackError(
                "stale_or_unknown_seal",
                f"{box_id}: presented {seal_id}, current {box['current_seal_id']}",
            )
        trusted = self.conn.execute(
            "SELECT record_json FROM sealed_boxes WHERE seal_id=? AND live=1",
            (seal_id,),
        ).fetchone()
        if trusted is None:
            raise PackError("unknown_seal", f"no live trusted record for {seal_id}")
        if json.loads(trusted["record_json"]) != m:
            raise PackError(
                "manifest_mismatch",
                f"{seal_id} content differs from trusted seal record",
            )
        # 已有合法封箱按“当时规则”复核实际内容, 不用当前规则追溯
        catalog = self.get_catalog(m["rule_revision"])
        if catalog is None:
            raise PackError("unknown_rule", f"rule revision {m['rule_revision']} unknown")
        live_contents = self.box_contents(box_id)
        if live_contents != sorted(m["contents"], key=lambda c: c["item_id"]):
            raise PackError(
                "contents_drift",
                f"{box_id} actual contents differ from sealed record",
            )
        if self._total_weight(box_id) != m["total_weight"]:
            raise PackError("weight_drift", f"{box_id} actual weight differs")
        conflicts = catalog.check([(c["item_id"], c["category"]) for c in live_contents])
        if conflicts:
            raise PackError(
                "seal_rule_violation",
                f"{seal_id} fails its recorded rule {catalog.revision}",
            )

    def _import_shipment(self, sh: dict) -> None:
        if not isinstance(sh, dict) or not isinstance(sh.get("shipment_id"), str):
            raise PackError("invalid_shipment", "seed shipment needs shipment_id")
        omit = sh.get("omit_manifests")
        manifests = sh.get("manifests")
        if manifests is None and isinstance(omit, list):
            manifests = [self.export_seal_for_import(b) for b in omit]
        if not isinstance(manifests, list):
            raise PackError("invalid_shipment", "manifests or omit_manifests needed")
        payload = {"schema": SCHEMAS["shipment_input"],
                   "shipment_id": sh["shipment_id"], "manifests": manifests}
        # 复用同一接收逻辑, 但在当前事务内 (嵌套 _tx 会失败, 故直接调用内部路径)
        self._receive_in_tx(payload)

    def _receive_in_tx(self, payload: dict) -> dict:
        shipment_id, canonical_manifests, canonical = _canonicalize_shipment_input(payload)
        prior = self.conn.execute(
            "SELECT canonical_json,result_json FROM shipments WHERE shipment_id=?",
            (shipment_id,),
        ).fetchone()
        if prior is not None:
            if prior["canonical_json"] == canonical:
                return {"ok": True, "replayed": True,
                        **json.loads(prior["result_json"])}
            raise PackError("shipment_conflict",
                            f"{shipment_id} already committed with different content")
        for m in canonical_manifests:
            self._verify_manifest_against_store(m)
        result = {"shipment_id": shipment_id,
                  "boxes": [m["box_id"] for m in canonical_manifests],
                  "seals": {m["box_id"]: m["seal_id"] for m in canonical_manifests}}
        self.conn.execute(
            "INSERT INTO shipments(shipment_id,canonical_json,result_json) VALUES (?,?,?)",
            (shipment_id, canonical, _canonical(result)),
        )
        for m in canonical_manifests:
            self.conn.execute("UPDATE boxes SET status='SHIPPED' WHERE box_id=?", (m["box_id"],))
            self.conn.execute(
                "INSERT INTO shipment_boxes(shipment_id,box_id,seal_id) VALUES (?,?,?)",
                (shipment_id, m["box_id"], m["seal_id"]),
            )
        self._event("receive_shipment", result)
        return result

    def export_seal_for_import(self, box_id: str) -> dict:
        row = self.conn.execute(
            "SELECT s.record_json FROM boxes b JOIN sealed_boxes s "
            "ON s.seal_id=b.current_seal_id WHERE b.box_id=?",
            (box_id,),
        ).fetchone()
        if row is None:
            raise PackError("no_seal", f"{box_id} has no seal")
        return json.loads(row["record_json"])

    def diagnose_box(self, box_id: str) -> dict:
        """供入口展示: 当前状态、有效封箱、按当时规则复核的结果."""
        box = self.box(box_id)
        out: dict[str, Any] = {"box": box, "contents": self.box_contents(box_id),
                               "total_weight": self._total_weight(box_id)}
        if box["current_seal_id"]:
            row = self.conn.execute(
                "SELECT record_json FROM sealed_boxes WHERE seal_id=?",
                (box["current_seal_id"],),
            ).fetchone()
            record = json.loads(row["record_json"])
            out["seal"] = record
            cat = self.get_catalog(record["rule_revision"])
            out["seal_rule_pairs"] = cat.pairs() if cat else None
            out["seal_rule_conflicts"] = (
                [c.as_dict() for c in cat.check(
                    [(c2["item_id"], c2["category"]) for c2 in out["contents"]])]
                if cat else None
            )
        else:
            cat = self.current_catalog()
            out["current_rule_pairs"] = cat.pairs()
            out["current_rule_conflicts"] = [
                c.as_dict() for c in cat.check(
                    [(c2["item_id"], c2["category"]) for c2 in out["contents"]])
            ]
        return out

    def replay_shipment(self, shipment_id: str) -> dict:
        row = self.conn.execute(
            "SELECT result_json FROM shipments WHERE shipment_id=?", (shipment_id,)
        ).fetchone()
        if row is None:
            raise PackError("unknown_shipment", f"no shipment {shipment_id}")
        return {"ok": True, "replayed": True, **json.loads(row["result_json"])}


# ---------- 事务/规范化辅助 ----------
class _Tx:
    """显式事务: 成功提交, 任何异常回滚 (含业务拒绝)."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        return False


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _positive_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def _valid_item(it: Any) -> bool:
    return (
        isinstance(it, dict)
        and isinstance(it.get("item_id"), str)
        and bool(it["item_id"])
        and isinstance(it.get("category"), str)
        and bool(it["category"])
        and _positive_int(it.get("weight"))
    )


def record_expected_contents(record: Any) -> list[dict]:
    """读取可信记录中的 contents, 仅用于导入时的内容一致性比对."""
    if not isinstance(record, dict) or not isinstance(record.get("contents"), list):
        raise PackError("invalid_trusted_seal", "trusted seal needs contents list")
    out = []
    for c in record["contents"]:
        if not _valid_item(c):
            raise PackError("invalid_trusted_seal", f"bad trusted content {c!r}")
        out.append({"item_id": c["item_id"], "category": c["category"],
                    "weight": int(c["weight"])})
    return sorted(out, key=lambda c: c["item_id"])


def _canonicalize_shipment_input(payload: dict) -> tuple[str, list[dict], str]:
    """校验并规范化 shipment-input/v1.

    返回 (shipment_id, 按 box_id 排序的规范化 manifests, 规范 JSON 串)。
    同一申请内同一 box_id 出现两次 -> 拒绝 (即使内容相同)。
    """
    shipment_id = payload.get("shipment_id")
    manifests = payload.get("manifests")
    if not isinstance(shipment_id, str) or not shipment_id:
        raise PackError("bad_shipment_id", "shipment_id required")
    if not isinstance(manifests, list) or not manifests:
        raise PackError("empty_shipment", "manifests must be a non-empty list")
    canonical_manifests: list[dict] = []
    seen_boxes: set[str] = set()
    for m in manifests:
        c = canonical_sealed_box(m)  # 结构 + 自洽校验
        if c["box_id"] in seen_boxes:
            raise PackError("duplicate_box_in_shipment",
                            f"{c['box_id']} appears more than once in one shipment")
        seen_boxes.add(c["box_id"])
        canonical_manifests.append(c)
    canonical_manifests.sort(key=lambda a: a["box_id"])
    canonical = _canonical({"schema": SCHEMAS["shipment_input"],
                            "shipment_id": shipment_id,
                            "manifests": canonical_manifests})
    return shipment_id, canonical_manifests, canonical


def canonical_sealed_box(artifact: Any) -> dict:
    """校验 sealed-box/v1 结构并返回规范化结果.

    规范化只影响键序与 contents 顺序, 不做值修正:
    - 字段/类型必须正确, 数值为整数;
    - contents 每项 item_id 唯一, 按 item_id 排序;
    - total_weight 必须等于内容重量之和 (这是“工件自洽”, 不足以证明可信)。
    """
    if not isinstance(artifact, dict):
        raise PackError("bad_manifest", "manifest must be an object")
    if artifact.get("schema") != SCHEMAS["sealed"]:
        raise PackError("bad_manifest_schema", "expected sealed-box/v1")
    seal_id = artifact.get("seal_id")
    box_id = artifact.get("box_id")
    revision = artifact.get("rule_revision")
    capacity = artifact.get("capacity")
    total = artifact.get("total_weight")
    contents = artifact.get("contents")
    if not isinstance(seal_id, str) or not seal_id:
        raise PackError("bad_manifest", "seal_id required")
    if not isinstance(box_id, str) or not box_id:
        raise PackError("bad_manifest", "box_id required")
    if not isinstance(revision, str) or not revision:
        raise PackError("bad_manifest", "rule_revision required")
    if not _positive_int(capacity):
        raise PackError("bad_manifest", "capacity must be a positive integer")
    if not isinstance(contents, list) or not contents:
        raise PackError("bad_manifest", "contents must be a non-empty list")
    seen: set[str] = set()
    canon_contents: list[dict] = []
    for c in contents:
        if not _valid_item(c):
            raise PackError("bad_manifest", f"bad content entry: {c!r}")
        if c["item_id"] in seen:
            raise PackError("duplicate_content_item",
                            f"{c['item_id']} repeated in one manifest")
        seen.add(c["item_id"])
        canon_contents.append(
            {"item_id": c["item_id"], "category": c["category"],
             "weight": int(c["weight"])}
        )
    canon_contents.sort(key=lambda c: c["item_id"])
    computed = sum(c["weight"] for c in canon_contents)
    if not isinstance(total, int) or isinstance(total, bool):
        raise PackError("bad_manifest", "total_weight must be an integer")
    if computed != total:
        raise PackError("manifest_not_self_consistent",
                        f"total_weight {total} != sum {computed}")
    if computed > capacity:
        raise PackError("manifest_over_capacity",
                        f"total_weight {computed} > capacity {capacity}")
    return {
        "schema": SCHEMAS["sealed"],
        "seal_id": seal_id,
        "box_id": box_id,
        "rule_revision": revision,
        "capacity": int(capacity),
        "contents": canon_contents,
        "total_weight": int(total),
    }
