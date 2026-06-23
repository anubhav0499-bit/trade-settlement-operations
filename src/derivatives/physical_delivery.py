"""
Physical Delivery Obligation Generator for Stock F&O.

Expiring stock futures and ITM/assigned stock options settle via delivery of
the underlying shares — the same NSDL/CDSL securities settlement equity cash
trades use. This module nets derivative positions into Obligation rows
(tagged product_segment=EQUITY_FO) so the existing matching, instruction, and
auction machinery can settle them without duplicating that logic.

All computation is deterministic and rule-based — no LLM reasoning.
"""

import uuid
from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy.orm import Session

from src.models.database import DerivativeContract, DerivativePosition, Obligation
from src.models.enums import (
    BuySell,
    ConfirmationStatus,
    CounterpartyType,
    Exchange,
    MatchStatus,
    NetDirection,
    ObligationStage,
    ObligationStatus,
    ProductSegment,
    SettlementCycle,
)
from src.derivatives.exercise_engine import AssignmentResult, ExerciseResult
from src.utils.config_loader import get_derivatives_settlement_config


def generate_futures_delivery_obligations(
    session: Session,
    contract: DerivativeContract,
    underlying_isin: str,
    fsp: Decimal,
    settlement_date: date,
) -> list[Obligation]:
    """Every open futures position at expiry converts to a delivery obligation, priced at FSP."""
    positions = (
        session.query(DerivativePosition)
        .filter(DerivativePosition.contract_id == contract.contract_id)
        .all()
    )

    net_qty: dict[str, int] = defaultdict(int)
    for pos in positions:
        signed = pos.quantity if pos.buy_sell == BuySell.BUY else -pos.quantity
        net_qty[pos.counterparty_id] += signed

    return _build_obligations(
        session, contract, underlying_isin, Decimal(str(fsp)), settlement_date, net_qty
    )


def generate_option_delivery_obligations(
    session: Session,
    contract: DerivativeContract,
    underlying_isin: str,
    exercise_results: list[ExerciseResult],
    assignment_results: list[AssignmentResult],
    settlement_date: date,
) -> list[Obligation]:
    """ITM-exercised longs receive shares; assigned shorts deliver — both at strike price."""
    strike = Decimal(str(contract.strike_price))

    net_qty: dict[str, int] = defaultdict(int)
    for r in exercise_results:
        if r.exercised_quantity:
            net_qty[r.counterparty_id] += r.exercised_quantity
    for a in assignment_results:
        net_qty[a.counterparty_id] -= a.assigned_quantity

    return _build_obligations(
        session, contract, underlying_isin, strike, settlement_date, net_qty
    )


def _build_obligations(
    session: Session,
    contract: DerivativeContract,
    underlying_isin: str,
    price: Decimal,
    settlement_date: date,
    net_qty_by_counterparty: dict[str, int],
) -> list[Obligation]:
    obligations = []
    for counterparty_id, net in net_qty_by_counterparty.items():
        if net == 0:
            continue

        quantity = abs(net) * contract.lot_size
        direction = NetDirection.PAY_OUT if net > 0 else NetDirection.PAY_IN
        net_value = (price * quantity).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        obligation = Obligation(
            obligation_id=str(uuid.uuid4()),
            isin=underlying_isin,
            security_name=contract.underlying,
            net_quantity=quantity,
            net_direction=direction,
            vwap_price=price,
            net_value=net_value,
            settlement_date=settlement_date,
            settlement_cycle=SettlementCycle.T1,
            counterparty_id=counterparty_id,
            counterparty_type=CounterpartyType.BROKER,
            exchange=Exchange.NSE,
            obligation_stage=ObligationStage.FINAL,
            product_segment=ProductSegment.EQUITY_FO,
            status=ObligationStatus.PENDING,
            match_status=MatchStatus.UNMATCHED,
            confirmation_status=ConfirmationStatus.NOT_REQUIRED,
            source_trade_ids=f'["DERIV-{contract.contract_id}"]',
        )
        obligations.append(obligation)
        session.add(obligation)

    session.commit()
    return obligations


def compute_delivery_margin(
    expiry_date: date,
    current_date: date,
    notional_value: Decimal,
) -> Decimal:
    """Incremental delivery margin, ramping linearly from E-N days to 0 days before expiry."""
    config = get_derivatives_settlement_config()["delivery_margin"]
    ramp_days = config["ramp_start_days_before_expiry"]
    max_pct = Decimal(str(config["max_margin_pct"])) / Decimal("100")

    days_to_expiry = (expiry_date - current_date).days
    if days_to_expiry > ramp_days or days_to_expiry < 0:
        return Decimal("0")

    progress = Decimal(ramp_days - days_to_expiry) / Decimal(ramp_days)
    pct = max_pct * progress
    return (Decimal(str(notional_value)) * pct).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def get_delivery_summary(obligations: list[Obligation]) -> dict:
    if not obligations:
        return {"total": 0, "pay_in": 0, "pay_out": 0, "total_value": "0"}
    pay_in = sum(1 for o in obligations if o.net_direction == NetDirection.PAY_IN)
    pay_out = sum(1 for o in obligations if o.net_direction == NetDirection.PAY_OUT)
    total_value = sum(Decimal(str(o.net_value)) for o in obligations)
    return {
        "total": len(obligations),
        "pay_in": pay_in,
        "pay_out": pay_out,
        "total_value": str(total_value),
    }
