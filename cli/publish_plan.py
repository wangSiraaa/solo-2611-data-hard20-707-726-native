"""发布单个模板为 sql-param-plan/v1 计划文件。

用法：
    python -m cli.publish_plan <store_dir> <template_id> <revision> <source_file ->
若 source_file 为 "-"，从标准入读取原文（可保留换行）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from sql_param.errors import PlanStoreError, TemplateCompileError
from sql_param.plan_store import PlanStore


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        print(__doc__, file=sys.stderr)
        return 2
    store_dir, template_id, revision_text, source_file = argv[1:]
    try:
        revision = int(revision_text)
    except ValueError:
        print("revision must be an integer", file=sys.stderr)
        return 2
    source = sys.stdin.read() if source_file == "-" else Path(source_file).read_text(encoding="utf-8")

    try:
        plan = PlanStore(store_dir).publish(template_id, revision, source)
    except (TemplateCompileError, PlanStoreError) as exc:
        print(f"publish rejected: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
