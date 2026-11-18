"""Failure-injection walkthrough for the 731 scenario.

Each block runs against a *fresh* ledger so a rejected operation cannot leave
partial state behind.  The output is intentionally verbose: it is the
"operations after flip, real D1, and every required failure mode" demo.

Run:  python3 -m coilcut.demo
"""

from __future__ import annotations

import json
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

from .errors import CoilCutError
from .operations import initialize_from_stock, load_stock
from .storage import Ledger

STOCK_PATH = Path(__file__).resolve().parents[1] / "materials" / "731" / "stock.json"


def _print(title: str, payload: Any) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _fresh() -> tuple[Ledger, tempfile.TemporaryDirectory[str]]:
    tmp = tempfile.TemporaryDirectory()
    ledger = Ledger(str(Path(tmp.name) / "demo.db"))
    initialize_from_stock(ledger, load_stock(STOCK_PATH))
    return ledger, tmp


def _expect(label: str, code: str, fn: Callable[[], Any]) -> None:
    try:
        fn()
    except CoilCutError as exc:
        ok = exc.code == code
        _print(
            f"{label} -> {'EXPECTED' if ok else 'UNEXPECTED'} {exc.code}",
            {"message": exc.message, "details": exc.details},
        )
        if not ok:
            raise SystemExit(f"{label}: expected {code}, got {exc.code}")
    else:
        raise SystemExit(f"{label}: expected {code}, but operation succeeded")


def scenario_731(ledger: Ledger) -> None:
    c1 = ledger.cut(
        cut_id="C1", piece_id="P0", expected_revision=1,
        u=30, left_child="L1", right_child="R1",
    )
    _print("C1: split P0 at local u=30 (orientation +1)", c1)

    flip = ledger.flip("R1", expected_revision=1)
    _print("FLIP R1: orientation reversed, root source unchanged", flip)

    c2 = ledger.cut(
        cut_id="C2", piece_id="R1", expected_revision=2,
        u=20, left_child="L2", right_child="R2",
    )
    _print("C2: SAME local u=20 but now measured from the -1 start", c2)
    print(
        "\nNOTE: L2 is root [80,100) orientation -1 (length 20).\n"
        "      It is NOT root [32,52).  The delivery D1 must say 80..100."
    )

    d1 = ledger.deliver(delivery_id="D1", piece_id="L2", required_length=20)
    _print("D1: deliver L2, real manifest", d1)

    _print("ROOT physical accounting", ledger.root_report("ROOT"))
    _print("Lineage of delivered L2", ledger.lineage("L2"))
    _print("Replay C2 (idempotent)", ledger.replay_cut("C2"))


def scenario_failures() -> None:
    # kerf out of bounds
    ledger, tmp = _fresh()
    with ledger:
        _expect("kerf out of bounds", "out_of_bounds", lambda: ledger.cut(
            cut_id="BAD_K", piece_id="P0", expected_revision=1,
            u=99, left_child="A", right_child="B",
        ))
    tmp.cleanup()

    # zero-length child
    ledger, tmp = _fresh()
    with ledger:
        _expect("zero-length left child", "zero_length_child", lambda: ledger.cut(
            cut_id="BAD_Z", piece_id="P0", expected_revision=1,
            u=0, left_child="A", right_child="B",
        ))
        _expect("zero-length right child", "zero_length_child", lambda: ledger.cut(
            cut_id="BAD_Z2", piece_id="P0", expected_revision=1,
            u=98, left_child="A", right_child="B",
        ))
    tmp.cleanup()

    # stale revision (after a flip)
    ledger, tmp = _fresh()
    with ledger:
        ledger.flip("P0", expected_revision=1)
        _expect("stale revision after flip", "stale_revision", lambda: ledger.cut(
            cut_id="BAD_R", piece_id="P0", expected_revision=1,
            u=30, left_child="A", right_child="B",
        ))
    tmp.cleanup()

    # duplicate cut id, different content
    ledger, tmp = _fresh()
    with ledger:
        ledger.cut(cut_id="C1", piece_id="P0", expected_revision=1,
                   u=30, left_child="L1", right_child="R1")
        _expect("duplicate cut with different content", "conflict", lambda: ledger.cut(
            cut_id="C1", piece_id="P0", expected_revision=1,
            u=20, left_child="L2", right_child="R2",
        ))
        replay = ledger.replay_cut("C1")
        _print("same cut replayed verbatim", replay["left"])
    tmp.cleanup()

    # duplicate delivery id, different content
    ledger, tmp = _fresh()
    with ledger:
        ledger.deliver(delivery_id="D1", piece_id="P0", required_length=100)
        _expect("duplicate delivery with different content", "conflict", lambda: ledger.deliver(
            delivery_id="D1", piece_id="P0", required_length=50,
        ))
        _print("same delivery replayed verbatim", ledger.replay_delivery("D1")["manifest"])
    tmp.cleanup()

    # two connections race for the same parent (sequential simulation)
    ledger, tmp = _fresh()
    with ledger:
        ledger.cut(cut_id="C1", piece_id="P0", expected_revision=1,
                   u=30, left_child="L1", right_child="R1")
        _expect("second cut on already-consumed parent", "piece_consumed", lambda: ledger.cut(
            cut_id="C2", piece_id="P0", expected_revision=1,
            u=40, left_child="L2", right_child="R2",
        ))
    tmp.cleanup()

    # stale preview submitted after a flip
    ledger, tmp = _fresh()
    with ledger:
        preview = ledger.preview_cut(piece_id="P0", u=30, left_child="L1", right_child="R1")
        _print("preview computed with orientation +1", {
            "revision": preview["revision"],
            "orientation": preview["orientation"],
            "left": preview["geometry"]["left"],
        })
        ledger.flip("P0", expected_revision=1)
        _expect("commit stale preview after flip", "stale_revision", lambda: ledger.commit_preview(
            "C1", preview,
        ))
    tmp.cleanup()

    # delivered piece cannot be cut or flipped
    ledger, tmp = _fresh()
    with ledger:
        ledger.deliver(delivery_id="D1", piece_id="P0", required_length=100)
        _expect("cut a delivered piece", "piece_consumed", lambda: ledger.cut(
            cut_id="C2", piece_id="P0", expected_revision=1,
            u=30, left_child="A", right_child="B",
        ))
        _expect("flip a delivered piece", "piece_consumed", lambda: ledger.flip(
            "P0", expected_revision=1,
        ))
    tmp.cleanup()

    # length mismatch on delivery
    ledger, tmp = _fresh()
    with ledger:
        _expect("delivery length mismatch", "length_mismatch", lambda: ledger.deliver(
            delivery_id="D1", piece_id="P0", required_length=99,
        ))
    tmp.cleanup()

    # reopen: state survives and a second init is refused
    ledger, tmp = _fresh()
    db = ledger.path
    with ledger:
        ledger.cut(cut_id="C1", piece_id="P0", expected_revision=1,
                   u=30, left_child="L1", right_child="R1")
    reopened = Ledger(db)
    with reopened:
        _print("reopened ledger root accounting", reopened.root_report("ROOT")["totals"])
        _expect("re-initialize a populated ledger", "already_initialized", lambda: initialize_from_stock(
            reopened, load_stock(STOCK_PATH),
        ))
    tmp.cleanup()


def scenario_concurrent_race() -> None:
    """Two real connections try to consume P0 at the same time."""

    tmp = tempfile.TemporaryDirectory()
    db = str(Path(tmp.name) / "race.db")
    first = Ledger(db)
    initialize_from_stock(first, load_stock(STOCK_PATH))
    second = Ledger(db)
    outcomes: list[tuple[str, str]] = []
    barrier = threading.Barrier(2)

    def attempt(ledger: Ledger, tag: str) -> None:
        barrier.wait()
        try:
            ledger.cut(
                cut_id=f"RACE_{tag}", piece_id="P0", expected_revision=1,
                u=30, left_child=f"L_{tag}", right_child=f"R_{tag}",
            )
            outcomes.append((tag, "committed"))
        except CoilCutError as exc:
            outcomes.append((tag, f"rejected: {exc.code}"))

    threads = [
        threading.Thread(target=attempt, args=(first, "A")),
        threading.Thread(target=attempt, args=(second, "B")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    report = first.root_report("ROOT")
    first.close()
    second.close()
    tmp.cleanup()
    _print(
        "two connections race for P0 (exactly one may win)",
        {"outcomes": sorted(outcomes), "root_totals": report["totals"]},
    )
    if sum(1 for _, status in outcomes if status == "committed") != 1:
        raise SystemExit("race demo expected exactly one committed cut")


def main() -> None:
    ledger, tmp = _fresh()
    with ledger:
        scenario_731(ledger)
    tmp.cleanup()
    scenario_failures()
    scenario_concurrent_race()


if __name__ == "__main__":  # pragma: no cover
    main()
