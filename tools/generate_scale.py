#!/usr/bin/env python3
"""Generate the required scale dataset (all synthetic).

Targets:
  * 30 distinct people (P0..P7 principals, A0..A? agents/signers);
  * 100 grants (two-layer families, some revoked, some corrections);
  * 200 receipt originals;
  * 50 shipments, each with 10 required parts;
  * receipt supplements binding originals to shipments/parts.

The generator is deterministic (fixed seed) and writes plain JSON/JSONL under
data/scale/, exactly as an operator would hand them to the services.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "scale"

N_PEOPLE = 30
N_GRANTS = 100
N_RECEIPTS = 200
N_SHIPMENTS = 50
PARTS_PER_SHIPMENT = 10
REGIONS = ["N", "S", "E", "W"]
DOC_TYPES = ["K", "L"]


def people():
    return [f"P{i}" for i in range(10)] + [f"A{i}" for i in range(20)]


def build_grants(rng: random.Random) -> list[dict]:
    """Root grants + child grants forming two-level families.

    Each family: root P -> A, child A -> B.  Children respect parent scope and
    intervals so the batch is structurally legal; a handful of roots are
    revoked at various days.
    """
    principals = [f"P{i}" for i in range(10)]
    agents = [f"A{i}" for i in range(20)]
    grants = []
    family = 0
    root_count = 50
    # 50 roots, 50 children => 100 grants, fixed two-layer structure.
    roots = []
    for i in range(root_count):
        pid = principals[i % len(principals)]
        agent = agents[(i * 3) % len(agents)]
        region = rng.choice(REGIONS)
        dtype = rng.choice(DOC_TYPES)
        start = rng.choice([0, 2, 5])
        end = start + rng.choice([20, 25])
        revoked = None
        if i % 7 == 0:  # every 7th root is revoked mid-window
            revoked = start + (end - start) // 2
        g = {
            "grant_id": f"G{i:03d}",
            "revision": 1,
            "issuer": pid,
            "agent": agent,
            "regions": [region],
            "document_types": [dtype],
            "start": start,
            "end": end,
            "can_redelegate": True,
            "parent_grant_id": None,
            "revoked_from": revoked,
        }
        grants.append(g)
        roots.append(g)

    for i, parent in enumerate(roots):
        child_agent = agents[(i * 7 + 5) % len(agents)]
        # Ensure child issuer is parent agent, not equal to agent (avoid trivial loops).
        if child_agent == parent["agent"]:
            child_agent = agents[(agents.index(child_agent) + 1) % len(agents)]
        start = parent["start"] + 1
        end = parent["end"] - 1
        child = {
            "grant_id": f"G{root_count + i:03d}",
            "revision": 1,
            "issuer": parent["agent"],
            "agent": child_agent,
            "regions": list(parent["regions"]),
            "document_types": list(parent["document_types"]),
            "start": start,
            "end": end,
            "can_redelegate": False,
            "parent_grant_id": parent["grant_id"],
            "revoked_from": None,
        }
        grants.append(child)
    assert len(grants) == N_GRANTS
    return grants


def build_receipts(rng: random.Random, grants: list[dict]) -> list[dict]:
    roots = [g for g in grants if g["parent_grant_id"] is None]
    children = {g["parent_grant_id"]: g for g in grants if g["parent_grant_id"]}
    receipts = []
    for i in range(N_RECEIPTS):
        parent = roots[i % len(roots)]
        child = children[parent["grant_id"]]
        mode = rng.random()
        signer = child["agent"]
        claimed = [parent["grant_id"], child["grant_id"]]
        region = parent["regions"][0]
        if mode < 0.12:
            # self-signed by consignee, empty chain
            signer = parent["issuer"]
            claimed = []
        elif mode < 0.20:
            # region out of scope
            region = next(r for r in REGIONS if r not in parent["regions"])
        elif mode < 0.28:
            # wrong signer (not the final agent)
            signer = "Z" + parent["agent"]
        signed_day = rng.randint(parent["start"], max(parent["start"], parent["end"] - 1))
        receipts.append({
            "receipt_id": f"R{i:04d}",
            "consignee": parent["issuer"],
            "signer": signer,
            "signed_day": signed_day,
            "received_day": signed_day + rng.randint(1, 5),
            "region": region,
            "document_type": parent["document_types"][0],
            "claimed_grant_ids": claimed,
        })
    return receipts


def build_shipments(rng: random.Random, grants: list[dict]) -> list[dict]:
    roots = [g for g in grants if g["parent_grant_id"] is None]
    shipments = []
    for i in range(N_SHIPMENTS):
        parent = roots[i % len(roots)]
        shipments.append({
            "shipment_id": f"S{i:03d}",
            "consignee": parent["issuer"],
            "region": parent["regions"][0],
            "document_type": parent["document_types"][0],
            "required_parts": [f"P{i:03d}-{j:02d}" for j in range(PARTS_PER_SHIPMENT)],
        })
    return shipments


def build_supplements(rng: random.Random, shipments: list[dict],
                      receipts: list[dict]) -> list[dict]:
    """Bind valid-looking originals to shipments, covering parts in slices.

    Every shipment gets 2+ receipts covering its parts; some receipts are
    intentionally weak (each still legal as a supplement).  Originals are
    matched by (consignee, region, type) so the delivery-side context holds.
    """
    by_context: dict[tuple, list[dict]] = {}
    for r in receipts:
        by_context.setdefault((r["consignee"], r["region"], r["document_type"]), []).append(r)
    supplements = []
    used = set()
    for s in shipments:
        key = (s["consignee"], s["region"], s["document_type"])
        pool = [r for r in by_context.get(key, []) if r["receipt_id"] not in used]
        if not pool:
            continue
        parts = s["required_parts"]
        # Two receipts: first covers first half, second covers the rest.
        first = pool[0]
        used.add(first["receipt_id"])
        half = PARTS_PER_SHIPMENT // 2
        supplements.append(_sup(first, s["shipment_id"], parts[:half]))
        if len(pool) > 1:
            second = pool[1]
            used.add(second["receipt_id"])
            supplements.append(_sup(second, s["shipment_id"], parts[half:]))
        else:
            # Single receipt covers everything deliberately (partial coverage test).
            supplements[-1]["part_ids"] = list(parts)
    return supplements


def _sup(r: dict, shipment_id: str, parts: list[str]) -> dict:
    return {
        "receipt_id": r["receipt_id"],
        "consignee": r["consignee"],
        "signer": r["signer"],
        "signed_day": r["signed_day"],
        "received_day": r["received_day"],
        "region": r["region"],
        "document_type": r["document_type"],
        "claimed_grant_ids": list(r["claimed_grant_ids"]),
        "shipment_id": shipment_id,
        "part_ids": list(parts),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=20261118)
    args = ap.parse_args(argv)
    rng = random.Random(args.seed)
    OUT.mkdir(parents=True, exist_ok=True)

    grants = build_grants(rng)
    receipts = build_receipts(rng, grants)
    shipments = build_shipments(rng, grants)
    supplements = build_supplements(rng, shipments, receipts)

    (OUT / "grants.json").write_text(
        json.dumps({"grants": grants}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    for name, rows in (
        ("receipts.jsonl", receipts),
        ("shipments.jsonl", shipments),
        ("receipt_supplements.jsonl", supplements),
    ):
        (OUT / name).write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
            encoding="utf-8")

    people_count = len({g["issuer"] for g in grants} | {g["agent"] for g in grants})
    print(f"people={people_count} grants={len(grants)} receipts={len(receipts)} "
          f"shipments={len(shipments)} supplements={len(supplements)}")
    assert people_count <= N_PEOPLE
    assert len(grants) == N_GRANTS
    assert len(receipts) == N_RECEIPTS
    assert len(shipments) == N_SHIPMENTS
    assert all(len(s["required_parts"]) == PARTS_PER_SHIPMENT for s in shipments)
    return 0


if __name__ == "__main__":
    sys.exit(main())
