"""
IRD Government Bond Futures — delivery basket, conversion factors, and
cheapest-to-deliver (CTD) identification.

Conversion factor approximates the CBOT-style convention: the price (per unit
face value) at which a bond would yield the notional coupon rate if
discounted at that rate, using semi-annual compounding to the bond's
remaining whole coupon periods. This is a simplification — it ignores the
stub period to the next coupon date (full accrued-interest-aware conversion
factors require the bond's exact next coupon date), consistent with the
day-count approximations used elsewhere in this codebase.

Quoted bond prices are caller-supplied — there is no embedded pricing model.
All computation is deterministic and rule-based — no LLM reasoning.
"""

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP


@dataclass
class DeliverableBond:
    isin: str
    coupon_rate_pct: Decimal
    years_to_maturity: Decimal
    quoted_price: Decimal


def compute_conversion_factor(
    coupon_rate_pct: Decimal,
    years_to_maturity: Decimal,
    notional_coupon_pct: Decimal,
    coupon_frequency: int = 2,
) -> Decimal:
    """Price per unit face value (coupon_rate_pct bond) at a yield of notional_coupon_pct."""
    coupon = Decimal(str(coupon_rate_pct)) / Decimal("100") / coupon_frequency
    yield_per_period = Decimal(str(notional_coupon_pct)) / Decimal("100") / coupon_frequency
    n_periods = int((Decimal(str(years_to_maturity)) * coupon_frequency).to_integral_value())

    if n_periods <= 0:
        return Decimal("1.0000")

    if yield_per_period == 0:
        pv_coupons = coupon * n_periods
        pv_redemption = Decimal("1")
    else:
        discount = (Decimal("1") + yield_per_period) ** n_periods
        pv_coupons = coupon * (Decimal("1") - Decimal("1") / discount) / yield_per_period
        pv_redemption = Decimal("1") / discount

    cf = pv_coupons + pv_redemption
    return cf.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def compute_delivery_cost(
    quoted_price: Decimal, futures_settlement_price: Decimal, conversion_factor: Decimal
) -> Decimal:
    """Cost to a short of delivering this bond: quoted price minus what the long pays (futures price x CF)."""
    invoice_amount = Decimal(str(futures_settlement_price)) * Decimal(str(conversion_factor))
    cost = Decimal(str(quoted_price)) - invoice_amount
    return cost.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def build_delivery_basket(
    bonds: list[DeliverableBond],
    futures_settlement_price: Decimal,
    notional_coupon_pct: Decimal,
) -> list[dict]:
    basket = []
    for bond in bonds:
        cf = compute_conversion_factor(
            bond.coupon_rate_pct, bond.years_to_maturity, notional_coupon_pct
        )
        cost = compute_delivery_cost(bond.quoted_price, futures_settlement_price, cf)
        basket.append({
            "isin": bond.isin,
            "conversion_factor": cf,
            "delivery_cost": cost,
        })
    basket.sort(key=lambda b: b["delivery_cost"])
    return basket


def identify_cheapest_to_deliver(
    bonds: list[DeliverableBond],
    futures_settlement_price: Decimal,
    notional_coupon_pct: Decimal,
) -> dict:
    basket = build_delivery_basket(bonds, futures_settlement_price, notional_coupon_pct)
    if not basket:
        raise ValueError("Delivery basket is empty")
    return basket[0]
