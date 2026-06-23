"""
NSCCL-SPAN-style portfolio margin engine.

Generates the standard 16-scenario risk array (7 price-scan fractions x 2
volatility directions, plus 2 extreme scenarios at reduced weight), prices
each position under every scenario, and takes the worst-case portfolio loss
as the scenario-based margin. Adds the short option minimum floor and
calendar spread charge, then nets the value of long options as a credit.

This engine has no embedded option pricing model. Option deltas and vegas
must be supplied by the caller (e.g. from a separate pricing service) — the
same caller-supplied-price pattern used for DSP/FSP in mtm_engine.py. A
missing delta defaults to 1.0 (full directional exposure), which is
conservative for an unpriced option rather than silently ignoring its risk.

All computation is deterministic and rule-based — no LLM reasoning.
"""

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy.orm import Session

from src.models.database import DerivativeContract, DerivativePosition
from src.models.enums import BuySell, ContractType
from src.utils.config_loader import get_margin_framework_config


@dataclass
class SpanMarginResult:
    counterparty_id: str
    underlying: str
    scenario_margin: Decimal
    short_option_minimum: Decimal
    calendar_spread_charge: Decimal
    net_option_value: Decimal
    total_margin: Decimal
    worst_scenario_pnl: Decimal


def _risk_scenarios(config: dict) -> list[tuple[Decimal, int, Decimal]]:
    """Return (price_fraction, vol_direction, weight) for all 16 SPAN scenarios."""
    fractions = [Decimal("0"), Decimal("1") / 3, Decimal("2") / 3, Decimal("1")]
    scenarios = []
    for f in fractions[1:]:
        for sign in (1, -1):
            for vol_dir in (1, -1):
                scenarios.append((f * sign, vol_dir, Decimal("1")))
    # price-unchanged scenarios (vol up / vol down only)
    scenarios.append((Decimal("0"), 1, Decimal("1")))
    scenarios.append((Decimal("0"), -1, Decimal("1")))

    extreme_mult = Decimal(str(config["extreme_scenario_multiplier"]))
    extreme_weight = Decimal(str(config["extreme_scenario_weight_pct"])) / Decimal("100")
    scenarios.append((extreme_mult, 0, extreme_weight))
    scenarios.append((-extreme_mult, 0, extreme_weight))
    return scenarios


def compute_span_margin(
    session: Session,
    counterparty_id: str,
    underlying: str,
    underlying_price: Decimal,
    is_index: bool = False,
    delta_by_contract: dict[str, Decimal] | None = None,
    vega_by_contract: dict[str, Decimal] | None = None,
    option_value_by_contract: dict[str, Decimal] | None = None,
) -> SpanMarginResult:
    delta_by_contract = delta_by_contract or {}
    vega_by_contract = vega_by_contract or {}
    option_value_by_contract = option_value_by_contract or {}

    config = get_margin_framework_config()["span"]
    underlying_price = Decimal(str(underlying_price))
    price_scan_pct = Decimal(
        str(config["price_scan_range_pct"]["index" if is_index else "stock"])
    ) / Decimal("100")
    price_scan_range = underlying_price * price_scan_pct
    vol_scan_range = price_scan_range * Decimal(
        str(config["volatility_scan_range_pct_of_price_scan"])
    ) / Decimal("100")

    contracts = (
        session.query(DerivativeContract)
        .filter(DerivativeContract.underlying == underlying)
        .all()
    )
    contract_ids = [c.contract_id for c in contracts]
    positions = (
        session.query(DerivativePosition)
        .filter(
            DerivativePosition.counterparty_id == counterparty_id,
            DerivativePosition.contract_id.in_(contract_ids),
        )
        .all()
    )

    net_qty: dict[str, int] = {}
    for pos in positions:
        signed = pos.quantity if pos.buy_sell == BuySell.BUY else -pos.quantity
        net_qty[pos.contract_id] = net_qty.get(pos.contract_id, 0) + signed

    contracts_by_id = {c.contract_id: c for c in contracts}
    scenarios = _risk_scenarios(config)

    scenario_pnls = []
    for price_fraction, vol_dir, weight in scenarios:
        price_move = price_scan_range * price_fraction
        portfolio_pnl = Decimal("0")
        for contract_id, qty in net_qty.items():
            if qty == 0:
                continue
            contract = contracts_by_id[contract_id]
            delta = delta_by_contract.get(contract_id, Decimal("1"))
            vega = vega_by_contract.get(contract_id, Decimal("0"))
            pnl_per_lot = delta * price_move + vega * vol_dir * vol_scan_range
            portfolio_pnl += pnl_per_lot * qty * contract.lot_size * weight
        scenario_pnls.append(portfolio_pnl)

    worst_scenario_pnl = min(scenario_pnls) if scenario_pnls else Decimal("0")
    scenario_margin = max(Decimal("0"), -worst_scenario_pnl)

    short_option_pct = Decimal(str(config["short_option_minimum_pct"])) / Decimal("100")
    short_option_minimum = Decimal("0")
    net_option_value = Decimal("0")
    for contract_id, qty in net_qty.items():
        contract = contracts_by_id[contract_id]
        if contract.contract_type != ContractType.OPTIONS:
            continue
        if qty < 0:
            short_option_minimum += (
                short_option_pct * underlying_price * contract.lot_size * abs(qty)
            )
        elif qty > 0:
            value = option_value_by_contract.get(contract_id, Decimal("0"))
            net_option_value += value * contract.lot_size * qty

    expiries = {contracts_by_id[cid].expiry_date for cid, q in net_qty.items() if q != 0}
    calendar_spread_charge = Decimal("0")
    if len(expiries) > 1:
        long_lots = sum(q for q in net_qty.values() if q > 0)
        short_lots = sum(-q for q in net_qty.values() if q < 0)
        spread_lots = min(long_lots, short_lots)
        calendar_spread_charge = spread_lots * Decimal(str(config["calendar_spread_charge_per_lot"]))

    total_margin = max(scenario_margin, short_option_minimum) + calendar_spread_charge - net_option_value
    total_margin = max(Decimal("0"), total_margin)

    def quantize(d: Decimal) -> Decimal:
        return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    return SpanMarginResult(
        counterparty_id=counterparty_id,
        underlying=underlying,
        scenario_margin=quantize(scenario_margin),
        short_option_minimum=quantize(short_option_minimum),
        calendar_spread_charge=quantize(calendar_spread_charge),
        net_option_value=quantize(net_option_value),
        total_margin=quantize(total_margin),
        worst_scenario_pnl=quantize(worst_scenario_pnl),
    )
