"""SQL 命名参数模板编译器。

扫描规则（材料合同）：
- 普通单引号字符串：'' 为转义；双引号标识符："" 为转义；两类引号内所有字符逐字保留，
  其中的冒号/问号/分号都不是 SQL 词法元素。
- 行注释 -- 到换行或文件结束；非嵌套块注释 /* ... */，二者内部逐字保留。
- 正常 SQL 区域中，完整的 ``:name``（name 为 ASCII 字母开头的字母数字下划线）是命名参数，
  替换为单个 ``?``；位置按 Python Unicode 字符索引（代码点）记录，含冒号。
- 正常区域出现 ``?``、``;``（含末尾）一律拒绝；``:`` 后不匹配 name 也明确拒绝，
  不做静默删除。未结束的引号或块注释拒绝。
- 仅接受一条 SELECT / INSERT / UPDATE 模板；不支持多语句、触发器及其它参数风格。

不做任何 Unicode 归一化，不连接数据库。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import TemplateCompileError

PLAN_FORMAT = "sql-param-plan/v1"
_NAME_HEAD = re.compile(r"[A-Za-z]")
_NAME_CHAR = re.compile(r"[A-Za-z0-9_]")
# 正常区域首个词决定语句种类；空白与注释已在扫描时排除，这里只做词面校验。
_LEADING_KEYWORD = re.compile(r"(SELECT|INSERT|UPDATE)\b", re.IGNORECASE)
_ORDER_BY = re.compile(r"\bORDER\s+BY\b", re.IGNORECASE)


@dataclass(frozen=True)
class Parameter:
    """一次 :name 出现；重复名字会产生多个 Parameter。"""

    name: str
    start: int  # 含冒号的 Unicode 字符起始索引
    end: int    # 专属结束索引 [start, end)


@dataclass(frozen=True)
class CompiledTemplate:
    template_id: str
    revision: int
    source: str
    sql: str
    parameters: tuple[Parameter, ...]
    statement: str          # "select" / "insert" / "update"
    has_order_by: bool

    @property
    def distinct_names(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for param in self.parameters:
            seen.setdefault(param.name, None)
        return tuple(seen)


def _is_name_head(ch: str) -> bool:
    return bool(_NAME_HEAD.fullmatch(ch))


def _is_name_char(ch: str) -> bool:
    return bool(_NAME_CHAR.fullmatch(ch))


def compile_template(source: str, template_id: str, revision: int) -> CompiledTemplate:
    """把原文模板编译为可执行 SQL（? 占位）加按出现顺序的参数表。

    位置全部基于 ``source`` 的 Python 字符索引（Unicode 代码点），
    与 UTF-8 字节数无关；原文不做归一化。
    """
    if not isinstance(source, str):
        raise TemplateCompileError("template source must be a string")
    if not isinstance(template_id, str) or not template_id:
        raise TemplateCompileError("template_id must be a non-empty string")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise TemplateCompileError("revision must be a positive integer")

    out: list[str] = []                 # 可执行 SQL 片段（引号/注释原文逐字进入）
    normal_parts: list[str] = []        # 仅正常 SQL 区域的文本，参数记为 ?
    parameters: list[Parameter] = []

    NORMAL, SQUOTE, DQUOTE, LINE_COMMENT, BLOCK_COMMENT = range(5)
    state = NORMAL
    i = 0
    n = len(source)

    while i < n:
        ch = source[i]

        if state == SQUOTE:
            out.append(ch)
            normal_parts.append(" ")  # 占位，保持正常区域索引不参与关键词判断即可
            if ch == "'":
                if i + 1 < n and source[i + 1] == "'":
                    out.append(source[i + 1])
                    normal_parts.append(" ")
                    i += 2
                    continue
                state = NORMAL
            i += 1
            continue

        if state == DQUOTE:
            out.append(ch)
            normal_parts.append(" ")
            if ch == '"':
                if i + 1 < n and source[i + 1] == '"':
                    out.append(source[i + 1])
                    normal_parts.append(" ")
                    i += 2
                    continue
                state = NORMAL
            i += 1
            continue

        if state == LINE_COMMENT:
            # 行注释可在文件结尾终止；只有 \n 结束它，\r 逐字保留。
            out.append(ch)
            normal_parts.append(" ")
            if ch == "\n":
                state = NORMAL
            i += 1
            continue

        if state == BLOCK_COMMENT:
            out.append(ch)
            normal_parts.append(" ")
            if ch == "*" and i + 1 < n and source[i + 1] == "/":
                out.append("/")
                normal_parts.append(" ")
                i += 2
                state = NORMAL
                continue
            i += 1
            continue

        # state == NORMAL
        if ch == "'":
            out.append(ch)
            normal_parts.append(" ")
            state = SQUOTE
            i += 1
            continue
        if ch == '"':
            out.append(ch)
            normal_parts.append(" ")
            state = DQUOTE
            i += 1
            continue
        if ch == "-" and i + 1 < n and source[i + 1] == "-":
            out.append("--")
            normal_parts.append("  ")
            state = LINE_COMMENT
            i += 2
            continue
        if ch == "/" and i + 1 < n and source[i + 1] == "*":
            out.append("/*")
            normal_parts.append("  ")
            state = BLOCK_COMMENT
            i += 2
            continue
        if ch == ";":
            raise TemplateCompileError(
                f"semicolon is not allowed in normal SQL at character {i}"
            )
        if ch == "?":
            raise TemplateCompileError(
                f"question-mark parameter style is not supported at character {i}"
            )
        if ch == ":":
            if i + 1 < n and _is_name_head(source[i + 1]):
                j = i + 2
                while j < n and _is_name_char(source[j]):
                    j += 1
                name = source[i + 1:j]
                parameters.append(Parameter(name=name, start=i, end=j))
                out.append("?")
                normal_parts.append("?")
                i = j
                continue
            raise TemplateCompileError(
                f"colon at character {i} is not followed by a valid parameter name"
            )

        out.append(ch)
        normal_parts.append(ch)
        i += 1

    if state == SQUOTE:
        raise TemplateCompileError("unterminated single-quoted string")
    if state == DQUOTE:
        raise TemplateCompileError("unterminated double-quoted identifier")
    if state == BLOCK_COMMENT:
        raise TemplateCompileError("unterminated block comment")
    # LINE_COMMENT 在 EOF 终止是合法的。

    sql = "".join(out)
    normal_text = "".join(normal_parts).strip()
    if not normal_text:
        raise TemplateCompileError("empty SQL template")
    match = _LEADING_KEYWORD.match(normal_text)
    if match is None:
        raise TemplateCompileError(
            "only a single SELECT, INSERT or UPDATE statement is supported"
        )
    statement = match.group(1).lower()
    has_order_by = statement == "select" and bool(_ORDER_BY.search(normal_text))

    return CompiledTemplate(
        template_id=template_id,
        revision=revision,
        source=source,
        sql=sql,
        parameters=tuple(parameters),
        statement=statement,
        has_order_by=has_order_by,
    )


def make_plan(compiled: CompiledTemplate) -> dict:
    """把编译产物序列化为公开的 sql-param-plan/v1 计划字典。"""
    return {
        "format": PLAN_FORMAT,
        "template_id": compiled.template_id,
        "revision": compiled.revision,
        "source": compiled.source,
        "sql": compiled.sql,
        "parameters": [
            {"name": p.name, "start": p.start, "end": p.end}
            for p in compiled.parameters
        ],
    }
