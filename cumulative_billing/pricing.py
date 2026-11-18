"""Pricing service: reads usage revisions and contracts, produces period fees.

Independent of the bill repository.  It answers, for one account-year:

* the confirmed cumulative window at each period (start, end);
* whether the chain up to a period is complete (no gaps);
* the marginal contribution per band and the *current* fee for each period.

Rules encoded here:

* Cumulative consumption is over the account's synthetic year, periods 1..12.
* A period's cumulative **end** is the cumulative end of the latest confirmed
  period at or before it; a period with no confirmed usage has no end and the
  chain is incomplete past it.
* ``F(x)`` prices ``[0, x)`` through all marginal bands.  A period's fee is
  ``F(end) - F(start)``.  Missing periods are **not** zero and are **not**
  treated as confirmed.
* Formal fees may only be produced when the chain from period 1 through the
  requested period is complete and monotone.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .contract import Contract
from .errors import IncompleteChainError, UsageError

MAX_PERIOD = 12


@dataclass(frozen=True)
class UsageRevision:
    account_id: str
    year: int
    period: int
    revision: int
    quantity: int

    def validate(self) -> None:
        if not self.account_id:
            raise UsageError("account_id must be non-empty")
        if not _is_int(self.year) or not (1 <= self.year <= 9999):
            raise UsageError("year must be an integer in 1..9999")
        if not _is_int(self.period) or not (1 <= self.period <= MAX_PERIOD):
            raise UsageError(f"period must be an integer in 1..{MAX_PERIOD}")
        if not _is_int(self.revision) or self.revision < 1:
            raise UsageError("revision must be a positive integer")
        if not _is_int(self.quantity) or self.quantity < 0:
            raise UsageError("quantity must be a non-negative integer")

    def to_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "year": self.year,
            "period": self.period,
            "revision": self.revision,
            "quantity": self.quantity,
        }


@dataclass(frozen=True)
class PeriodPosition:
    period: int
    confirmed: bool
    quantity: int | None            # confirmed quantity at this period, if any
    cumulative_start: int | None    # None when a prior period is unconfirmed
    cumulative_end: int | None      # None when this period is unconfirmed
    fee_cents: int | None           # None when not computable
    marginal: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class YearStatement:
    account_id: str
    year: int
    contract_id: str
    contract_revision: int
    periods: list[PeriodPosition]
    complete_through: int           # last period for which the chain is complete
    gap_periods: list[int]          # periods with no confirmed usage up to chain end

    def fee_at(self, period: int) -> int:
        pos = self.position(period)
        if pos.fee_cents is None:
            raise IncompleteChainError(
                f"no formal fee for {self.account_id}/{self.year}/{period}: "
                f"incomplete cumulative chain (complete through {self.complete_through})"
            )
        return pos.fee_cents

    def position(self, period: int) -> PeriodPosition:
        if not (1 <= period <= MAX_PERIOD):
            raise UsageError(f"period must be in 1..{MAX_PERIOD}")
        return self.periods[period - 1]

    def to_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "year": self.year,
            "contract_id": self.contract_id,
            "contract_revision": self.contract_revision,
            "complete_through": self.complete_through,
            "gap_periods": self.gap_periods,
            "periods": [
                {
                    "period": p.period,
                    "confirmed": p.confirmed,
                    "quantity": p.quantity,
                    "cumulative_start": p.cumulative_start,
                    "cumulative_end": p.cumulative_end,
                    "fee_cents": p.fee_cents,
                    "marginal": p.marginal,
                }
                for p in self.periods
            ],
        }


class PricingService:
    """Pure pricing over an explicit collection of confirmed usage revisions."""

    def __init__(self, contract: Contract):
        self.contract = contract

    def assess_year(
        self,
        account_id: str,
        year: int,
        revisions: Sequence[UsageRevision],
        *,
        through_period: int = MAX_PERIOD,
        period_revision: dict[int, int] | None = None,
    ) -> YearStatement:
        """Assess periods 1..through_period from the confirmed revisions.

        ``period_revision`` optionally pins a specific usage revision per
        period (used when a bill records ``usage_revisions``); when omitted the
        highest revision per period is used.
        """
        if not (1 <= through_period <= MAX_PERIOD):
            raise UsageError(f"through_period must be in 1..{MAX_PERIOD}")

        resolved = _resolve_revisions(account_id, year, revisions, period_revision)
        periods: list[PeriodPosition] = []
        running = 0
        chain_ok = True
        complete_through = 0

        for period in range(1, MAX_PERIOD + 1):
            quantity = resolved.get(period)
            confirmed = quantity is not None

            if not confirmed:
                chain_ok = False
                periods.append(
                    PeriodPosition(
                        period=period,
                        confirmed=False,
                        quantity=None,
                        cumulative_start=None,
                        cumulative_end=None,
                        fee_cents=None,
                    )
                )
                continue

            if chain_ok:
                # A confirmed period still sits on a complete chain.
                start = running
                end = running + quantity
                running = end
                complete_through = period
                fee = self.contract.period_fee(start, end)
                marginal = self.contract.marginal_breakdown(start, end)
            else:
                # Missing periods are not zero: cumulative positions past a
                # gap are undefined, so no formal fee or position is produced.
                start = None
                end = None
                fee = None
                marginal = []

            periods.append(
                PeriodPosition(
                    period=period,
                    confirmed=True,
                    quantity=quantity,
                    cumulative_start=start,
                    cumulative_end=end,
                    fee_cents=fee,
                    marginal=marginal,
                )
            )

        # Internal gaps only: a missing period before the last confirmed one.
        # Trailing unconfirmed periods are simply not yet reported, not gaps.
        max_confirmed = max(resolved) if resolved else 0
        reported_gaps = [p for p in range(1, max_confirmed + 1) if p not in resolved]
        return YearStatement(
            account_id=account_id,
            year=year,
            contract_id=self.contract.contract_id,
            contract_revision=self.contract.revision,
            periods=periods[:MAX_PERIOD],
            complete_through=complete_through,
            gap_periods=reported_gaps,
        )


def _resolve_revisions(
    account_id: str,
    year: int,
    revisions: Sequence[UsageRevision],
    period_revision: dict[int, int] | None,
) -> dict[int, int]:
    """Pick the confirmed quantity per period (highest revision by default)."""
    chosen: dict[int, tuple[int, int]] = {}
    seen: set[tuple[int, int]] = set()
    for rev in revisions:
        rev.validate()
        if rev.account_id != account_id or rev.year != year:
            continue
        key = (rev.period, rev.revision)
        if key in seen:
            raise UsageError(
                f"duplicate usage revision {account_id}/{year}/{rev.period}#{rev.revision}"
            )
        seen.add(key)
        if period_revision is not None and rev.period in period_revision:
            if rev.revision != period_revision[rev.period]:
                continue
        current = chosen.get(rev.period)
        if current is None or rev.revision > current[0]:
            chosen[rev.period] = (rev.revision, rev.quantity)
    return {period: qty for period, (_, qty) in chosen.items()}


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
