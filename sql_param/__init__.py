"""独立 SQL 命名参数模板编译与 SQLite 批量执行库。

公开入口：
- compile_template:        单条模板的字符级扫描编译器
- make_plan:               由编译产物构造 sql-param-plan/v1 计划字典
- decode_value / bind_parameters: 绑定值解码与按出现顺序绑定
- PlanStore:               计划发布、重放校验与持久化加载
- BatchExecutor:           固定修订、预检绑定、单事务批量执行与回执
"""
from .compiler import CompiledTemplate, Parameter, TemplateCompileError, compile_template, make_plan
from .bindings import BindingError, bind_parameters, decode_value, encode_json_value
from .errors import BatchError, PlanStoreError
from .plan_store import PLAN_FORMAT, PlanStore, validate_plan
from .executor import BatchExecutor

__all__ = [
    "CompiledTemplate",
    "Parameter",
    "TemplateCompileError",
    "compile_template",
    "make_plan",
    "BindingError",
    "bind_parameters",
    "decode_value",
    "encode_json_value",
    "BatchError",
    "PlanStoreError",
    "PLAN_FORMAT",
    "PlanStore",
    "validate_plan",
    "BatchExecutor",
]
