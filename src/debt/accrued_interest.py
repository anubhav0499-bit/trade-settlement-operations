"""
Accrued interest and clean/dirty price conversion for debt instruments.

Day-count fraction is computed per the instrument's configured convention
(30/360, Actual/365, or Actual/Actual). Actual/Actual here uses the common
365.25-day-year approximation rather than full ICMA period-weighted
Actual/Actual, since the latter requires the surrounding coupon schedule
and is out of scope for this settlement system.

All computation is deterministic and rule-based — no LLM reasoning.
"""

from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from src.models.enums import DayCountConvention


def _thirty_360_days(start_date: date, end_date: date) -> int:
    d1 = 30 if start_date.day == 31 else start_date.day
    d2 = 30 if (end_date.day == 31 and d1 == 30) else end_date.day
    return (
        360 * (end_date.year - start_date.year)
        + 30 * (end_date.month - start_date.month)
        + (d2 - d1)
    )


def day_count_fraction(
    start_date: date, end_date: date, convention: DayCountConvention
) -> Decimal:
    """Fraction of a year between two dates under the given convention."""
    if convention == DayCountConvention.THIRTY_360:
        return Decimal(_thirty_360_days(start_date, end_date)) / Decimal("360")
    if convention == DayCountConvention.ACTUAL_365:
        return Decimal((end_date - start_date).days) / Decimal("365")
    if convention == DayCountConvention.ACTUAL_ACTUAL:
        return Decimal((end_date - start_date).days) / Decimal("365.25")
    raise ValueError(f"Unsupported day count convention: {convention}")


def compute_accrued_interest(
    face_value: Decimal,
    coupon_rate_pct: Decimal,
    last_coupon_date: date,
    settlement_date: date,
    day_count_convention: DayCountConvention,
) -> Decimal:
    """Accrued interest, per unit of face value, since the last coupon date."""
    fraction = day_count_fraction(
        last_coupon_date, settlement_date, day_count_convention
    )
    accrued = Decimal(str(face_value)) * Decimal(str(coupon_rate_pct)) / Decimal("100") * fraction
    return accrued.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def clean_to_dirty_price(
    clean_price: Decimal,
    face_value: Decimal,
    coupon_rate_pct: Decimal,
    last_coupon_date: date,
    settlement_date: date,
    day_count_convention: DayCountConvention,
) -> Decimal:
    accrued = compute_accrued_interest(
        face_value, coupon_rate_pct, last_coupon_date, settlement_date, day_count_convention
    )
    return (Decimal(str(clean_price)) + accrued).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def dirty_to_clean_price(
    dirty_price: Decimal,
    face_value: Decimal,
    coupon_rate_pct: Decimal,
    last_coupon_date: date,
    settlement_date: date,
    day_count_convention: DayCountConvention,
) -> Decimal:
    accrued = compute_accrued_interest(
        face_value, coupon_rate_pct, last_coupon_date, settlement_date, day_count_convention
    )
    return (Decimal(str(dirty_price)) - accrued).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
