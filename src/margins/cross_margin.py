"""
Cross Margin — benefit for hedged positions across correlated contracts
(e.g. long futures + short call, long futures + long put).

The benefit reduces the combined margin requirement of the two legs by a
configured percentage of the smaller leg's margin, reflecting that a
genuinely hedged book cannot lose both legs' worth simultaneously.

All computation is deterministic and rule-based — no LLM reasoning.
"""

from decimal import Decimal, ROUND_HALF_UP

from src.utils.config_loader import get_margin_framework_config


def compute_cross_margin_benefit(
    leg1_margin: Decimal,
    leg2_margin: Decimal,
    is_hedged: bool,
) -> Decimal:
    """Margin credit for a recognized hedge pair; zero if the legs aren't a recognized hedge."""
    if not is_hedged:
        return Decimal("0")

    config = get_margin_framework_config()["cross_margin"]
    benefit_pct = Decimal(str(config["hedge_benefit_pct"])) / Decimal("100")
    smaller_leg = min(Decimal(str(leg1_margin)), Decimal(str(leg2_margin)))
    return (smaller_leg * benefit_pct).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def apply_cross_margin(total_margin_before: Decimal, benefit: Decimal) -> Decimal:
    """Net margin after applying the cross-margin benefit, floored at zero."""
    return max(Decimal("0"), Decimal(str(total_margin_before)) - Decimal(str(benefit)))
