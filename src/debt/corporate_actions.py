"""
Corporate action cash-flow computations for debt instruments: coupon
payments, maturity redemption, and call/put exercise.

These are pure computation functions, not a corporate-actions processing
pipeline — applying the result to positions/obligations is the caller's
responsibility. Exercise price for call/put is supplied by the caller
(there is no embedded bond pricing model), matching the caller-supplied
data pattern used in the derivatives and margin modules.

All computation is deterministic and rule-based — no LLM reasoning.
"""

from decimal import Decimal, ROUND_HALF_UP


def compute_coupon_payment(
    face_value: Decimal, coupon_rate_pct: Decimal, coupon_frequency: int, quantity: int
) -> Decimal:
    """Coupon cash flow for one coupon period, across `quantity` units held."""
    per_unit = (
        Decimal(str(face_value)) * Decimal(str(coupon_rate_pct)) / Decimal("100") / coupon_frequency
    )
    return (per_unit * quantity).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def compute_redemption_amount(face_value: Decimal, quantity: int) -> Decimal:
    """Principal redemption at maturity, at par."""
    return (Decimal(str(face_value)) * quantity).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def compute_call_put_amount(quantity: int, exercise_price: Decimal) -> Decimal:
    """Cash flow from a call/put exercise at the caller-supplied exercise price."""
    return (Decimal(str(exercise_price)) * quantity).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
