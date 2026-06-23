"""
Portfolio Stress Testing — worst-case loss scenarios for clearing members'
open derivative positions, used to identify the top-N most exposed CMs.

Stress loss is computed by shocking each position's reference price by a
caller-supplied percentage in the adverse direction (down for longs, up for
shorts) and revaluing the position. Reference prices and margin held are
caller-supplied — there is no embedded pricing model.

rank_top_n_stressed_cms ranks CMs independently — it can't see that several
CMs losing money simultaneously under the SAME price shock (because they're
all exposed to the same underlying) is a different, more systemic risk than
the same total loss spread across unrelated underlyings: a single adverse
move stresses the SGF all at once rather than in an uncorrelated trickle.
identify_contagion_clusters below adds that cross-CM view, grouping stress
loss by underlying rather than by counterparty.

All computation is deterministic and rule-based — no LLM reasoning.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy.orm import Session

from src.models.database import DerivativeContract, DerivativePosition
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


@dataclass
class ContagionCluster:
    underlying: str
    affected_cm_ids: list[str]
    total_stress_loss: Decimal
    share_of_total_stress_loss_pct: Decimal


def identify_contagion_clusters(
    session: Session,
    counterparty_ids: list[str],
    as_of_date: date,
    shock_pct: Decimal,
    reference_prices: dict[str, Decimal],
    min_cms: int = 2,
) -> list[ContagionCluster]:
    """Group stress loss by underlying instead of by counterparty, to surface
    underlyings where a single price shock would hit min_cms or more CMs at
    once — a concentration of correlated exposure the per-CM ranking in
    rank_top_n_stressed_cms can't see, since it only sums each CM's own
    portfolio across whatever underlyings they happen to hold.

    A CM with positions in multiple contracts on the same underlying (e.g. a
    future and an option) is one CM in that underlying's affected_cm_ids,
    not counted twice — contagion is about the breadth of distinct
    counterparties exposed, not position count.
    """
    positions = (
        session.query(DerivativePosition)
        .filter(
            DerivativePosition.counterparty_id.in_(counterparty_ids),
            DerivativePosition.position_date == as_of_date,
        )
        .all()
    )
    if not positions:
        return []

    contract_ids = {p.contract_id for p in positions}
    underlying_by_contract = {
        c.contract_id: c.underlying
        for c in session.query(DerivativeContract).filter(DerivativeContract.contract_id.in_(contract_ids)).all()
    }

    loss_by_underlying: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    cms_by_underlying: dict[str, set[str]] = defaultdict(set)
    for pos in positions:
        underlying = underlying_by_contract.get(pos.contract_id)
        if underlying is None or pos.contract_id not in reference_prices:
            continue
        loss = compute_position_stress_loss(pos, reference_prices[pos.contract_id], shock_pct)
        if loss > 0:
            loss_by_underlying[underlying] += loss
            cms_by_underlying[underlying].add(pos.counterparty_id)

    grand_total = sum(loss_by_underlying.values(), Decimal("0"))

    clusters = []
    for underlying, cm_set in cms_by_underlying.items():
        if len(cm_set) < min_cms:
            continue
        total_loss = loss_by_underlying[underlying].quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        share = (
            (total_loss / grand_total * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if grand_total > 0 else Decimal("0")
        )
        clusters.append(ContagionCluster(
            underlying=underlying,
            affected_cm_ids=sorted(cm_set),
            total_stress_loss=total_loss,
            share_of_total_stress_loss_pct=share,
        ))

    clusters.sort(key=lambda c: c.total_stress_loss, reverse=True)
    return clusters


def get_contagion_summary(clusters: list[ContagionCluster]) -> dict:
    if not clusters:
        return {"cluster_count": 0, "largest_cluster_underlying": None, "largest_cluster_loss": Decimal("0")}
    largest = clusters[0]
    return {
        "cluster_count": len(clusters),
        "largest_cluster_underlying": largest.underlying,
        "largest_cluster_loss": largest.total_stress_loss,
        "largest_cluster_cm_count": len(largest.affected_cm_ids),
    }
