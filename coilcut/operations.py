"""Run the operations JSONL protocol against a ledger.

Supported operations mirror ``materials/731/operations.jsonl`` plus a few
failure-injection helpers used by the demo/tests:

* ``cut``      -- cut a piece at a local coordinate
* ``flip``     -- reverse a piece's local direction
* ``deliver``  -- deliver an active piece, producing a ``piece-delivery/v1``
* ``replay``   -- read back a previously successful cut
* ``replay_delivery`` -- read back a previously successful delivery
* ``report``   -- inspect root physical accounting / lineage
* ``expect_error`` -- assert that the next operation fails with a given code
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import CoilCutError, InvalidOperationError
from .storage import Ledger


def load_stock(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


def initialize_from_stock(ledger: Ledger, stock: dict[str, Any]) -> None:
    ledger.initialize(
        roots=stock["roots"],
        active_pieces=stock["active_pieces"],
        kerf=stock["tool"]["kerf"],
        tool_id=stock["tool"].get("tool_id", "SAW"),
    )


def apply_operation(ledger: Ledger, op: dict[str, Any]) -> dict[str, Any]:
    kind = op.get("op")
    if kind == "cut":
        return ledger.cut(
            cut_id=op["cut_id"],
            piece_id=op["piece_id"],
            expected_revision=op.get("expected_revision"),
            expected_orientation=op.get("expected_orientation"),
            u=op["u"],
            kerf=op.get("kerf"),
            left_child=op["left_child"],
            right_child=op["right_child"],
        )
    if kind == "flip":
        return ledger.flip(op["piece_id"], expected_revision=op.get("expected_revision"))
    if kind == "deliver":
        return ledger.deliver(
            delivery_id=op["delivery_id"],
            piece_id=op["piece_id"],
            required_length=op["required_length"],
        )
    if kind == "replay":
        return ledger.replay_cut(op["cut_id"])
    if kind == "replay_delivery":
        return ledger.replay_delivery(op["delivery_id"])
    if kind == "preview":
        return ledger.preview_cut(
            piece_id=op["piece_id"],
            u=op["u"],
            kerf=op.get("kerf"),
            left_child=op["left_child"],
            right_child=op["right_child"],
        )
    if kind == "commit_preview":
        return ledger.commit_preview(op["cut_id"], op["preview"])
    if kind == "report":
        if "piece_id" in op:
            return ledger.lineage(op["piece_id"])
        return ledger.root_report(op["root_id"])
    raise InvalidOperationError("unsupported operation", op=kind)


def run_operations(
    ledger: Ledger,
    operations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Apply operations in order, recording each outcome.

    An ``expect_error`` wrapper turns an expected failure into a recorded,
    non-fatal result.  Any other failure propagates with its original type.
    """

    results: list[dict[str, Any]] = []
    for index, op in enumerate(operations):
        if op.get("op") == "expect_error":
            target = op["operation"]
            try:
                apply_operation(ledger, target)
            except CoilCutError as exc:
                if exc.code != op["code"]:
                    raise InvalidOperationError(
                        "operation failed with an unexpected code",
                        index=index,
                        expected=op["code"],
                        actual=exc.code,
                    ) from exc
                results.append(
                    {
                        "index": index,
                        "op": "expect_error",
                        "code": exc.code,
                        "message": exc.message,
                        "details": exc.details,
                    }
                )
            else:
                raise InvalidOperationError(
                    "operation unexpectedly succeeded",
                    index=index,
                    expected=op["code"],
                )
            continue
        results.append({"index": index, "op": op.get("op"), "result": apply_operation(ledger, op)})
    return results


def run_operations_file(
    ledger: Ledger, path: str | Path
) -> list[dict[str, Any]]:
    ops = [
        json.loads(line)
        for line in Path(path).read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return run_operations(ledger, ops)
