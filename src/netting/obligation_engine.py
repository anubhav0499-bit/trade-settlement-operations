"""
Netting & Obligation Engine (§2).

Computes multilateral net obligations at the (ISIN, counterparty, settlement_date)
grain from raw trades. Produces VWAP-priced net obligations in two stages:
PROVISIONAL (end of T day) and FINAL (morning of settlement day).

Design note: NSE Clearing computes net fund obligations as the sum of individual
trade values (not VWAP × net quantity). We use VWAP as a modeling simplification
to make the matching engine cleaner. This is documented in the methodology README.
"""

import json
import uuid
from collections import defaultdict
from datetime import datetime, date
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import and_
from sqlalchemy.orm import Session

from src.models.database import Trade, Obligation, get_session
from src.models.enums import (
    BuySell,
    ConfirmationStatus,
    CounterpartyType,
    MatchStatus,
    NetDirection,
    ObligationStage,
    ObligationStatus,
    SourceSystem,
)


# Netting key: (ISIN, counterparty_id, settlement_date, exchange, source_system)
NettingKey = tuple[str, str, date, str, str]


def _compute_vwap(trades: list[Trade]) -> Decimal:
    """Volume-weighted average price from a list of trades."""
    total_value = sum(Decimal(str(t.price)) * t.quantity for t in trades)
    total_qty = sum(t.quantity for t in trades)
    if total_qty == 0:
        return Decimal("0")
    return (total_value / total_qty).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _net_trades(trades: list[Trade]) -> tuple[int, NetDirection, list[str]]:
    """Net a group of trades into a single direction and quantity.

    Returns (net_quantity, net_direction, source_trade_ids).
    """
    buy_qty = sum(t.quantity for t in trades if t.buy_sell == BuySell.BUY)
    sell_qty = sum(t.quantity for t in trades if t.buy_sell == BuySell.SELL)
    net = buy_qty - sell_qty

    if net >= 0:
        direction = NetDirection.PAY_OUT  # net buyer → receives securities (pay out funds)
    else:
        direction = NetDirection.PAY_IN   # net seller → delivers securities (pay in)

    trade_ids = [t.trade_id for t in trades]
    return abs(net), direction, trade_ids


def compute_obligations(
    session: Session,
    source_system: SourceSystem,
    stage: ObligationStage,
    as_of: datetime | None = None,
) -> list[Obligation]:
    """Compute net obligations from trades of a given source system.

    Groups by (ISIN, counterparty_id, settlement_date, exchange) and nets
    buy vs sell quantities into a single obligation per group.

    Args:
        session: DB session
        source_system: Which source to compute obligations from
        stage: PROVISIONAL or FINAL
        as_of: Timestamp for the computation (defaults to now)
    """
    if as_of is None:
        as_of = datetime.utcnow()

    trades = (
        session.query(Trade)
        .filter(Trade.source_system == source_system)
        .all()
    )

    # Group by netting key
    groups: dict[tuple, list[Trade]] = defaultdict(list)
    for t in trades:
        key = (t.isin, t.counterparty_id, t.settlement_date, t.exchange.value)
        groups[key].append(t)

    obligations = []
    for (isin, cp_id, settle_date, exchange), group_trades in groups.items():
        net_qty, direction, trade_ids = _net_trades(group_trades)

        if net_qty == 0:
            continue

        vwap = _compute_vwap(group_trades)
        net_value = (vwap * net_qty).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        first_trade = group_trades[0]

        # Custodian-facing obligations need confirmation
        needs_confirmation = first_trade.counterparty_type == CounterpartyType.CUSTODIAN
        conf_status = (
            ConfirmationStatus.PENDING if needs_confirmation
            else ConfirmationStatus.NOT_REQUIRED
        )

        obligation = Obligation(
            obligation_id=str(uuid.uuid4()),
            isin=isin,
            security_name=first_trade.security_name,
            net_quantity=net_qty,
            net_direction=direction,
            vwap_price=vwap,
            net_value=net_value,
            settlement_date=settle_date,
            settlement_cycle=first_trade.settlement_cycle,
            counterparty_id=cp_id,
            counterparty_type=first_trade.counterparty_type,
            exchange=first_trade.exchange,
            obligation_stage=stage,
            status=ObligationStatus.PENDING,
            match_status=MatchStatus.UNMATCHED,
            confirmation_status=conf_status,
            computed_at=as_of,
            source_trade_ids=json.dumps(trade_ids),
        )
        obligations.append(obligation)

    return obligations


def compute_all_obligations(session: Session) -> dict[str, list[Obligation]]:
    """Compute obligations from all three source systems.

    Returns a dict keyed by source system name with lists of obligations.
    OMS obligations are the 'internal' view; broker and custodian are
    the 'counterparty' views used for matching.
    """
    results = {}

    for source, stage_label in [
        (SourceSystem.OMS, "internal"),
        (SourceSystem.BROKER_CONFIRM, "broker"),
        (SourceSystem.CUSTODIAN_STATEMENT, "custodian"),
    ]:
        # Compute provisional obligations (end of T day)
        provisional = compute_obligations(
            session, source, ObligationStage.PROVISIONAL
        )
        for ob in provisional:
            session.add(ob)

        # Compute final obligations (morning of settlement day)
        final = compute_obligations(
            session, source, ObligationStage.FINAL
        )
        for ob in final:
            session.add(ob)

        results[stage_label] = provisional + final
        print(f"  {stage_label}: {len(provisional)} provisional, {len(final)} final obligations")

    session.commit()
    return results


def get_obligations_for_matching(
    session: Session,
    stage: ObligationStage = ObligationStage.FINAL,
) -> tuple[list[Obligation], list[Obligation], list[Obligation]]:
    """Retrieve obligations split by source for the matching engine.

    Returns (internal_obligations, broker_obligations, custodian_obligations),
    all at the specified stage.
    """
    def _query_by_source(source_prefix: str) -> list[Obligation]:
        """Get obligations whose source_trade_ids contain trades from the given source."""
        all_obs = (
            session.query(Obligation)
            .filter(Obligation.obligation_stage == stage)
            .all()
        )
        return [
            ob for ob in all_obs
            if any(tid.startswith(source_prefix) for tid in ob.get_source_trade_ids())
            or (source_prefix == "" and not any(
                tid.startswith(p) for p in ("BRK-", "CUS-")
                for tid in ob.get_source_trade_ids()
            ))
        ]

    internal = _query_by_source("")       # OMS trades (no prefix)
    broker = _query_by_source("BRK-")     # Broker-sourced
    custodian = _query_by_source("CUS-")  # Custodian-sourced

    return internal, broker, custodian
