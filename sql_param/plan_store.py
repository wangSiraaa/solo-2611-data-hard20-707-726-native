"""sql-param-plan/v1 计划的发布、重放校验与持久化加载。

存储布局（全部为普通 UTF-8 JSON 文件，可独立保存/拷走）::

    store_dir/plans/<template_id>/r<revision>.json

兼容与可见性边界：
- 计划一经发布即不可变：同 id+revision+source 重放返回原计划；同 id+revision 异文拒绝；
  新修订写入新文件，永不覆盖旧计划。
- 写入采用临时文件 + os.replace 原子替换，进程崩溃不会留下半截计划。
- 每次 load 都重新从磁盘读取并按公开合同校验，不缓存编译对象；因此另一个进程发布的
  新修订对本进程后续 load 立即可见（文件系统可见性边界内）。
- 计划文件即公开的 sql-param-plan/v1 格式，材料自带的 plan_reader.py 可直接消费。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from .compiler import PLAN_FORMAT, compile_template, make_plan
from .errors import PlanStoreError

_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")
_TEMPLATE_ID = re.compile(r"[A-Za-z0-9_-]+\Z")
MAX_TEMPLATES = 100


def _iter_distinct_names(parameters: list[dict]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for item in parameters:
        seen.setdefault(item["name"], None)
    return tuple(seen)


class PlanStore:
    def __init__(self, store_dir: str | os.PathLike):
        self.store_dir = Path(store_dir)
        self.plans_dir = self.store_dir / "plans"
        self.plans_dir.mkdir(parents=True, exist_ok=True)

    # ---- 路径与限额 -------------------------------------------------------
    def _plan_path(self, template_id: str, revision: int) -> Path:
        if not isinstance(template_id, str) or _TEMPLATE_ID.fullmatch(template_id) is None:
            raise PlanStoreError(f"unsafe or invalid template_id: {template_id!r}")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise PlanStoreError("revision must be a positive integer")
        return self.plans_dir / template_id / f"r{revision}.json"

    def _template_dirs(self) -> list[Path]:
        return [p for p in self.plans_dir.iterdir() if p.is_dir()] if self.plans_dir.exists() else []

    # ---- 发布 -------------------------------------------------------------
    def publish(self, template_id: str, revision: int, source: str) -> dict:
        """编译并发布计划；返回公开计划字典。重放同文幂等，异文拒绝。"""
        compiled = compile_template(source, template_id, revision)
        plan = make_plan(compiled)
        path = self._plan_path(template_id, revision)

        if path.exists():
            with path.open("r", encoding="utf-8") as fh:
                existing = json.load(fh)
            validate_plan(existing)
            if existing["source"] != source:
                raise PlanStoreError(
                    f"plan {template_id} r{revision} already exists with a different source; "
                    "publish a new revision instead of overwriting"
                )
            return existing

        template_dir = path.parent
        if not template_dir.exists() and len(self._template_dirs()) >= MAX_TEMPLATES:
            raise PlanStoreError(f"template limit reached ({MAX_TEMPLATES})")
        template_dir.mkdir(parents=True, exist_ok=True)
        self._atomic_write_json(path, plan)
        return plan

    @staticmethod
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

    # ---- 读取 -------------------------------------------------------------
    def load(self, template_id: str, revision: int) -> dict:
        """从磁盘读取已保存计划并按公开合同校验；不解析 SQL、不重新编译。"""
        path = self._plan_path(template_id, revision)
        if not path.exists():
            raise PlanStoreError(f"plan not found: {template_id} r{revision}")
        with path.open("r", encoding="utf-8") as fh:
            plan = json.load(fh)
        validate_plan(plan)
        return plan

    def load_requested(self, request: dict) -> dict:
        """按绑定请求里固定的 template_id/revision 读取计划并核对一致性。"""
        try:
            template_id = request["template_id"]
            revision = request["revision"]
        except (KeyError, TypeError) as exc:
            raise PlanStoreError("request must pin template_id and revision") from exc
        plan = self.load(template_id, revision)
        if (plan["template_id"], plan["revision"]) != (template_id, revision):
            raise PlanStoreError("request refers to another plan")
        return plan

    def distinct_names(self, plan: dict) -> tuple[str, ...]:
        return _iter_distinct_names(plan["parameters"])


def validate_plan(plan: object) -> dict:
    """按 sql-param-plan/v1 公开合同校验计划，规则与材料 plan_reader.py 对齐。"""
    if not isinstance(plan, dict):
        raise PlanStoreError("plan must be a JSON object")
    if plan.get("format") != PLAN_FORMAT:
        raise PlanStoreError("unsupported plan format")
    if not isinstance(plan.get("template_id"), str) or not plan["template_id"]:
        raise PlanStoreError("invalid template_id")
    if type(plan.get("revision")) is not int or plan["revision"] < 1:
        raise PlanStoreError("invalid plan revision")
    if type(plan.get("source")) is not str or type(plan.get("sql")) is not str:
        raise PlanStoreError("invalid template text")
    parameters = plan.get("parameters")
    if not isinstance(parameters, list):
        raise PlanStoreError("parameters must be a list")

    source = plan["source"]
    last_end = 0
    for item in parameters:
        if not isinstance(item, dict):
            raise PlanStoreError("each parameter must be an object")
        name, start, end = item.get("name"), item.get("start"), item.get("end")
        if not isinstance(name, str) or _NAME.fullmatch(name) is None:
            raise PlanStoreError("invalid parameter name")
        if type(start) is not int or type(end) is not int:
            raise PlanStoreError("invalid source character position")
        if not (last_end <= start < end <= len(source)):
            raise PlanStoreError("invalid source character position")
        if source[start:end] != ":" + name:
            raise PlanStoreError("position does not name source token")
        last_end = end
    return plan
