"""Pure authority/chain evaluation for a single receipt original.

Deliberately independent of storage: the service resolves the claimed chain to
immutable grant revisions and hands them here.  A receipt is assessed *only*
against the chain it actually claims -- there is no fallback to another grant,
and no identity is ever inferred from a name.

Rules implemented here:
  * a self-signed receipt (signer is the consignee) may carry an empty chain;
  * a delegated receipt must claim a complete path, root first;
  * maximum depth is two levels;
  * the root issuer must be the consignee (the principal);
  * each level's issuer must be the previous level's agent, the parent must
    allow redelegation, and interval + region/type sets may not exceed parent;
  * the actual signer must be the final claimed agent;
  * the receipt is invalid if its signing day reaches any revocation day on the
    claimed path -- a parent revocation truncates everything beneath it;
  * intaken day (received_day) is never consulted for validity.
"""

from __future__ import annotations

from typing import Any


def _effective(grant: dict, day: int) -> bool:
    if not (grant["start"] <= day < grant["end"]):
        return False
    revoked = grant["revoked_from"]
    if revoked is not None and day >= revoked:
        return False
    return True


def _scope_failure(grant: dict, receipt: dict) -> str | None:
    if receipt["region"] not in grant["regions"]:
        return f"region {receipt['region']!r} not in {grant['regions']}"
    if receipt["document_type"] not in grant["document_types"]:
        return f"document_type {receipt['document_type']!r} not in {grant['document_types']}"
    return None


def _link_failure(child: dict, parent: dict) -> str | None:
    """Structural checks between an adjacent parent/child pair."""
    if parent["parent_grant_id"] is not None:
        return "parent is itself delegated; maximum depth is 2"
    if child["issuer"] != parent["agent"]:
        return (
            f"issuer {child['issuer']!r} is not the parent agent {parent['agent']!r}"
        )
    if child["parent_grant_id"] != parent["grant_id"]:
        return (
            f"parent_grant_id {child['parent_grant_id']!r} does not match claimed "
            f"parent {parent['grant_id']!r}"
        )
    if not parent["can_redelegate"]:
        return f"parent {parent['grant_id']!r} does not permit redelegation"
    if child["start"] < parent["start"] or child["end"] > parent["end"]:
        return (
            f"nominal [{child['start']},{child['end']}) exceeds parent interval "
            f"[{parent['start']},{parent['end']})"
        )
    if not set(child["regions"]).issubset(set(parent["regions"])):
        return f"regions {child['regions']} exceed parent regions {parent['regions']}"
    if not set(child["document_types"]).issubset(set(parent["document_types"])):
        return (
            f"document_types {child['document_types']} exceed parent "
            f"document_types {parent['document_types']}"
        )
    return None


def _rev(grant: dict) -> Any:
    return grant.get("resolved_revision", grant.get("revision"))


def _valid_level(idx: int, grant: dict, receipt: dict) -> dict:
    return {
        "level": idx,
        "grant_id": grant["grant_id"],
        "revision": _rev(grant),
        "issuer": grant["issuer"],
        "agent": grant["agent"],
        "nominal": [grant["start"], grant["end"]],
        "revoked_from": grant["revoked_from"],
        "signed_day": receipt["signed_day"],
        "effective": True,
        "reason": "effective at signing within scope",
    }


def _invalid_level(idx: int, grant_id: str, kind: str, reason: str,
                   grant: dict | None = None) -> dict:
    return {
        "level": idx,
        "grant_id": grant_id,
        "revision": _rev(grant) if grant and "_missing" not in grant else None,
        "effective": False,
        "kind": kind,
        "reason": reason,
    }


def _unchecked_level(idx: int, grant_id: str, broken_at: int | None) -> dict:
    return {
        "level": idx,
        "grant_id": grant_id,
        "revision": None,
        "effective": False,
        "kind": "not_checked",
        "reason": f"path already broken at level {broken_at}",
    }


def assess_receipt(receipt: dict, chain: list[dict], as_of: int) -> dict:
    """Assess one receipt original against its claimed chain."""
    signer = receipt["signer"]
    consignee = receipt["consignee"]
    claimed = receipt["claimed_chain"]
    day = receipt["signed_day"]

    levels: list[dict] = []
    reason: str | None = None
    broken_at: int | None = None
    root_origin_error: str | None = None
    signer_error: str | None = None

    # ---- pre-checks that are not tied to one level ------------------------
    if signer is None:
        reason = "receipt has no actual signer"
        broken_at = None
    elif not claimed:
        if signer != consignee:
            reason = (
                f"signer {signer!r} claims no delegation and is not the consignee "
                f"{consignee!r}"
            )
    elif len(claimed) > 2:
        reason = f"claimed chain of {len(claimed)} levels exceeds the maximum of 2"

    # ---- per-level evaluation --------------------------------------------
    if reason is None and claimed:
        for idx, grant in enumerate(chain):
            gid = claimed[idx]
            if "_missing" in grant:
                reason, broken_at = f"grant {gid!r} is not known", idx
                levels.append(_invalid_level(idx, gid, "resolve", reason))
                break
            if idx == 0:
                if grant["parent_grant_id"] is not None:
                    reason = (
                        f"grant {gid!r} is a delegated child but its parent is not "
                        f"claimed; the path is incomplete"
                    )
                    broken_at = idx
                    levels.append(_invalid_level(idx, gid, "path", reason, grant))
                    break
                if grant["issuer"] != consignee:
                    root_origin_error = (
                        f"root issuer {grant['issuer']!r} is not the consignee "
                        f"{consignee!r}"
                    )
                    reason = root_origin_error
                    broken_at = idx
                    levels.append(_invalid_level(idx, gid, "origin", reason, grant))
                    break
            else:
                link = _link_failure(grant, chain[idx - 1])
                if link:
                    reason = f"level {idx} ({gid}): {link}"
                    broken_at = idx
                    levels.append(_invalid_level(idx, gid, "link", reason, grant))
                    break
            if not _effective(grant, day):
                reason = (
                    f"grant {gid!r} rev{_rev(grant)} not effective at signed_day {day} "
                    f"(nominal [{grant['start']},{grant['end']}), "
                    f"revoked_from {grant['revoked_from']})"
                )
                broken_at = idx
                levels.append(_invalid_level(idx, gid, "time", reason, grant))
                break
            scope = _scope_failure(grant, receipt)
            if scope:
                reason = f"grant {gid!r}: {scope}"
                broken_at = idx
                levels.append(_invalid_level(idx, gid, "scope", reason, grant))
                break
            levels.append(_valid_level(idx, grant, receipt))

        # Fill in levels after the break as explicitly not checked.
        if broken_at is not None:
            for idx in range(broken_at + 1, len(claimed)):
                levels.append(_unchecked_level(idx, claimed[idx], broken_at))
        if reason is None:
            last_agent = chain[-1]["agent"]
            if signer != last_agent:
                signer_error = (
                    f"signer {signer!r} is not the final claimed agent {last_agent!r}"
                )
                reason = signer_error

    if reason is not None:
        if not levels:
            # Rejections with no level (missing signer / depth / empty chain).
            for idx, gid in enumerate(claimed):
                levels.append(_unchecked_level(idx, gid, None))
        return _result(receipt, claimed, as_of, False, reason, broken_at, levels)

    return _result(receipt, claimed, as_of, True, None, None, levels)


def _result(receipt: dict, claimed: list[str], as_of: int, valid: bool,
            reason: str | None, broken_at: int | None, levels: list[dict]) -> dict:
    return {
        "receipt_id": receipt["receipt_id"],
        "consignee": receipt["consignee"],
        "signer": receipt["signer"],
        "signed_day": receipt["signed_day"],
        "received_day": receipt["received_day"],
        "region": receipt["region"],
        "document_type": receipt["document_type"],
        "claimed_grant_ids": claimed,
        "valid": valid,
        "as_of": as_of,
        "reason": reason,
        "broken_at_level": broken_at,
        "levels": levels,
    }
