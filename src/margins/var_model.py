"""
EWMA Volatility & VaR Margin Model (Equity Cash Segment).

NSE's cash-segment VaR margin is a 99% one-day Value-at-Risk computed from
an exponentially-weighted moving average (EWMA) of squared returns —
this gives recent price moves more weight than older ones, the standard
RiskMetrics approach.

All computation is deterministic and rule-based — no LLM reasoning.
"""

from decimal import Decimal, ROUND_HALF_UP

from src.utils.config_loader import get_margin_framework_config


def ewma_volatility(returns: list[Decimal], lambda_: Decimal | None = None) -> Decimal:
    """Daily EWMA volatility (standard deviation) from a chronological return series."""
    if not returns:
        return Decimal("0")

    config = get_margin_framework_config()["var_margin"]
    lam = lambda_ if lambda_ is not None else Decimal(str(config["ewma_lambda"]))

    variance = Decimal(str(returns[0])) ** 2
    for r in returns[1:]:
        variance = lam * variance + (Decimal("1") - lam) * Decimal(str(r)) ** 2

    return variance.sqrt()


def compute_var_margin(
    price: Decimal,
    volatility: Decimal,
    confidence_z: Decimal | None = None,
) -> Decimal:
    """99% one-day VaR margin = price * daily volatility * z-score."""
    config = get_margin_framework_config()["var_margin"]
    z = confidence_z if confidence_z is not None else Decimal(str(config["confidence_z"]))

    margin = Decimal(str(price)) * Decimal(str(volatility)) * z
    return margin.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
