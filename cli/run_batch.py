"""从批次 JSON 执行一个批量，打印回执状态与回执文件路径。

批次文件格式：{"batch_id": "...", "items": [ {template_id, revision, bindings}, ... ]}
用法：
    python -m cli.run_batch <store_dir> <db_path> <receipts_dir> <batch_file>
约束失败等执行期回滚以退出码 3 返回（回执已落盘）；预检失败退出码 1。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from sql_param.errors import BatchError
from sql_param.executor import BatchExecutor
from sql_param.plan_store import PlanStore


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        print(__doc__, file=sys.stderr)
        return 2
    store_dir, db_path, receipts_dir, batch_file = argv[1:]
    batch = json.loads(Path(batch_file).read_text(encoding="utf-8"))

    executor = BatchExecutor(PlanStore(store_dir), db_path, receipts_dir)
    try:
        receipt = executor.execute_batch(batch["batch_id"], batch["items"])
    except BatchError as exc:
        print(f"preflight rejected: {exc}", file=sys.stderr)
        return 1

    receipt_path = Path(receipts_dir) / f"{batch['batch_id']}.json"
    summary = {
        "batch_id": receipt["batch_id"],
        "status": receipt["status"],
        "receipt": str(receipt_path),
        "items": [
            {"index": item["index"], "template_id": item["template_id"],
             "revision": item["revision"], "status": item["status"],
             "error": item.get("error")}
            for item in receipt["items"]
        ],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if receipt["status"] == "committed" else 3


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
