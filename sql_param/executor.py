"""SQLite 批量执行库。

执行合同：
1. 每个条目必须固定 template_id + revision；执行侧只从 PlanStore 保存的计划文件读取
   sql 与参数顺序，绝不重新扫描 source，也不借用内存里的编译器对象。
2. 开事务前对整批做预检：计划存在性、绑定键集合（缺/多即拒）、值类型、语句种类。
   任一不过则抛 BatchError，不打开事务、不写任何数据。
3. 预检通过后在一个真实事务里依次执行；某项 SQLite 约束失败立即 ROLLBACK，
   前面的写入全部撤销，回执明确标记 rolled_back 并落盘，绝不标成功。
4. SELECT 项保存列名与结果行；带 ORDER BY 时按游标顺序保留行序，未带 ORDER BY
   不承诺跨次行序。INSERT/UPDATE 项保存 rowcount。
5. 回执逐字内嵌当时的计划快照（含 source 原文与 sql），批次 ID 不可重用；
   新修订只影响后续批次，历史批次重开仍可回读。

连接复用边界：每个 execute_batch 使用独立 sqlite3 连接，提交/回滚后关闭；
批次之间不共享连接、不共享事务。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path

from .bindings import decode_value, encode_json_value
from .errors import BatchError, BindingError, PlanStoreError
from .plan_store import PlanStore

MAX_ITEMS_PER_BATCH = 100
_LEADING_KEYWORD = re.compile(r"\s*(SELECT|INSERT|UPDATE)\b", re.IGNORECASE)
_ORDER_BY = re.compile(r"\bORDER\s+BY\b", re.IGNORECASE)
_COMMITTED = "committed"
_ROLLED_BACK = "rolled_back"


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


class BatchExecutor:
    def __init__(
        self,
        store: PlanStore,
        db_path: str | os.PathLike,
        receipts_dir: str | os.PathLike,
        max_items: int = MAX_ITEMS_PER_BATCH,
    ):
        self.store = store
        self.db_path = Path(db_path)
        self.receipts_dir = Path(receipts_dir)
        self.receipts_dir.mkdir(parents=True, exist_ok=True)
        self.max_items = max_items

    # ---- 公开入口 ---------------------------------------------------------
    def execute_batch(self, batch_id: str, items: list[dict]) -> dict:
        if not isinstance(batch_id, str) or not batch_id:
            raise BatchError("batch_id must be a non-empty string")
        if not isinstance(items, list) or not items:
            raise BatchError("batch must contain at least one item")
        if len(items) > self.max_items:
            raise BatchError(f"batch item limit exceeded ({self.max_items})")
        receipt_path = self.receipts_dir / f"{batch_id}.json"
        if receipt_path.exists():
            raise BatchError(f"batch {batch_id!r} already exists; batch receipts are immutable")

        prepared = self._preflight(items)

        receipt: dict = {
            "batch_id": batch_id,
            "status": _COMMITTED,
            "items": [],
        }
        conn = sqlite3.connect(self.db_path)
        conn.isolation_level = None  # 自己显式管理事务
        try:
            conn.execute("BEGIN")
            for index, prepared_item in enumerate(prepared):
                item_receipt = self._run_one(conn, index, prepared_item)
                receipt["items"].append(item_receipt)
                if item_receipt["status"] == "failed":
                    conn.execute("ROLLBACK")
                    receipt["status"] = _ROLLED_BACK
                    receipt["error"] = item_receipt["error"]
                    self._persist(receipt_path, receipt)
                    return receipt
            conn.execute("COMMIT")
        except sqlite3.Error as exc:  # 事务/连接层面的意外错误也必须回滚
            conn.execute("ROLLBACK")
            receipt["status"] = _ROLLED_BACK
            receipt["error"] = f"unexpected sqlite error: {exc}"
            self._persist(receipt_path, receipt)
            return receipt
        finally:
            conn.close()

        self._persist(receipt_path, receipt)
        return receipt

    def read_batch(self, batch_id: str) -> dict:
        path = self.receipts_dir / f"{batch_id}.json"
        if not path.exists():
            raise BatchError(f"batch receipt not found: {batch_id!r}")
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    # ---- 预检：不开事务、不写库 -------------------------------------------
    def _preflight(self, items: list[dict]) -> list[dict]:
        prepared: list[dict] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise BatchError(f"item {index}: must be an object")
            try:
                template_id = item["template_id"]
                revision = item["revision"]
                raw_bindings = item["bindings"]
            except KeyError as exc:
                raise BatchError(f"item {index}: missing field {exc.args[0]!r}") from exc
            if not isinstance(raw_bindings, dict):
                raise BatchError(f"item {index}: bindings must be an object")

            # 固定修订：计划只能来自磁盘上的公开计划文件。
            try:
                plan = self.store.load(template_id, revision)
            except PlanStoreError as exc:
                raise BatchError(f"item {index}: {exc}") from exc
            expected_names = self.store.distinct_names(plan)
            if set(raw_bindings) != set(expected_names):
                missing = sorted(set(expected_names) - set(raw_bindings))
                extra = sorted(set(raw_bindings) - set(expected_names))
                details = []
                if missing:
                    details.append(f"missing keys: {', '.join(missing)}")
                if extra:
                    details.append(f"extra keys: {', '.join(extra)}")
                raise BatchError(f"item {index} ({template_id} r{revision}): {'; '.join(details)}")

            try:
                decoded = {name: decode_value(raw_bindings[name]) for name in raw_bindings}
            except BindingError as exc:
                raise BatchError(
                    f"item {index} ({template_id} r{revision}): {exc}"
                ) from exc
            # 按计划参数出现顺序展开（含重复名字，同名复制同值），与 plan_reader 语义一致。
            param_names = [p["name"] for p in plan["parameters"]]
            ordered_values = tuple(decoded[name] for name in param_names)

            kind_match = _LEADING_KEYWORD.match(plan["sql"])
            if kind_match is None:
                raise BatchError(
                    f"item {index}: saved plan sql is not SELECT/INSERT/UPDATE"
                )
            statement = kind_match.group(1).lower()
            has_order_by = statement == "select" and bool(_ORDER_BY.search(plan["sql"]))

            prepared.append(
                {
                    "index": index,
                    "plan": plan,
                    "distinct_names": expected_names,
                    "ordered_values": ordered_values,
                    "statement": statement,
                    "has_order_by": has_order_by,
                }
            )
        return prepared

    # ---- 单条执行 ---------------------------------------------------------
    @staticmethod
    def _run_one(conn: sqlite3.Connection, index: int, prepared: dict) -> dict:
        plan = prepared["plan"]
        item_receipt: dict = {
            "index": index,
            "template_id": plan["template_id"],
            "revision": plan["revision"],
            "status": "ok",
            "plan": plan,  # 内嵌当时计划快照（含 source 原文）
            "distinct_names": list(prepared["distinct_names"]),
            "ordered_values": [encode_json_value(v) for v in prepared["ordered_values"]],
        }
        try:
            cursor = conn.execute(plan["sql"], prepared["ordered_values"])
        except sqlite3.Error as exc:
            item_receipt["status"] = "failed"
            item_receipt["error"] = f"{type(exc).__name__}: {exc}"
            return item_receipt

        if prepared["statement"] == "select":
            columns = [desc[0] for desc in cursor.description]
            rows = [[encode_json_value(cell) for cell in row] for row in cursor.fetchall()]
            item_receipt["result"] = {
                "kind": "select",
                "order_by": prepared["has_order_by"],
                "columns": columns,
                "rows": rows,
            }
        else:
            item_receipt["result"] = {
                "kind": prepared["statement"],
                "rowcount": cursor.rowcount,
            }
        return item_receipt

    def _persist(self, path: Path, receipt: dict) -> None:
        _atomic_write_json(path, receipt)
