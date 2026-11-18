"""绑定值解码与按参数出现顺序绑定。

支持的值：None、有符号 64 位整数、str、bytes（JSON 中以 ``{"$bytes": "十六进制"}``
表示，空串解码为空 bytes）。bool 是 int 的子类，按合同明确拒绝。
编译/绑定全程不连接数据库。
"""
from __future__ import annotations

import re
from typing import Any

from .compiler import CompiledTemplate
from .errors import BindingError

_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1
_HEX = re.compile(r"[0-9A-Fa-f]*\Z")


def decode_value(value: Any) -> None | int | str | bytes:
    """把 JSON 风格的请求值解码为可交给 sqlite3 的 Python 值。"""
    # bool 必须在 int 之前拦截：True/False 不是合法绑定值。
    if isinstance(value, bool):
        raise BindingError("boolean values are not accepted as bindings")
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        if not (_INT64_MIN <= value <= _INT64_MAX):
            raise BindingError("integer binding is outside signed 64-bit range")
        return value
    if isinstance(value, dict) and set(value) == {"$bytes"}:
        encoded = value["$bytes"]
        if not isinstance(encoded, str) or len(encoded) % 2 or _HEX.fullmatch(encoded) is None:
            raise BindingError("invalid $bytes hexadecimal encoding")
        return bytes.fromhex(encoded)
    raise BindingError(f"unsupported binding value type: {type(value).__name__}")


def encode_json_value(value: None | int | str | bytes) -> Any:
    """把已解码值转回请求/回执使用的 JSON 表示（bytes 回到 $bytes）。"""
    if isinstance(value, bytes):
        return {"$bytes": value.hex()}
    return value


def bind_parameters(
    compiled: CompiledTemplate, bindings: dict[str, Any]
) -> tuple[None | int | str | bytes, ...]:
    """校验绑定字典并按参数出现顺序返回值元组。

    绑定字典必须恰含全部不同参数名：缺失或多余都拒绝；
    同名参数出现几次就按顺序复制几次同一个已解码值。
    """
    if not isinstance(bindings, dict):
        raise BindingError("bindings must be an object")
    expected = set(compiled.distinct_names)
    actual = set(bindings)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        details = []
        if missing:
            details.append(f"missing keys: {', '.join(missing)}")
        if extra:
            details.append(f"extra keys: {', '.join(extra)}")
        raise BindingError("; ".join(details))

    decoded = {name: decode_value(bindings[name]) for name in expected}
    return tuple(decoded[param.name] for param in compiled.parameters)
