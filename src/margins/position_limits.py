"""
Position Limits — market-wide, clearing-member-level, and client-level
open-interest checks for F&O / currency / IRD segments.

Market-wide limit is a percentage of free float (or other underlying
notional measure). CM-level and client-level limits are then expressed as a
percentage of that market-wide limit, per NSE's tiered approach.

All computation is deterministic and rule-based — no LLM reasoning.
"""

from dataclasses import dataclass
from decimal import Decimal

from src.utils.config_loader import get_margin_framework_config


@dataclass
class PositionLimitViolation:
    level: str
    entity_id: str
    limit: int
    actual: int


def market_wide_limit(free_float_lots: int) -> int:
    config = get_margin_framework_config()["position_limits"]
    pct = Decimal(str(config["market_wide_pct_of_free_float"])) / Decimal("100")
    return int(Decimal(free_float_lots) * pct)


def check_market_wide_limit(open_interest_lots: int, free_float_lots: int) -> PositionLimitViolation | None:
    limit = market_wide_limit(free_float_lots)
    if open_interest_lots > limit:
        return PositionLimitViolation("MARKET_WIDE", "MARKET", limit, open_interest_lots)
    return None


def check_cm_level_limit(
    cm_id: str, cm_open_interest_lots: int, free_float_lots: int
) -> PositionLimitViolation | None:
    config = get_margin_framework_config()["position_limits"]
    pct = Decimal(str(config["cm_level_pct_of_market_wide"])) / Decimal("100")
    limit = int(Decimal(market_wide_limit(free_float_lots)) * pct)
    if cm_open_interest_lots > limit:
        return PositionLimitViolation("CM_LEVEL", cm_id, limit, cm_open_interest_lots)
    return None


def check_client_level_limit(
    client_id: str, client_open_interest_lots: int, free_float_lots: int
) -> PositionLimitViolation | None:
    config = get_margin_framework_config()["position_limits"]
    pct = Decimal(str(config["client_level_pct_of_market_wide"])) / Decimal("100")
    limit = int(Decimal(market_wide_limit(free_float_lots)) * pct)
    if client_open_interest_lots > limit:
        return PositionLimitViolation("CLIENT_LEVEL", client_id, limit, client_open_interest_lots)
    return None
