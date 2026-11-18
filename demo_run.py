"""端到端驱动：实际读取 materials/717 的材料，发布计划并执行各批次。

流程：
  1. 初始化 SQLite（schema.sql）与工作目录。
  2. 发布 templates.json 的三个模板，生成 sql-param-plan/v1 计划文件。
  3. 用材料自带的 plan_reader.py（子进程）消费我们写出的计划，证明格式互操作。
  4. 按 requests.json 执行正常批 b-ok 与末项约束失败批 b-rollback，并核对数据库。
  5. 重开 PlanStore，发布 operations.json 的 put r2 修订，执行 b-new，跑多余键拒绝例，
     再回读旧批次 b-ok 的历史回执与旧计划。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MATERIALS = ROOT / "materials" / "717"
WORK = ROOT / "workdir"

from sql_param.errors import BatchError, PlanStoreError  # noqa: E402
from sql_param.executor import BatchExecutor  # noqa: E402
from sql_param.plan_store import PlanStore  # noqa: E402


def load_json(name: str):
    return json.loads((MATERIALS / name).read_text(encoding="utf-8"))


def section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show(label: str, payload) -> None:
    print(f"\n--- {label} ---")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def run_reader(plan_path: Path, request_path: Path) -> dict:
    """调用材料提供的 plan_reader.py，验证公开计划可被其直接消费。"""
    proc = subprocess.run(
        [sys.executable, str(MATERIALS / "plan_reader.py"), str(plan_path), str(request_path)],
        check=True, capture_output=True, text=True,
    )
    return json.loads(proc.stdout)


def main() -> int:
    if WORK.exists():
        shutil.rmtree(WORK)
    store_dir = WORK / "store"
    receipts_dir = WORK / "receipts"
    db_path = WORK / "notes.db"
    requests_dir = WORK / "reader-requests"
    requests_dir.mkdir(parents=True)

    # 1. 初始化数据库（多语句脚本只在初始化时 executescript，模板本身不允许分号）。
    with sqlite3.connect(db_path) as conn:
        conn.executescript((MATERIALS / "schema.sql").read_text(encoding="utf-8"))

    store = PlanStore(store_dir)
    executor = BatchExecutor(store, db_path, receipts_dir)

    # 2. 发布三个模板。
    section("1. 发布 templates.json 的计划（sql-param-plan/v1）")
    templates = load_json("templates.json")["templates"]
    plans = {}
    for tpl in templates:
        plan = store.publish(tpl["template_id"], tpl["revision"], tpl["source"])
        plans[(tpl["template_id"], tpl["revision"])] = plan
        show(f"{tpl['template_id']} r{tpl['revision']} 计划", plan)

    # 3. 与材料 plan_reader.py 互操作：为 get/quoted 写请求文件再交给读取入口。
    section("2. 材料 plan_reader.py 消费本服务输出的计划（不解析 SQL）")
    interop_cases = [
        ("get", 1, {"template_id": "get", "revision": 1, "bindings": {"id": 1}}),
        ("quoted", 1, {"template_id": "quoted", "revision": 1, "bindings": {"id": 42}}),
    ]
    for template_id, revision, request in interop_cases:
        req_path = requests_dir / f"{template_id}-r{revision}.json"
        req_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        plan_path = store._plan_path(template_id, revision)
        reader_output = run_reader(plan_path, req_path)
        show(f"plan_reader 读取 {template_id} r{revision}", reader_output)

    # 材料手写的外部计划样例也必须能被同一读取合同消费。
    example_output = run_reader(
        MATERIALS / "reader-example-plan.json",
        MATERIALS / "reader-example-request.json",
    )
    show("plan_reader 读取材料手写样例计划", example_output)

    # 4. requests.json 的两个批次。
    section("3. requests.json：正常批 b-ok 与末项约束失败批 b-rollback")
    batches = load_json("requests.json")["batches"]
    ok_receipt = executor.execute_batch(batches[0]["batch_id"], batches[0]["items"])
    show("b-ok 回执", {"status": ok_receipt["status"],
                       "items": [{"template_id": i["template_id"],
                                  "revision": i["revision"],
                                  "status": i["status"],
                                  "result": i["result"]} for i in ok_receipt["items"]]})

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT id, message, payload FROM notes ORDER BY id").fetchall()
    show("b-ok 后数据库内容", [
        {"id": r[0], "message": r[1], "payload": r[2].hex() if isinstance(r[2], bytes) else r[2]}
        for r in rows
    ])
    assert rows == [(1, "hello:literal", b"\x00\xff")], "正常批写入不符合预期"

    rollback_receipt = executor.execute_batch(batches[1]["batch_id"], batches[1]["items"])
    show("b-rollback 回执", {"status": rollback_receipt["status"],
                            "error": rollback_receipt.get("error"),
                            "items": [{"status": i["status"], "error": i.get("error")}
                                      for i in rollback_receipt["items"]]})
    with sqlite3.connect(db_path) as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM notes ORDER BY id").fetchall()]
    show("回滚后数据库仅剩的 id", ids)
    assert ids == [1], "回滚失败：id=2 不应落库"
    assert rollback_receipt["status"] == "rolled_back"

    # 5. 重开存储：修订、新批、拒绝例与历史回读。
    section("4. operations.json：重开存储后的修订/新批/拒绝/历史回读")
    reopened_store = PlanStore(store_dir)          # 模拟进程重开
    reopened_executor = BatchExecutor(reopened_store, db_path, receipts_dir)
    operations = load_json("operations.json")["operations"]

    publish_op = operations[0]
    new_plan = reopened_store.publish(
        publish_op["template_id"], publish_op["revision"], publish_op["source"]
    )
    show("发布 put r2 新修订计划", new_plan)
    # 旧计划仍在且未被覆盖。
    old_put = reopened_store.load("put", 1)
    assert ":literal" in old_put["source"]

    execute_op = operations[1]
    new_receipt = reopened_executor.execute_batch(execute_op["batch_id"], execute_op["items"])
    show("b-new 回执", {"status": new_receipt["status"],
                        "items": [{"template_id": i["template_id"], "revision": i["revision"],
                                   "status": i["status"], "result": i["result"]}
                                  for i in new_receipt["items"]]})
    with sqlite3.connect(db_path) as conn:
        revised = conn.execute("SELECT message FROM notes WHERE id=3").fetchone()[0]
    show("id=3 实际落库 message", revised)
    assert revised == "new:revised"

    reject_op = operations[2]
    try:
        # 直接走执行器预检：多余键必须在事务前拒绝。
        reopened_executor.execute_batch(
            "should-not-exist",
            [{"template_id": reject_op["template_id"],
              "revision": reject_op["revision"],
              "bindings": reject_op["bindings"]}],
        )
        raise AssertionError("多余键未被拒绝")
    except BatchError as exc:
        show("多余键拒绝例（预期失败）", str(exc))
    assert not (receipts_dir / "should-not-exist.json").exists()

    read_op = operations[3]
    historical = reopened_executor.read_batch(read_op["batch_id"])
    show("回读历史批次 b-ok（含当时计划快照）", {
        "batch_id": historical["batch_id"],
        "status": historical["status"],
        "first_item_revision": historical["items"][0]["revision"],
        "first_item_source": historical["items"][0]["plan"]["source"],
        "first_item_sql": historical["items"][0]["plan"]["sql"],
    })
    assert historical["items"][0]["plan"]["source"] == plans[("put", 1)]["source"]

    # 异文重放必须拒绝。
    section("5. 附加约束校验")
    try:
        reopened_store.publish("put", 1, "INSERT INTO notes VALUES(:id)")
        raise AssertionError("异文重放未被拒绝")
    except PlanStoreError as exc:
        show("同 id+revision 异文重放拒绝（预期失败）", str(exc))

    with sqlite3.connect(db_path) as conn:
        final_rows = conn.execute("SELECT id, message FROM notes ORDER BY id").fetchall()
    show("最终数据库状态", final_rows)
    assert final_rows == [(1, "hello:literal"), (3, "new:revised")]

    print("\n全部端到端断言通过。")
    print(f"计划目录: {store_dir}")
    print(f"回执目录: {receipts_dir}")
    print(f"数据库:   {db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
