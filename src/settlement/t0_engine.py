"""
T+0 Parallel Settlement Path (Equity Cash).

T+0 is an optional same-day settlement cycle (up to 500 stocks, per NSE's
live Feb 2026 rollout) that runs alongside the mandatory T+1 cycle, with
three sequential intraday cutoffs on trade day itself:

1. trade_cutoff       — trades reported after this miss the T0 window
2. obligation_cutoff   — net obligations must be determined before this
3. funds_settlement_cutoff — funds/securities must move before this, or
                              the obligation fails (same-day, no T+1 grace)

ISIN eligibility (which securities are in the current T0 tier) is
caller-supplied, not embedded here — same caller-supplied-data convention
used for settlement prices in mtm_engine.py — since eligibility is an
exchange-published list, not a policy percentage.

Reuses the netting/VWAP logic from `src.netting.obligation_engine` rather
than duplicating it, filtering to T0-cycle trades only.

All computation is deterministic and rule-based — no LLM reasoning.
"""

import json
import uuid
from collections import defaultdict
from datetime import datetime, time
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy.orm import Session

from src.models.database import Obligation, Trade
from src.models.enums import (
    ConfirmationStatus,
    MatchStatus,
    ObligationStage,
    ObligationStatus,
    SettlementCycle,
)
from src.netting.obligation_engine import _compute_vwap, _net_trades
from src.utils.clock import utcnow
from src.utils.config_loader import get_t0_settlement_config


def _parse_cutoff(cutoff_str: str) -> time:
    hour, minute = cutoff_str.split(":")
    return time(int(hour), int(minute))


def is_trade_eligible_for_t0(trade_time: time) -> bool:
    config = get_t0_settlement_config()
    cutoff = _parse_cutoff(config["trade_cutoff"])
    return trade_time <= cutoff


def is_within_obligation_cutoff(current_time: time) -> bool:
    config = get_t0_settlement_config()
    cutoff = _parse_cutoff(config["obligation_cutoff"])
    return current_time <= cutoff


def is_within_funds_settlement_cutoff(current_time: time) -> bool:
    config = get_t0_settlement_config()
    cutoff = _parse_cutoff(config["funds_settlement_cutoff"])
    return current_time <= cutoff


def partition_t0_eligible_trades(
    trades: list[Trade],
    eligible_isins: set[str] | None = None,
    trade_times: dict[str, time] | None = None,
) -> tuple[list[Trade], list[Trade]]:
    """Split T0-tagged trades into (eligible, redirect_to_t1).

    A trade is redirected — not dropped — when its ISIN isn't in the current
    T0 tier, or when its trade_time is past the trade cutoff. Both checks
    are optional: a None eligible_isins or trade_times skips that check
    entirely, so a caller that doesn't have one of these inputs yet still
    gets the other enforced.
    """
    eligible, redirected = [], []
    for t in trades:
        isin_ok = eligible_isins is None or t.isin in eligible_isins
        time_ok = (
            trade_times is None
            or t.trade_id not in trade_times
            or is_trade_eligible_for_t0(trade_times[t.trade_id])
        )
        (eligible if isin_ok and time_ok else redirected).append(t)
    return eligible, redirected


def compute_t0_obligations(
    session: Session, as_of: datetime | None = None, current_time: time | None = None,
) -> list[Obligation]:
    """Net T0-cycle trades into same-day FINAL obligations.

    T+0 has no provisional stage — pay-in/pay-out happen same day, so every
    netted obligation is generated directly at FINAL stage.

    If current_time is supplied and the obligation cutoff has already
    passed, the window is closed for the day: returns [] rather than netting
    late — callers should have already redirected eligible trades to T+1
    via partition_t0_eligible_trades before this point in the day.
    """
    if as_of is None:
        as_of = utcnow()
    if current_time is not None and not is_within_obligation_cutoff(current_time):
        return []

    trades = (
        session.query(Trade).filter(Trade.settlement_cycle == SettlementCycle.T0).all()
    )

    groups: dict[tuple, list[Trade]] = defaultdict(list)
    for t in trades:
        key = (t.isin, t.counterparty_id, t.settlement_date, t.exchange.value)
        groups[key].append(t)

    obligations = []
    for (isin, cp_id, settle_date, _exchange), group_trades in groups.items():
        net_qty, direction, trade_ids = _net_trades(group_trades)
        if net_qty == 0:
            continue

        vwap = _compute_vwap(group_trades)
        net_value = (vwap * net_qty).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        first_trade = group_trades[0]

        obligation = Obligation(
            obligation_id=str(uuid.uuid4()),
            isin=isin,
            security_name=first_trade.security_name,
            net_quantity=net_qty,
            net_direction=direction,
            vwap_price=vwap,
            net_value=net_value,
            settlement_date=settle_date,
            settlement_cycle=SettlementCycle.T0,
            counterparty_id=cp_id,
            counterparty_type=first_trade.counterparty_type,
            exchange=first_trade.exchange,
            obligation_stage=ObligationStage.FINAL,
            product_segment=first_trade.product_segment,
            status=ObligationStatus.PENDING,
            match_status=MatchStatus.UNMATCHED,
            confirmation_status=ConfirmationStatus.NOT_REQUIRED,
            computed_at=as_of,
            source_trade_ids=json.dumps(trade_ids),
        )
        obligations.append(obligation)
        session.add(obligation)

    session.commit()
    return obligations


def settle_t0_funds(obligations: list[Obligation], current_time: time) -> list[Obligation]:
    """Final same-day leg: funds/securities must move before the funds
    settlement cutoff, or the obligation fails outright — T0 has no
    next-day grace period the way a T+1 fail would (see breaks/auction)."""
    new_status = (
        ObligationStatus.SETTLED if is_within_funds_settlement_cutoff(current_time)
        else ObligationStatus.FAILED
    )
    for ob in obligations:
        ob.status = new_status
    return obligations


def get_t0_summary(obligations: list[Obligation]) -> dict:
    if not obligations:
        return {"total": 0, "total_value": Decimal("0"), "settled": 0, "failed": 0}
    total_value = sum((Decimal(str(o.net_value)) for o in obligations), Decimal("0"))
    settled = sum(1 for o in obligations if o.status == ObligationStatus.SETTLED)
    failed = sum(1 for o in obligations if o.status == ObligationStatus.FAILED)
    return {
        "total": len(obligations), "total_value": total_value,
        "settled": settled, "failed": failed,
    }
