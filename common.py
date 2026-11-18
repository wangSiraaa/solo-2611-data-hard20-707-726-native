"""Shared validation, hashing and JSON helpers.

All data handled by this project is fictional questionnaire data.  Nothing here
performs diagnosis, norming, reliability estimation or statistical imputation.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from fractions import Fraction
from typing import Any, Iterator


class ContractError(ValueError):
    """A deterministic, machine-readable contract violation."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        out = {"code": self.code, "message": self.message}
        if self.details:
            out["details"] = self.details
        return out


def is_int(value: Any) -> bool:
    """True only for real ints.  bool is an int subclass in Python, so exclude it."""
    return type(value) is int


def is_bool(value: Any) -> bool:
    return type(value) is bool


def validate_id(value: Any, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ContractError("invalid_id", f"{name} must be a non-empty string",
                            field=name, value=repr(value))
    if value != value.strip():
        raise ContractError("invalid_id", f"{name} must not have surrounding whitespace",
                            field=name, value=repr(value))
    return value


def require_int(value: Any, name: str, *, minimum: int | None = None,
                maximum: int | None = None, code: str = "invalid_integer") -> int:
    if not is_int(value):
        raise ContractError(code, f"{name} must be an integer "
                                   f"(booleans and floats are rejected)",
                            field=name, value=repr(value))
    if minimum is not None and value < minimum:
        raise ContractError(code, f"{name} must be >= {minimum}",
                            field=name, value=value, minimum=minimum)
    if maximum is not None and value > maximum:
        raise ContractError(code, f"{name} must be <= {maximum}",
                            field=name, value=value, maximum=maximum)
    return value


def require_bool(value: Any, name: str) -> bool:
    if not is_bool(value):
        raise ContractError("invalid_boolean", f"{name} must be a JSON boolean",
                            field=name, value=repr(value))
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_hash(value: Any) -> str:
    return sha256_text(canonical_json(value))


def fraction_string(numerator: int, denominator: int) -> str:
    """Reduced exact rational as ``a/b``.  Zero denominator is undefined."""
    if denominator == 0:
        return "undefined"
    frac = Fraction(numerator, denominator)
    return f"{frac.numerator}/{frac.denominator}"


def load_json_file(path: str) -> Any:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def atomic_write_json(path: str, payload: Any) -> None:
    """Write JSON through a same-directory temp file then os.replace.

    Readers never observe a half-written product file.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".scoring-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@contextmanager
def transaction(connection: Any) -> Iterator[None]:
    """Re-entrant write transaction.

    Uses ``BEGIN IMMEDIATE`` only at the outermost level so nested helpers (for
    example a catalog import calling ``publish_scale``) share one atomic unit.
    Any exception rolls the whole outer unit back.
    """
    if connection.in_transaction:
        yield
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")


def parse_json_argument(text: str, name: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ContractError("invalid_json", f"{name} is not valid JSON: {exc}") from exc
