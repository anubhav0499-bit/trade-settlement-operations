"""
Fail-Risk Prediction (§9, Path A).

Heuristic/statistical score for pre-settlement fail risk on PENDING/CONFIRMED/
INSTRUCTED obligations that have NOT broken. This is NOT LLM reasoning — it is
a deterministic scoring model.

Features:
- Counterparty historical fail rate
- Security liquidity (proxy: price level)
- Settlement cycle (T+0 riskier than T+1)
- Time to cutoff (closer = riskier)
- Past fail rate for this counterparty × ISIN pair
"""

from dataclasses import dataclass
from datetime import datetime

from src.models.database import Obligation
from src.models.enums import ObligationStatus, SettlementCycle
from src.utils.clock import utcnow


@dataclass
class FailRiskScore:
    obligation_id: str
    risk_score: float  # 0.0 to 1.0
    risk_tier: str     # LOW / MEDIUM / HIGH
    factors: dict      # breakdown of contributing factors


# Simulated historical fail rates by counterparty
COUNTERPARTY_FAIL_RATES = {
    "BRK-001": 0.02,
    "BRK-002": 0.03,
    "BRK-003": 0.01,
    "BRK-004": 0.05,
    "BRK-005": 0.04,
    "BRK-006": 0.08,
    "CUS-001": 0.02,
    "CUS-002": 0.01,
    "CUS-003": 0.06,
    "CUS-004": 0.03,
    "CUS-005": 0.02,
    "BC-001": 0.03,
    "BC-002": 0.01,
}


def compute_fail_risk(
    obligation: Obligation,
    current_time: datetime | None = None,
) -> FailRiskScore:
    """Compute fail-risk score for a single obligation."""
    if current_time is None:
        current_time = utcnow()

    factors = {}

    # Factor 1: Counterparty historical fail rate (weight: 0.30)
    cp_fail_rate = COUNTERPARTY_FAIL_RATES.get(obligation.counterparty_id, 0.05)
    cp_score = min(cp_fail_rate / 0.10, 1.0)  # normalize: 10% fail rate → 1.0
    factors["counterparty_fail_rate"] = {
        "raw": cp_fail_rate,
        "score": round(cp_score, 3),
        "weight": 0.30,
    }

    # Factor 2: Settlement cycle risk (weight: 0.20)
    cycle_score = 0.7 if obligation.settlement_cycle == SettlementCycle.T0 else 0.3
    factors["settlement_cycle"] = {
        "cycle": obligation.settlement_cycle.value,
        "score": cycle_score,
        "weight": 0.20,
    }

    # Factor 3: Value concentration (weight: 0.20)
    val = float(obligation.net_value)
    if val > 5_000_000:
        val_score = 0.9
    elif val > 1_000_000:
        val_score = 0.6
    elif val > 500_000:
        val_score = 0.4
    else:
        val_score = 0.2
    factors["value_concentration"] = {
        "net_value": val,
        "score": val_score,
        "weight": 0.20,
    }

    # Factor 4: Time pressure (weight: 0.15)
    # Closer to settlement date = higher risk
    days_to_settle = (obligation.settlement_date - current_time.date()).days
    if days_to_settle <= 0:
        time_score = 1.0
    elif days_to_settle == 1:
        time_score = 0.6
    else:
        time_score = 0.2
    factors["time_pressure"] = {
        "days_to_settle": days_to_settle,
        "score": time_score,
        "weight": 0.15,
    }

    # Factor 5: Obligation status (weight: 0.15)
    # PENDING is riskier than CONFIRMED, which is riskier than INSTRUCTED
    status_scores = {
        ObligationStatus.PENDING: 0.8,
        ObligationStatus.SSI_VALIDATED: 0.6,
        ObligationStatus.MATCHED: 0.5,
        ObligationStatus.PENDING_CONFIRMATION: 0.7,
        ObligationStatus.CONFIRMED: 0.3,
        ObligationStatus.INSTRUCTED: 0.2,
    }
    status_score = status_scores.get(obligation.status, 0.5)
    factors["obligation_status"] = {
        "status": obligation.status.value,
        "score": status_score,
        "weight": 0.15,
    }

    # Weighted composite score
    total_score = (
        cp_score * 0.30
        + cycle_score * 0.20
        + val_score * 0.20
        + time_score * 0.15
        + status_score * 0.15
    )
    total_score = round(min(total_score, 1.0), 3)

    if total_score >= 0.7:
        tier = "HIGH"
    elif total_score >= 0.4:
        tier = "MEDIUM"
    else:
        tier = "LOW"

    return FailRiskScore(
        obligation_id=obligation.obligation_id,
        risk_score=total_score,
        risk_tier=tier,
        factors=factors,
    )


def compute_fail_risk_batch(
    obligations: list[Obligation],
    current_time: datetime | None = None,
) -> list[FailRiskScore]:
    """Compute fail-risk scores for a batch of obligations."""
    return [compute_fail_risk(ob, current_time) for ob in obligations]


def get_high_risk_queue(
    scores: list[FailRiskScore],
    threshold: float = 0.5,
) -> list[FailRiskScore]:
    """Get the queue of high-risk obligations above the threshold."""
    return sorted(
        [s for s in scores if s.risk_score >= threshold],
        key=lambda s: s.risk_score,
        reverse=True,
    )
