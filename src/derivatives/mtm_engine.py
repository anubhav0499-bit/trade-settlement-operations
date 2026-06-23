"""
Daily Mark-to-Market Settlement Engine for derivatives (Equity F&O, Currency, IRD).

Computes per-position P/L versus the prior settlement price (or trade price
for day-1 positions), then nets it to a single funds obligation per
counterparty. Settlement prices are supplied by the caller — DSP for equity
F&O, RBI reference rate for currency derivatives, MIBOR/OIS-derived price for
IRD — this engine does not source market data itself.

All computation is deterministic and rule-based — no LLM reasoning.
"""

import uuid
from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy.orm import Session

from src.models.database import DerivativeContract, DerivativePosition, MTMSettlement
from src.models.enums import BuySell


def _prior_settlement_price(
    session: Session,
    contract_id: str,
    counterparty_id: str,
    before_date: date,
) -> Decimal | None:
    prior = (
        session.query(MTMSettlement)
        .filter(
            MTMSettlement.contract_id == contract_id,
            MTMSettlement.counterparty_id == counterparty_id,
            MTMSettlement.settlement_date < before_date,
        )
        .order_by(MTMSettlement.settlement_date.desc())
        .first()
    )
    return Decimal(str(prior.settlement_price)) if prior else None


def compute_daily_mtm(
    session: Session,
    settlement_date: date,
    settlement_prices: dict[str, Decimal],
) -> list[MTMSettlement]:
    """Compute MTM settlement for every open position in the given contracts.

    Args:
        settlement_prices: contract_id -> daily settlement price (DSP / reference rate)
    """
    if not settlement_prices:
        return []

    positions = (
        session.query(DerivativePosition)
        .filter(DerivativePosition.contract_id.in_(settlement_prices.keys()))
        .all()
    )
    contracts = {
        c.contract_id: c
        for c in session.query(DerivativeContract)
        .filter(DerivativeContract.contract_id.in_(settlement_prices.keys()))
        .all()
    }

    records = []
    for pos in positions:
        contract = contracts.get(pos.contract_id)
        if contract is None:
            continue

        dsp = Decimal(str(settlement_prices[pos.contract_id]))
        prior_price = _prior_settlement_price(
            session, pos.contract_id, pos.counterparty_id, settlement_date
        )
        reference_price = (
            prior_price if prior_price is not None else Decimal(str(pos.trade_price))
        )

        price_diff = dsp - reference_price
        if pos.buy_sell == BuySell.SELL:
            price_diff = -price_diff

        mtm_amount = (price_diff * pos.quantity * contract.lot_size).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

        record = MTMSettlement(
            mtm_id=str(uuid.uuid4()),
            contract_id=pos.contract_id,
            counterparty_id=pos.counterparty_id,
            settlement_date=settlement_date,
            settlement_price=dsp,
            mtm_amount=mtm_amount,
        )
        records.append(record)
        session.add(record)

    session.commit()
    return records


def net_mtm_by_counterparty(records: list[MTMSettlement]) -> dict[str, Decimal]:
    """Net MTM cash flows per counterparty — positive = receivable, negative = payable."""
    net: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for r in records:
        net[r.counterparty_id] += Decimal(str(r.mtm_amount))
    return dict(net)


def get_mtm_summary(records: list[MTMSettlement]) -> dict:
    if not records:
        return {"total_positions": 0, "total_pnl": "0", "net_by_counterparty": {}}
    net = net_mtm_by_counterparty(records)
    total_pnl = sum(Decimal(str(r.mtm_amount)) for r in records)
    return {
        "total_positions": len(records),
        "total_pnl": str(total_pnl),
        "net_by_counterparty": {k: str(v) for k, v in net.items()},
    }
