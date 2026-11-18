"""Redemption ledger: transactional redeem/cancel plus frozen request replay.

The write side owns:

* instance remaining counts (``coupon_instances.remaining``),
* successful redemption facts (``redemptions`` + ``redemption_uses``),
* cancellation facts (``cancellations`` + ``cancellation_results``),
* first-request content/result freezing (``request_results``).

Every mutation runs inside ``BEGIN IMMEDIATE``.  That single write lock means
the catalog pointer, the eligibility evaluation, the balance checks and the
decrements are all observed against one consistent snapshot, and two
independent connections cannot both consume the last use of an instance.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping

from . import catalog, errors, jsonutil
from .errors import Issue, issue
from .models import (
    CouponInput,
    OrderLine,
    QualificationRequest,
    TemplatePin,
    cancel_content,
    content_json,
    parse_cancel_content,
    parse_redeem_content,
    redeem_content,
)
from .qualification import Evaluation, evaluate

REJECTED = "REJECTED"
REDEEMED = "redeemed"
CANCELLED = "cancelled"

_FAMILY_ORDER = [
    errors.INVALID_REQUEST,
    errors.COMBINATION,
    errors.OWNERSHIP,
    errors.QUALIFICATION,
    errors.STALE_CATALOG,
    errors.BALANCE,
    errors.NOT_FOUND,
]


@dataclass(frozen=True)
class RedeemResponse:
    request_id: str
    replayed: bool
    frozen: bool
    result: dict[str, Any]
    current_status: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "entry": "ledger",
            "kind": "redeem",
            "request_id": self.request_id,
            "replayed": self.replayed,
            "frozen": self.frozen,
            "result": self.result,
        }
        if self.current_status is not None:
            payload["current_status"] = self.current_status
        return payload


@dataclass(frozen=True)
class CancelResponse:
    request_id: str
    replayed: bool
    frozen: bool
    result: dict[str, Any]
    order_status: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "entry": "ledger",
            "kind": "cancel",
            "request_id": self.request_id,
            "replayed": self.replayed,
            "frozen": self.frozen,
            "result": self.result,
        }
        if self.order_status is not None:
            payload["order_status"] = self.order_status
        return payload


class _BalanceLost(Exception):
    def __init__(self, instance_id: str):
        super().__init__(instance_id)
        self.instance_id = instance_id


class _Transaction:
    def __init__(self, con: sqlite3.Connection):
        self.con = con

    def __enter__(self) -> sqlite3.Connection:
        self.con.execute("BEGIN IMMEDIATE")
        return self.con

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            try:
                self.con.execute("COMMIT")
            except Exception:
                self.con.execute("ROLLBACK")
                raise
        else:
            self.con.execute("ROLLBACK")
        return False


def _primary(issues) -> str:
    families = {item.family for item in issues}
    for family in _FAMILY_ORDER:
        if family in families:
            return family
    return errors.INVALID_REQUEST


def _reject(reason: str, **detail: Any) -> dict[str, Any]:
    item = issue(reason, **detail)
    return {
        "status": REJECTED,
        "code": item.family,
        "issues": [item.as_dict()],
        "deducted": False,
    }


def _parse_stored(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def _same_content(stored: str, content: Mapping[str, Any]) -> bool:
    return jsonutil.equal_serialized(stored, content)


# ---------------------------------------------------------------------------
# shared reads (always inside the caller's transaction)
# ---------------------------------------------------------------------------


def _instance(con: sqlite3.Connection, instance_id: str) -> sqlite3.Row | None:
    return con.execute(
        "SELECT instance_id, user_id, template_id, remaining "
        "FROM coupon_instances WHERE instance_id=?",
        (instance_id,),
    ).fetchone()


def _existing_order(con: sqlite3.Connection, order_id: str) -> sqlite3.Row | None:
    return con.execute(
        "SELECT order_id, user_id, request_id, request_json, "
        "qualification_json, status FROM redemptions WHERE order_id=?",
        (order_id,),
    ).fetchone()


def _stored_request_result(
    con: sqlite3.Connection, request_id: str
) -> sqlite3.Row | None:
    return con.execute(
        "SELECT request_id, kind, request_json, result_json "
        "FROM request_results WHERE request_id=?",
        (request_id,),
    ).fetchone()


def _freeze(
    con: sqlite3.Connection,
    request_id: str,
    kind: str,
    request_json: str,
    result: Mapping[str, Any],
) -> None:
    con.execute(
        "INSERT INTO request_results(request_id, kind, request_json, result_json) "
        "VALUES(?,?,?,?)",
        (request_id, kind, request_json, jsonutil.dumps(result)),
    )


def _pin_coupons(
    con: sqlite3.Connection,
    user_id: str,
    lines: tuple[OrderLine, ...],
    coupon_ids: tuple[str, ...],
) -> tuple[QualificationRequest, dict[str, int], list[Issue]]:
    """Pin each instance's *current* template version inside the transaction.

    The qualification rules themselves stay pure.  This adapter supplies the
    ownership and explicit ``(template_id, version)`` binding from the ledger,
    plus the remaining counts observed under the same write lock.
    """

    issues: list[Issue] = []
    coupons: list[CouponInput] = []
    pins: dict[tuple[str, int], TemplatePin] = {}
    remaining: dict[str, int] = {}

    for instance_id in coupon_ids:
        row = _instance(con, instance_id)
        if row is None:
            issues.append(issue("unknown_coupon", instance_id=instance_id))
            remaining[instance_id] = 0
            continue
        remaining[instance_id] = int(row["remaining"])
        template_id = row["template_id"]
        current = catalog.get_current(con, template_id)
        if current is None:
            issues.append(
                issue(
                    "unknown_template_version",
                    instance_id=instance_id,
                    template_id=template_id,
                    template_version=None,
                )
            )
            continue
        coupons.append(
            CouponInput(
                instance_id=instance_id,
                owner_user_id=row["user_id"],
                template_id=template_id,
                template_version=current.version,
            )
        )
        pins[(current.template_id, current.version)] = TemplatePin(
            template_id=current.template_id,
            version=current.version,
            spec=current.spec,
        )

    request = QualificationRequest(
        user_id=user_id,
        lines=lines,
        coupons=tuple(coupons),
        templates=tuple(pins.values()),
    )
    return request, remaining, issues


def _balance_issues(
    coupon_ids: tuple[str, ...], remaining: Mapping[str, int]
) -> list[Issue]:
    return [
        issue(
            "no_remaining_uses",
            instance_id=instance_id,
            remaining=remaining.get(instance_id, 0),
        )
        for instance_id in coupon_ids
        if remaining.get(instance_id, 0) < 1
    ]


def _result_for_redemption(
    con: sqlite3.Connection, order_row: sqlite3.Row
) -> dict[str, Any]:
    """The original frozen success result for an order."""

    frozen = con.execute(
        "SELECT result_json FROM request_results WHERE request_id=? AND kind='redeem'",
        (order_row["request_id"],),
    ).fetchone()
    if frozen is not None:
        return _parse_stored(frozen["result_json"])
    qualification = _parse_stored(order_row["qualification_json"])
    gross = qualification.get("gross") if isinstance(qualification, dict) else None
    discount = (
        qualification.get("discount") if isinstance(qualification, dict) else None
    )
    payable = qualification.get("payable") if isinstance(qualification, dict) else None
    return {
        "status": REDEEMED,
        "order_id": order_row["order_id"],
        "gross": gross,
        "discount": discount,
        "payable": payable,
    }


# ---------------------------------------------------------------------------
# redeem
# ---------------------------------------------------------------------------


def redeem(
    con: sqlite3.Connection, request_id: str, request: Mapping[str, Any]
) -> RedeemResponse:
    """Apply a redemption request exactly once per ``request_id``.

    The first request for an id freezes its content and result (including
    rejection).  Replays with equal JSON value return the frozen result;
    replays with different content are refused without touching balances.
    """

    if not isinstance(request_id, str) or not request_id:
        raise errors.RequestError("request_id must be a non-empty string")

    content = redeem_content(request)
    content_text = content_json(content)

    try:
        return _redeem_tx(con, request_id, content, content_text)
    except _BalanceLost as lost:
        # Defensive only: BEGIN IMMEDIATE makes this unreachable with real
        # concurrency.  No coupon was consumed; freeze the rejection separately.
        result = _reject(
            "no_remaining_uses", instance_id=lost.instance_id, remaining=0
        )
        with _Transaction(con):
            _freeze(con, request_id, "redeem", content_text, result)
        return RedeemResponse(request_id, False, True, result)


def _redeem_tx(
    con: sqlite3.Connection,
    request_id: str,
    content: Mapping[str, Any],
    content_text: str,
) -> RedeemResponse:
    with _Transaction(con):
        stored = _stored_request_result(con, request_id)
        if stored is not None:
            if stored["kind"] != "redeem":
                return RedeemResponse(
                    request_id,
                    False,
                    True,
                    _reject(
                        "request_id_kind_mismatch",
                        request_id=request_id,
                        first_kind=stored["kind"],
                        requested_kind="redeem",
                    ),
                )
            if not _same_content(stored["request_json"], content):
                return RedeemResponse(
                    request_id,
                    False,
                    True,
                    _reject(
                        "request_id_content_mismatch", request_id=request_id
                    ),
                )
            result = _parse_stored(stored["result_json"])
            order_id = content.get("order_id")
            order = _existing_order(con, order_id) if isinstance(order_id, str) else None
            return RedeemResponse(
                request_id,
                True,
                True,
                result,
                current_status=order["status"] if order else None,
            )

        # --- validate shape; an invalid first request is frozen too -------
        try:
            order_id, user_id, lines, coupon_ids = parse_redeem_content(content)
        except errors.RequestError as exc:
            result = {
                "status": REJECTED,
                "code": errors.INVALID_REQUEST,
                "issues": [issue("invalid_request", message=str(exc)).as_dict()],
                "deducted": False,
            }
            _freeze(con, request_id, "redeem", content_text, result)
            return RedeemResponse(request_id, False, True, result)

        # --- an order id, once redeemed, is bound to its original content -
        existing = _existing_order(con, order_id)
        if existing is not None:
            if not _same_content(existing["request_json"], content):
                result = _reject(
                    "order_content_mismatch",
                    order_id=order_id,
                    bound_request_id=existing["request_id"],
                )
                _freeze(con, request_id, "redeem", content_text, result)
                return RedeemResponse(request_id, False, True, result)
            result = _result_for_redemption(con, existing)
            _freeze(con, request_id, "redeem", content_text, result)
            return RedeemResponse(
                request_id,
                True,
                True,
                result,
                current_status=existing["status"],
            )

        # --- actual eligibility against the pinned catalog snapshot ------
        qual_request, remaining, build_issues = _pin_coupons(
            con, user_id, lines, coupon_ids
        )
        evaluation = evaluate(qual_request)

        issues: list[Issue] = list(build_issues)
        issues.extend(evaluation.issues)
        if not issues:
            issues.extend(_balance_issues(coupon_ids, remaining))
        if issues:
            result = {
                "status": REJECTED,
                "code": _primary(issues),
                "issues": [item.as_dict() for item in issues],
                "deducted": False,
            }
            _freeze(con, request_id, "redeem", content_text, result)
            return RedeemResponse(request_id, False, True, result)

        assert evaluation.plan is not None
        plan = evaluation.plan

        # --- consume one use of every requested coupon, or none ----------
        for coupon_plan in plan.coupons:
            cursor = con.execute(
                "UPDATE coupon_instances SET remaining=remaining-1 "
                "WHERE instance_id=? AND remaining>=1",
                (coupon_plan.instance_id,),
            )
            if cursor.rowcount != 1:
                raise _BalanceLost(coupon_plan.instance_id)

        payment = {
            "status": REDEEMED,
            "order_id": order_id,
            "user_id": user_id,
            "gross": plan.gross,
            "discount": plan.total_discount,
            "payable": plan.payable,
            "coupons": [
                {
                    "instance_id": coupon.instance_id,
                    "template_id": coupon.template_id,
                    "template_version": coupon.template_version,
                    "discount": coupon.discount,
                    "used_count": 1,
                }
                for coupon in plan.coupons
            ],
        }

        con.execute(
            "INSERT INTO redemptions(order_id, user_id, request_id, request_json, "
            "qualification_json, status) VALUES(?,?,?,?,?,?)",
            (
                order_id,
                user_id,
                request_id,
                content_text,
                jsonutil.dumps(plan.as_dict()),
                "ACTIVE",
            ),
        )
        for coupon in plan.coupons:
            con.execute(
                "INSERT INTO redemption_uses(order_id, instance_id, used_count, "
                "template_version, discount) VALUES(?,?,?,?,?)",
                (
                    order_id,
                    coupon.instance_id,
                    1,
                    coupon.template_version,
                    coupon.discount,
                ),
            )
        _freeze(con, request_id, "redeem", content_text, payment)
        return RedeemResponse(request_id, False, True, payment, "ACTIVE")


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


def cancel(
    con: sqlite3.Connection, request_id: str, request: Mapping[str, Any]
) -> CancelResponse:
    """Cancel a successful redemption exactly once and refund its uses.

    Refunds are driven by the persisted ``redemption_uses`` rows, so later
    template revisions or retirement do not change what is returned.
    """

    if not isinstance(request_id, str) or not request_id:
        raise errors.RequestError("request_id must be a non-empty string")

    content = cancel_content(request)
    content_text = content_json(content)

    with _Transaction(con):
        stored = _stored_request_result(con, request_id)
        if stored is not None:
            if stored["kind"] != "cancel":
                return CancelResponse(
                    request_id,
                    False,
                    True,
                    _reject(
                        "request_id_kind_mismatch",
                        request_id=request_id,
                        first_kind=stored["kind"],
                        requested_kind="cancel",
                    ),
                )
            if not _same_content(stored["request_json"], content):
                return CancelResponse(
                    request_id,
                    False,
                    True,
                    _reject(
                        "cancel_id_content_mismatch", request_id=request_id
                    ),
                )
            result = _parse_stored(stored["result_json"])
            order_id = content.get("order_id")
            order = _existing_order(con, order_id) if isinstance(order_id, str) else None
            return CancelResponse(
                request_id,
                True,
                True,
                result,
                order_status=order["status"] if order else None,
            )

        try:
            cancel_id, order_id, user_id = parse_cancel_content(content)
        except errors.RequestError as exc:
            result = {
                "status": REJECTED,
                "code": errors.INVALID_REQUEST,
                "issues": [issue("invalid_request", message=str(exc)).as_dict()],
                "deducted": False,
            }
            _freeze(con, request_id, "cancel", content_text, result)
            return CancelResponse(request_id, False, True, result)

        def finish(result: dict[str, Any], order_status: str | None = None) -> CancelResponse:
            _freeze(con, request_id, "cancel", content_text, result)
            return CancelResponse(request_id, False, True, result, order_status)

        order = _existing_order(con, order_id)
        if order is None:
            return finish(
                {
                    "status": REJECTED,
                    "code": errors.NOT_FOUND,
                    "issues": [issue("not_found", order_id=order_id).as_dict()],
                    "deducted": False,
                }
            )
        if order["user_id"] != user_id:
            return finish(
                {
                    "status": REJECTED,
                    "code": errors.OWNERSHIP,
                    "issues": [
                        issue(
                            "order_user_mismatch",
                            order_id=order_id,
                            order_user_id=order["user_id"],
                            request_user_id=user_id,
                        ).as_dict()
                    ],
                    "deducted": False,
                },
                order["status"],
            )

        already = con.execute(
            "SELECT cancel_id, order_id, request_json FROM cancellations "
            "WHERE cancel_id=? OR order_id=?",
            (cancel_id, order_id),
        ).fetchone()

        if already is not None and already["cancel_id"] == cancel_id:
            # Same logical cancellation retried under a fresh request id:
            # replay the frozen facts, never refund twice.
            original = con.execute(
                "SELECT request_json, result_json FROM cancellation_results "
                "WHERE cancel_id=?",
                (cancel_id,),
            ).fetchone()
            if original is not None:
                if not _same_content(original["request_json"], content):
                    result = _reject(
                        "cancel_id_content_mismatch", cancel_id=cancel_id
                    )
                    _freeze(con, request_id, "cancel", content_text, result)
                    return CancelResponse(
                        request_id, False, True, result, order_status="CANCELLED"
                    )
                result = _parse_stored(original["result_json"])
                _freeze(con, request_id, "cancel", content_text, result)
                return CancelResponse(
                    request_id, True, True, result, order_status="CANCELLED"
                )
            if _same_content(already["request_json"], content):
                # A cancellation recorded before this service existed (or
                # without the additive table).  Confirm it from the original
                # columns; do not refund again.
                result = {
                    "status": CANCELLED,
                    "cancel_id": cancel_id,
                    "order_id": order_id,
                    "user_id": user_id,
                    "refunded": [],
                    "note": "replayed from cancellations; no new refund",
                }
                _freeze(con, request_id, "cancel", content_text, result)
                return CancelResponse(
                    request_id, True, True, result, order_status=order["status"]
                )

        if already is not None or order["status"] != "ACTIVE":
            return finish(
                {
                    "status": REJECTED,
                    "code": errors.CANCEL_CONFLICT,
                    "issues": [
                        issue(
                            "already_cancelled",
                            order_id=order_id,
                            existing_cancel_id=already["cancel_id"] if already else None,
                            attempted_cancel_id=cancel_id,
                        ).as_dict()
                    ],
                    "deducted": False,
                },
                order["status"],
            )

        uses = con.execute(
            "SELECT instance_id, used_count FROM redemption_uses "
            "WHERE order_id=? ORDER BY rowid",
            (order_id,),
        ).fetchall()
        if not uses:
            return finish(
                {
                    "status": REJECTED,
                    "code": errors.NOT_FOUND,
                    "issues": [
                        issue("not_found", order_id=order_id, uses=0).as_dict()
                    ],
                    "deducted": False,
                },
                order["status"],
            )

        # Verify every referenced instance still exists *before* mutating, so
        # the refund is all-or-nothing and never needs a mid-loop rollback.
        before: dict[str, int] = {}
        for use in uses:
            instance_id = use["instance_id"]
            before_row = con.execute(
                "SELECT remaining FROM coupon_instances WHERE instance_id=?",
                (instance_id,),
            ).fetchone()
            if before_row is None:
                return finish(
                    {
                        "status": REJECTED,
                        "code": errors.NOT_FOUND,
                        "issues": [
                            issue("unknown_coupon", instance_id=instance_id).as_dict()
                        ],
                        "deducted": False,
                    },
                    order["status"],
                )
            before[instance_id] = int(before_row["remaining"])

        refunded: list[dict[str, Any]] = []
        for use in uses:
            instance_id = use["instance_id"]
            used_count = int(use["used_count"])
            con.execute(
                "UPDATE coupon_instances SET remaining=remaining+? WHERE instance_id=?",
                (used_count, instance_id),
            )
            refunded.append(
                {
                    "instance_id": instance_id,
                    "used_count": used_count,
                    "remaining_before": before[instance_id],
                    "remaining_after": before[instance_id] + used_count,
                }
            )

        # ``cancellations`` keeps its original columns.  The frozen result
        # (including the exact refund record) lives in ``cancellation_results``
        # and is mirrored into ``request_results`` for request-id replay.
        con.execute(
            "INSERT INTO cancellations(cancel_id, order_id, request_json) VALUES(?,?,?)",
            (cancel_id, order_id, content_text),
        )
        con.execute(
            "UPDATE redemptions SET status='CANCELLED' WHERE order_id=?", (order_id,)
        )

        result = {
            "status": CANCELLED,
            "cancel_id": cancel_id,
            "order_id": order_id,
            "user_id": user_id,
            "refunded": refunded,
        }
        con.execute(
            "INSERT INTO cancellation_results(cancel_id, order_id, user_id, "
            "request_json, result_json) VALUES(?,?,?,?,?)",
            (cancel_id, order_id, user_id, content_text, jsonutil.dumps(result)),
        )
        _freeze(con, request_id, "cancel", content_text, result)
        return CancelResponse(request_id, False, True, result, "CANCELLED")


# ---------------------------------------------------------------------------
# read-only entry point (no deductions, no writes)
# ---------------------------------------------------------------------------


def preflight_redeem(
    con: sqlite3.Connection,
    user_id: str,
    lines: tuple[OrderLine, ...],
    coupon_ids: tuple[str, ...],
) -> Evaluation:
    """Evaluate a prospective redemption without touching the ledger.

    The pure rules live in :mod:`coupon_service.qualification`; this adapter
    adds instance ownership, catalog pinning and remaining counts so callers
    can distinguish qualification / combination / ownership / balance
    rejections.  It never writes.
    """

    qual_request, remaining, build_issues = _pin_coupons(
        con, user_id, lines, coupon_ids
    )
    evaluation = evaluate(qual_request)
    issues: list[Issue] = list(build_issues)
    issues.extend(evaluation.issues)
    if not issues:
        issues.extend(_balance_issues(coupon_ids, remaining))
    if not issues:
        return evaluation
    return Evaluation(ok=False, issues=tuple(issues), plan=None)


def current_balances(
    con: sqlite3.Connection, instance_ids: tuple[str, ...]
) -> dict[str, int | None]:
    result: dict[str, int | None] = {}
    for instance_id in instance_ids:
        row = con.execute(
            "SELECT remaining FROM coupon_instances WHERE instance_id=?",
            (instance_id,),
        ).fetchone()
        result[instance_id] = int(row["remaining"]) if row else None
    return result


def order_status(con: sqlite3.Connection, order_id: str) -> str | None:
    row = con.execute(
        "SELECT status FROM redemptions WHERE order_id=?", (order_id,)
    ).fetchone()
    return row["status"] if row else None


__all__ = [
    "redeem",
    "cancel",
    "preflight_redeem",
    "current_balances",
    "order_status",
    "RedeemResponse",
    "CancelResponse",
]
