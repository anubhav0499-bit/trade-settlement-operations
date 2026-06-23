"""
T+0 Parallel Settlement Path (Equity Cash).

T+0 is an optional same-day settlement cycle (top 500 stocks) that runs
alongside the mandatory T+1 cycle. Trades must be reported before a trade
cutoff and net obligations finalized before an obligation cutoff, both on
trade day itself — unlike T+1, there's no separate provisional/final stage
split across two days.

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


def compute_t0_obligations(
    session: Session, as_of: datetime | None = None
) -> list[Obligation]:
    """Net T0-cycle trades into same-day FINAL obligations.

    T+0 has no provisional stage — pay-in/pay-out happen same day, so every
    netted obligation is generated directly at FINAL stage.
    """
    if as_of is None:
        as_of = datetime.utcnow()

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


def get_t0_summary(obligations: list[Obligation]) -> dict:
    if not obligations:
        return {"total": 0, "total_value": Decimal("0")}
    total_value = sum((Decimal(str(o.net_value)) for o in obligations), Decimal("0"))
    return {"total": len(obligations), "total_value": total_value}
