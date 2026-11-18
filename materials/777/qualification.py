"""Store the first official seat publication and return immutable receipts."""

import json
import os
import tempfile
from pathlib import Path


def load(path):
    path = Path(path)
    if not path.exists():
        return {"current": {}, "locked": [], "receipts": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def save(path, state):
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        json.dump(state, stream, ensure_ascii=False, sort_keys=True)
        temporary = stream.name
    try:
        os.replace(temporary, path)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()


def publish_first(path, release, slots, rankings):
    state = load(path)
    proposed = {slot: rankings[match] for slot, match in slots.items()}
    if release in state["receipts"]:
        receipt = state["receipts"][release]
        if receipt["slots"] != proposed:
            raise ValueError("release identifier already used for different scores")
        return receipt
    if state["current"]:
        raise ValueError("qualification already published")
    if len({row["team"] for row in proposed.values()}) != len(proposed):
        raise ValueError("a team cannot occupy two seats")
    receipt = {"format": "bracket-v1", "release": release, "slots": proposed}
    state["current"] = proposed
    state["receipts"][release] = receipt
    save(path, state)
    return receipt


def mark_started(path, seat):
    state = load(path)
    if seat not in state["current"]:
        raise ValueError("no team was assigned to this seat")
    if seat not in state["locked"]:
        state["locked"].append(seat)
        save(path, state)
    return state["current"][seat]
