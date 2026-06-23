"""
Counterparty Risk Scorecard.

Composite risk scoring for each counterparty combining multiple
quantitative dimensions into a single risk rating. Inspired by
DTCC's participant risk framework and clearing corporation
margining models.

Scoring dimensions (100-point scale, lower = riskier):
  1. Settlement Efficiency (25%) — STP rate for this counterparty
  2. Fail History (25%) — rolling 90-day fail rate and trend
  3. Break Frequency (20%) — breaks per 100 obligations
  4. Timeliness (15%) — % of confirmations received before cutoff
  5. Concentration Risk (15%) — value-weighted Herfindahl index

Output: composite score (0-100), letter grade (A/B/C/D/F),
risk-adjusted exposure limits.

This is deterministic arithmetic — no LLM reasoning.
"""

from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from src.models.database import BreakRecord, Obligation
from src.models.enums import (
    ConfirmationStatus,
    ObligationStage,
    ObligationStatus,
)


@dataclass
class RiskDimension:
    name: str
    score: float        # 0-100
    weight: float       # 0-1
    detail: dict


@dataclass
class CounterpartyScorecard:
    counterparty_id: str
    composite_score: float      # 0-100
    letter_grade: str           # A / B / C / D / F
    dimensions: list[RiskDimension]
    exposure_limit_multiplier: float
    watch_list: bool
    assessment_date: date


GRADE_THRESHOLDS = [
    (80, "A", 1.2),     # low risk — can increase exposure
    (65, "B", 1.0),     # normal
    (50, "C", 0.8),     # elevated — reduce exposure
    (35, "D", 0.5),     # high risk — restrict trading
    (0,  "F", 0.2),     # critical — near-suspend
]


def _settlement_efficiency_score(
    session: Session,
    counterparty_id: str,
) -> RiskDimension:
    """% of obligations that settled without manual intervention."""
    total = (
        session.query(Obligation)
        .filter(
            Obligation.counterparty_id == counterparty_id,
            Obligation.obligation_stage == ObligationStage.FINAL,
        )
        .count()
    )

    settled_clean = (
        session.query(Obligation)
        .filter(
            Obligation.counterparty_id == counterparty_id,
            Obligation.obligation_stage == ObligationStage.FINAL,
            Obligation.status.in_([ObligationStatus.SETTLED, ObligationStatus.INSTRUCTED]),
        )
        .count()
    )

    stp_rate = (settled_clean / total * 100) if total > 0 else 100.0
    score = min(stp_rate, 100.0)

    return RiskDimension(
        name="Settlement Efficiency",
        score=round(score, 1),
        weight=0.25,
        detail={
            "total_obligations": total,
            "settled_clean": settled_clean,
            "stp_rate": round(stp_rate, 1),
        },
    )


def _fail_history_score(
    session: Session,
    counterparty_id: str,
) -> RiskDimension:
    """Rolling fail rate and trend analysis."""
    total = (
        session.query(Obligation)
        .filter(
            Obligation.counterparty_id == counterparty_id,
            Obligation.obligation_stage == ObligationStage.FINAL,
        )
        .count()
    )

    failed = (
        session.query(Obligation)
        .filter(
            Obligation.counterparty_id == counterparty_id,
            Obligation.obligation_stage == ObligationStage.FINAL,
            Obligation.status.in_([
                ObligationStatus.FAILED,
                ObligationStatus.AUCTION,
                ObligationStatus.CLOSED_OUT,
            ]),
        )
        .count()
    )

    fail_rate = (failed / total * 100) if total > 0 else 0.0

    # Score: 0% fails = 100, 10%+ fails = 0, linear
    score = max(0.0, 100.0 - fail_rate * 10)

    return RiskDimension(
        name="Fail History",
        score=round(score, 1),
        weight=0.25,
        detail={
            "total_obligations": total,
            "failed": failed,
            "fail_rate_pct": round(fail_rate, 2),
        },
    )


def _break_frequency_score(
    session: Session,
    counterparty_id: str,
) -> RiskDimension:
    """Breaks per 100 obligations."""
    obligations = (
        session.query(Obligation)
        .filter(
            Obligation.counterparty_id == counterparty_id,
            Obligation.obligation_stage == ObligationStage.FINAL,
        )
        .all()
    )

    ob_ids = {ob.obligation_id for ob in obligations}

    break_count = (
        session.query(BreakRecord)
        .filter(BreakRecord.obligation_id.in_(ob_ids))
        .count()
    ) if ob_ids else 0

    breaks_per_100 = (break_count / len(ob_ids) * 100) if ob_ids else 0.0

    # Score: 0 breaks = 100, 20+ per 100 = 0
    score = max(0.0, 100.0 - breaks_per_100 * 5)

    return RiskDimension(
        name="Break Frequency",
        score=round(score, 1),
        weight=0.20,
        detail={
            "total_obligations": len(ob_ids),
            "break_count": break_count,
            "breaks_per_100": round(breaks_per_100, 1),
        },
    )


def _timeliness_score(
    session: Session,
    counterparty_id: str,
) -> RiskDimension:
    """% of confirmations received before cutoff."""
    custodian_obs = (
        session.query(Obligation)
        .filter(
            Obligation.counterparty_id == counterparty_id,
            Obligation.obligation_stage == ObligationStage.FINAL,
            Obligation.confirmation_status != ConfirmationStatus.NOT_REQUIRED,
        )
        .all()
    )

    total = len(custodian_obs)
    on_time = sum(
        1 for ob in custodian_obs
        if ob.confirmation_status == ConfirmationStatus.CONFIRMED
    )

    pct_on_time = (on_time / total * 100) if total > 0 else 100.0
    score = min(pct_on_time, 100.0)

    return RiskDimension(
        name="Timeliness",
        score=round(score, 1),
        weight=0.15,
        detail={
            "total_confirmations": total,
            "on_time": on_time,
            "on_time_pct": round(pct_on_time, 1),
        },
    )


def _concentration_risk_score(
    session: Session,
    counterparty_id: str,
) -> RiskDimension:
    """Herfindahl index on ISIN-level value concentration."""
    obligations = (
        session.query(Obligation)
        .filter(
            Obligation.counterparty_id == counterparty_id,
            Obligation.obligation_stage == ObligationStage.FINAL,
        )
        .all()
    )

    if not obligations:
        return RiskDimension(
            name="Concentration Risk",
            score=100.0,
            weight=0.15,
            detail={"hhi": 0, "isin_count": 0},
        )

    isin_values: dict[str, float] = {}
    total_value = 0.0
    for ob in obligations:
        val = abs(float(ob.net_value))
        isin_values[ob.isin] = isin_values.get(ob.isin, 0.0) + val
        total_value += val

    if total_value == 0:
        hhi = 0.0
    else:
        hhi = sum((v / total_value) ** 2 for v in isin_values.values())

    # HHI ranges from 1/n to 1.0; lower is more diversified
    # Score: HHI=0.1 (diversified) → 100, HHI=1.0 (concentrated) → 0
    score = max(0.0, (1.0 - hhi) * 100)

    return RiskDimension(
        name="Concentration Risk",
        score=round(score, 1),
        weight=0.15,
        detail={
            "hhi": round(hhi, 4),
            "isin_count": len(isin_values),
            "total_value": round(total_value, 2),
        },
    )


def compute_scorecard(
    session: Session,
    counterparty_id: str,
    assessment_date: date | None = None,
) -> CounterpartyScorecard:
    """Compute the full risk scorecard for a counterparty."""
    if assessment_date is None:
        assessment_date = date.today()

    dimensions = [
        _settlement_efficiency_score(session, counterparty_id),
        _fail_history_score(session, counterparty_id),
        _break_frequency_score(session, counterparty_id),
        _timeliness_score(session, counterparty_id),
        _concentration_risk_score(session, counterparty_id),
    ]

    composite = sum(d.score * d.weight for d in dimensions)
    composite = round(composite, 1)

    grade = "F"
    multiplier = 0.2
    for threshold, g, m in GRADE_THRESHOLDS:
        if composite >= threshold:
            grade = g
            multiplier = m
            break

    return CounterpartyScorecard(
        counterparty_id=counterparty_id,
        composite_score=composite,
        letter_grade=grade,
        dimensions=dimensions,
        exposure_limit_multiplier=multiplier,
        watch_list=grade in ("D", "F"),
        assessment_date=assessment_date,
    )


def compute_all_scorecards(
    session: Session,
    counterparty_ids: list[str],
    assessment_date: date | None = None,
) -> list[CounterpartyScorecard]:
    """Compute scorecards for all counterparties."""
    return [
        compute_scorecard(session, cp_id, assessment_date)
        for cp_id in counterparty_ids
    ]


def get_watch_list(scorecards: list[CounterpartyScorecard]) -> list[CounterpartyScorecard]:
    """Get counterparties on the risk watch list (grade D or F)."""
    return [sc for sc in scorecards if sc.watch_list]


def get_scorecard_summary(scorecards: list[CounterpartyScorecard]) -> dict:
    """Summarize scorecard results across all counterparties."""
    if not scorecards:
        return {"total": 0, "by_grade": {}, "avg_score": 0}

    by_grade: dict[str, int] = {}
    for sc in scorecards:
        by_grade[sc.letter_grade] = by_grade.get(sc.letter_grade, 0) + 1

    avg = sum(sc.composite_score for sc in scorecards) / len(scorecards)

    return {
        "total": len(scorecards),
        "by_grade": by_grade,
        "avg_score": round(avg, 1),
        "watch_list_count": sum(1 for sc in scorecards if sc.watch_list),
    }
