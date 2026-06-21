"""
CSDR-Style Progressive Settlement Penalty Calculator.

Implements the Central Securities Depositories Regulation (EU 2022/1930)
penalty framework adapted for Indian equity markets:

- Daily cash penalties accrue from settlement date + 1 until delivery
- Penalty rate escalates with aging: base rate for days 1-3, 2x for days 4-7,
  3x for days 8+
- Rates differ by security liquidity tier (liquid vs illiquid)
- Separate penalty rates for fails-to-deliver (higher) vs fails-to-receive
- Penalties computed on the settlement value, not the market value
- Monthly aggregation for counterparty billing

Reference: ESMA Guidelines on CSDR cash penalties (ESMA70-156-5765),
adapted with SEBI-aligned penalty bands.

This is deterministic arithmetic — no LLM reasoning.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from src.models.database import BreakRecord, Obligation
from src.models.enums import BreakType, NetDirection, SettlementCycle


@dataclass
class PenaltyRate:
    base_rate_bps: Decimal      # basis points per day (liquid securities)
    illiquid_rate_bps: Decimal   # basis points per day (illiquid securities)
    escalation_2x_day: int      # day from which rate doubles
    escalation_3x_day: int      # day from which rate triples


DEFAULT_PENALTY_RATES = PenaltyRate(
    base_rate_bps=Decimal("1.0"),       # 0.01% per day for liquid equities
    illiquid_rate_bps=Decimal("0.5"),    # 0.005% per day for illiquid
    escalation_2x_day=4,
    escalation_3x_day=8,
)

FAIL_TO_DELIVER_MULTIPLIER = Decimal("1.5")

ILLIQUID_ISINS = {
    "INE274J01014", "INE545U01014", "INE124N01016", "INE483S01020",
    "INE00IN01015", "INE03YQ01011", "INE00WK01013", "INE03VK01010",
    "INE148O01018", "INE761H01022", "INE059B01024", "INE550C01020",
    "INE104S01021",
}


@dataclass
class DailyPenalty:
    day: int                    # settlement fail day (1-based)
    date: date
    rate_bps: Decimal
    penalty_amount: Decimal     # INR
    cumulative: Decimal         # INR


@dataclass
class PenaltyAssessment:
    obligation_id: str
    counterparty_id: str
    isin: str
    settlement_value: Decimal
    fail_direction: str         # DELIVER or RECEIVE
    fail_start_date: date
    assessment_date: date
    total_fail_days: int
    daily_breakdown: list[DailyPenalty]
    total_penalty: Decimal
    penalty_tier: str           # STANDARD / ESCALATED / CRITICAL


def _get_daily_rate(
    day: int,
    is_illiquid: bool,
    is_fail_to_deliver: bool,
    rates: PenaltyRate,
) -> Decimal:
    """Compute the applicable penalty rate for a given fail day."""
    base = rates.illiquid_rate_bps if is_illiquid else rates.base_rate_bps

    if day >= rates.escalation_3x_day:
        multiplier = Decimal("3")
    elif day >= rates.escalation_2x_day:
        multiplier = Decimal("2")
    else:
        multiplier = Decimal("1")

    rate = base * multiplier

    if is_fail_to_deliver:
        rate = rate * FAIL_TO_DELIVER_MULTIPLIER

    return rate


def compute_penalty(
    obligation: Obligation,
    fail_start_date: date,
    assessment_date: date,
    rates: PenaltyRate | None = None,
) -> PenaltyAssessment:
    """Compute progressive penalties for a single failed obligation."""
    if rates is None:
        rates = DEFAULT_PENALTY_RATES

    settlement_value = Decimal(str(obligation.net_value))
    is_illiquid = obligation.isin in ILLIQUID_ISINS
    is_ftd = obligation.net_direction == NetDirection.PAY_IN

    total_days = (assessment_date - fail_start_date).days
    total_days = max(total_days, 0)

    daily_breakdown = []
    cumulative = Decimal("0")

    for day_num in range(1, total_days + 1):
        penalty_date = fail_start_date + timedelta(days=day_num)
        rate_bps = _get_daily_rate(day_num, is_illiquid, is_ftd, rates)

        daily_amount = (settlement_value * rate_bps / Decimal("10000")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        cumulative += daily_amount

        daily_breakdown.append(DailyPenalty(
            day=day_num,
            date=penalty_date,
            rate_bps=rate_bps,
            penalty_amount=daily_amount,
            cumulative=cumulative,
        ))

    if total_days >= rates.escalation_3x_day:
        tier = "CRITICAL"
    elif total_days >= rates.escalation_2x_day:
        tier = "ESCALATED"
    else:
        tier = "STANDARD"

    return PenaltyAssessment(
        obligation_id=obligation.obligation_id,
        counterparty_id=obligation.counterparty_id,
        isin=obligation.isin,
        settlement_value=settlement_value,
        fail_direction="DELIVER" if is_ftd else "RECEIVE",
        fail_start_date=fail_start_date,
        assessment_date=assessment_date,
        total_fail_days=total_days,
        daily_breakdown=daily_breakdown,
        total_penalty=cumulative,
        penalty_tier=tier,
    )


def compute_penalties_batch(
    failed_obligations: list[tuple[Obligation, date]],
    assessment_date: date,
    rates: PenaltyRate | None = None,
) -> list[PenaltyAssessment]:
    """Compute penalties for a batch of failed obligations.

    Args:
        failed_obligations: List of (Obligation, fail_start_date) tuples
        assessment_date: Date to compute penalties through
        rates: Custom penalty rates (uses defaults if None)
    """
    return [
        compute_penalty(ob, fail_date, assessment_date, rates)
        for ob, fail_date in failed_obligations
    ]


def aggregate_by_counterparty(
    assessments: list[PenaltyAssessment],
) -> dict[str, dict]:
    """Aggregate penalties by counterparty for billing."""
    cp_totals: dict[str, dict] = {}

    for pa in assessments:
        if pa.counterparty_id not in cp_totals:
            cp_totals[pa.counterparty_id] = {
                "counterparty_id": pa.counterparty_id,
                "total_penalty": Decimal("0"),
                "fail_count": 0,
                "avg_fail_days": 0.0,
                "by_tier": {"STANDARD": 0, "ESCALATED": 0, "CRITICAL": 0},
                "obligations": [],
            }

        entry = cp_totals[pa.counterparty_id]
        entry["total_penalty"] += pa.total_penalty
        entry["fail_count"] += 1
        entry["by_tier"][pa.penalty_tier] += 1
        entry["obligations"].append(pa.obligation_id)

    for entry in cp_totals.values():
        if entry["fail_count"] > 0:
            total_days = sum(
                a.total_fail_days
                for a in assessments
                if a.counterparty_id == entry["counterparty_id"]
            )
            entry["avg_fail_days"] = round(total_days / entry["fail_count"], 1)

    return cp_totals


def get_penalty_summary(assessments: list[PenaltyAssessment]) -> dict:
    """Get a summary of all penalty assessments."""
    if not assessments:
        return {
            "total_penalties": Decimal("0"),
            "total_fails": 0,
            "by_tier": {},
            "by_direction": {},
        }

    total = sum(a.total_penalty for a in assessments)

    by_tier: dict[str, int] = {}
    by_direction: dict[str, int] = {}
    for a in assessments:
        by_tier[a.penalty_tier] = by_tier.get(a.penalty_tier, 0) + 1
        by_direction[a.fail_direction] = by_direction.get(a.fail_direction, 0) + 1

    return {
        "total_penalties": total,
        "total_fails": len(assessments),
        "by_tier": by_tier,
        "by_direction": by_direction,
        "avg_penalty_per_fail": round(total / len(assessments), 2) if assessments else Decimal("0"),
    }
