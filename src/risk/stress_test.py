"""
Portfolio Stress Testing — worst-case loss scenarios for clearing members'
open derivative positions, used to identify the top-N most exposed CMs.

Stress loss is computed by shocking each position's reference price by a
caller-supplied percentage in the adverse direction (down for longs, up for
shorts) and revaluing the position. Reference prices and margin held are
caller-supplied — there is no embedded pricing model.

All computation is deterministic and rule-based — no LLM reasoning.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy.orm import Session

from src.models.database import DerivativePosition
from src.models.enums import BuySell


@dataclass
class StressResult:
    counterparty_id: str
    stress_loss: Decimal
    margin_held: Decimal
    shortfall: Decimal


def compute_position_stress_loss(
    position: DerivativePosition, reference_price: Decimal, shock_pct: Decimal
) -> Decimal:
    """Loss if the reference price moves against this position by shock_pct."""
    price = Decimal(str(reference_price))
    shock = Decimal(str(shock_pct)) / Decimal("100")
    shocked_price = price * (Decimal("1") - shock) if position.buy_sell == BuySell.BUY else price * (
        Decimal("1") + shock
    )
    pnl = (shocked_price - price) * position.quantity if position.buy_sell == BuySell.BUY else (
        price - shocked_price
    ) * position.quantity
    loss = -pnl if pnl < 0 else Decimal("0")
    return loss.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def compute_portfolio_stress_loss(
    session: Session,
    counterparty_id: str,
    as_of_date: date,
    shock_pct: Decimal,
    reference_prices: dict[str, Decimal],
) -> Decimal:
    """Total stress loss across a counterparty's open positions as of a date."""
    positions = (
        session.query(DerivativePosition)
        .filter(
            DerivativePosition.counterparty_id == counterparty_id,
            DerivativePosition.position_date == as_of_date,
        )
        .all()
    )
    total = Decimal("0")
    for pos in positions:
        if pos.contract_id not in reference_prices:
            continue
        total += compute_position_stress_loss(pos, reference_prices[pos.contract_id], shock_pct)
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def rank_top_n_stressed_cms(
    session: Session,
    counterparty_ids: list[str],
    as_of_date: date,
    shock_pct: Decimal,
    reference_prices: dict[str, Decimal],
    margin_held: dict[str, Decimal],
    top_n: int,
) -> list[StressResult]:
    results = []
    for cm_id in counterparty_ids:
        stress_loss = compute_portfolio_stress_loss(
            session, cm_id, as_of_date, shock_pct, reference_prices
        )
        held = Decimal(str(margin_held.get(cm_id, Decimal("0"))))
        shortfall = (stress_loss - held).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if shortfall < 0:
            shortfall = Decimal("0")
        results.append(StressResult(cm_id, stress_loss, held, shortfall))

    results.sort(key=lambda r: r.shortfall, reverse=True)
    return results[:top_n]


def get_stress_summary(results: list[StressResult]) -> dict:
    if not results:
        return {"total_stress_loss": Decimal("0"), "total_shortfall": Decimal("0"), "cms_with_shortfall": 0}
    return {
        "total_stress_loss": sum((r.stress_loss for r in results), Decimal("0")),
        "total_shortfall": sum((r.shortfall for r in results), Decimal("0")),
        "cms_with_shortfall": sum(1 for r in results if r.shortfall > 0),
    }
