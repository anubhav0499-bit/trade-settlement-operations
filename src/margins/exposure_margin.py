"""
Exposure Margin — additional buffer levied on top of SPAN margin.

Index derivatives: fixed percentage of notional. Stock derivatives: the
higher of a fixed percentage or a multiple of the stock's volatility,
per NSE Clearing's published methodology.

All computation is deterministic and rule-based — no LLM reasoning.
"""

from decimal import Decimal, ROUND_HALF_UP

from src.utils.config_loader import get_margin_framework_config


def compute_exposure_margin(
    underlying_price: Decimal,
    lot_size: int,
    net_quantity_lots: int,
    is_index: bool,
    std_dev_pct: Decimal | None = None,
) -> Decimal:
    """Notional-based exposure margin for a net position in one contract."""
    if net_quantity_lots == 0:
        return Decimal("0")

    config = get_margin_framework_config()["exposure_margin"]
    underlying_price = Decimal(str(underlying_price))
    notional = underlying_price * lot_size * abs(net_quantity_lots)

    if is_index:
        pct = Decimal(str(config["index_pct"])) / Decimal("100")
    else:
        fixed_pct = Decimal(str(config["stock_pct"])) / Decimal("100")
        if std_dev_pct is not None:
            vol_pct = (
                Decimal(str(std_dev_pct))
                / Decimal("100")
                * Decimal(str(config["stock_min_std_dev_multiplier"]))
            )
            pct = max(fixed_pct, vol_pct)
        else:
            pct = fixed_pct

    return (notional * pct).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
