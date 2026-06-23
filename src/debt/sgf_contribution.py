"""
Settlement Guarantee Fund issuer contribution for debt instruments —
a configured basis-point rate of issuance value, per annum, scaled by
the instrument's tenor to maturity.

All computation is deterministic and rule-based — no LLM reasoning.
"""

from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from src.utils.config_loader import get_debt_settlement_config


def compute_sgf_issuer_contribution(
    issuance_value: Decimal, issue_date: date, maturity_date: date
) -> Decimal:
    config = get_debt_settlement_config()["sgf_contribution"]
    bps = Decimal(str(config["issuer_bps_per_annum"]))
    annual_rate = bps / Decimal("10000")

    years_to_maturity = Decimal((maturity_date - issue_date).days) / Decimal("365.25")
    contribution = Decimal(str(issuance_value)) * annual_rate * years_to_maturity
    return contribution.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
