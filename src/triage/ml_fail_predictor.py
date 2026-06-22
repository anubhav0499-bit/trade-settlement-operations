"""
ML-Based Settlement Fail Predictor.

Replaces the simple weighted-heuristic scorer with a gradient-boosted
classifier (XGBoost or sklearn GBM) trained on synthetic historical
settlement data. Feature engineering follows Accenture / DTCC research
on predictive fail models.

Features (13-dimensional):
  - Counterparty historical fail rate (rolling 90-day)
  - Counterparty historical fail count
  - Settlement cycle (T+0 = 1, T+1 = 0)
  - Net obligation value (log-scaled)
  - Net quantity
  - Days to settlement deadline
  - Obligation status ordinal
  - Security price volatility proxy (price level)
  - Counterparty type ordinal (BROKER=0, CUSTODIAN=1, CLEARING_CORP=2)
  - Hour of day (intraday timing for T+0)
  - Is month-end (concentration risk)
  - Number of concurrent obligations for same counterparty
  - ISIN-level historical fail rate

This is NOT LLM reasoning — it is a deterministic ML model.
"""

import hashlib
import hmac
import math
import pickle
from dataclasses import dataclass, field
from datetime import datetime, date
from decimal import Decimal
from pathlib import Path

import numpy as np

from src.models.database import Obligation
from src.models.enums import CounterpartyType, ObligationStatus, SettlementCycle


MODEL_PATH = Path("data/generated/fail_predictor_model.pkl")
MODEL_HMAC_PATH = Path("data/generated/fail_predictor_model.hmac")
_MODEL_SIGNING_KEY = b"trade-settlement-model-integrity-v1"
FEATURE_NAMES = [
    "cp_fail_rate_90d",
    "cp_fail_count",
    "is_t0",
    "log_net_value",
    "net_quantity",
    "days_to_settle",
    "status_ordinal",
    "price_level",
    "cp_type_ordinal",
    "hour_of_day",
    "is_month_end",
    "concurrent_obligations",
    "isin_fail_rate",
]


@dataclass
class MLFailRiskScore:
    obligation_id: str
    fail_probability: float
    risk_tier: str
    feature_contributions: dict
    model_version: str


# Simulated 90-day rolling fail statistics
COUNTERPARTY_FAIL_STATS = {
    "BRK-001": {"fail_rate": 0.02, "fail_count": 3, "total_obligations": 150},
    "BRK-002": {"fail_rate": 0.03, "fail_count": 5, "total_obligations": 167},
    "BRK-003": {"fail_rate": 0.01, "fail_count": 1, "total_obligations": 100},
    "BRK-004": {"fail_rate": 0.05, "fail_count": 8, "total_obligations": 160},
    "BRK-005": {"fail_rate": 0.04, "fail_count": 6, "total_obligations": 150},
    "BRK-006": {"fail_rate": 0.08, "fail_count": 12, "total_obligations": 150},
    "CUS-001": {"fail_rate": 0.02, "fail_count": 4, "total_obligations": 200},
    "CUS-002": {"fail_rate": 0.01, "fail_count": 2, "total_obligations": 200},
    "CUS-003": {"fail_rate": 0.06, "fail_count": 9, "total_obligations": 150},
    "CUS-004": {"fail_rate": 0.03, "fail_count": 4, "total_obligations": 133},
    "CUS-005": {"fail_rate": 0.02, "fail_count": 3, "total_obligations": 150},
    "BC-001": {"fail_rate": 0.03, "fail_count": 4, "total_obligations": 133},
    "BC-002": {"fail_rate": 0.01, "fail_count": 1, "total_obligations": 100},
}

ISIN_FAIL_RATES = {}

STATUS_ORDINAL = {
    ObligationStatus.PENDING: 0,
    ObligationStatus.SSI_VALIDATED: 1,
    ObligationStatus.MATCHED: 2,
    ObligationStatus.PENDING_CONFIRMATION: 3,
    ObligationStatus.CONFIRMED: 4,
    ObligationStatus.INSTRUCTED: 5,
}

CP_TYPE_ORDINAL = {
    CounterpartyType.BROKER: 0,
    CounterpartyType.CUSTODIAN: 1,
    CounterpartyType.CLEARING_CORP: 2,
}


def extract_features(
    obligation: Obligation,
    current_time: datetime,
    concurrent_count: int = 1,
) -> np.ndarray:
    """Extract the 13-dimensional feature vector for a single obligation."""
    cp_stats = COUNTERPARTY_FAIL_STATS.get(
        obligation.counterparty_id,
        {"fail_rate": 0.05, "fail_count": 5, "total_obligations": 100},
    )

    is_t0 = 1.0 if obligation.settlement_cycle == SettlementCycle.T0 else 0.0

    net_value = float(obligation.net_value)
    log_value = math.log1p(net_value) if net_value > 0 else 0.0

    days_to_settle = (obligation.settlement_date - current_time.date()).days
    days_to_settle = max(days_to_settle, 0)

    status_ord = STATUS_ORDINAL.get(obligation.status, 3)
    price_level = float(obligation.vwap_price)

    cp_type_ord = CP_TYPE_ORDINAL.get(obligation.counterparty_type, 0)

    hour = current_time.hour
    is_month_end = 1.0 if current_time.day >= 28 else 0.0

    isin_fail_rate = ISIN_FAIL_RATES.get(obligation.isin, 0.03)

    return np.array([
        cp_stats["fail_rate"],
        cp_stats["fail_count"],
        is_t0,
        log_value,
        float(obligation.net_quantity),
        float(days_to_settle),
        float(status_ord),
        price_level,
        float(cp_type_ord),
        float(hour),
        is_month_end,
        float(concurrent_count),
        isin_fail_rate,
    ], dtype=np.float64)


def _generate_synthetic_training_data(n_samples: int = 5000) -> tuple[np.ndarray, np.ndarray]:
    """Generate synthetic historical settlement data for model training.

    Uses realistic distributions derived from industry benchmarks:
    - Overall fail rate ~3-5%
    - T+0 fails ~2x T+1 rate
    - High-value obligations fail more often
    - Late-stage obligations fail less
    """
    rng = np.random.RandomState(42)

    X = np.zeros((n_samples, len(FEATURE_NAMES)))
    y = np.zeros(n_samples)

    for i in range(n_samples):
        cp_fail_rate = rng.beta(2, 50)
        cp_fail_count = int(cp_fail_rate * rng.randint(50, 300))
        is_t0 = float(rng.random() < 0.3)
        log_value = rng.normal(14.0, 2.0)
        net_qty = rng.randint(1, 5000)
        days_to_settle = rng.choice([0, 1, 2, 3], p=[0.3, 0.4, 0.2, 0.1])
        status_ord = rng.choice([0, 1, 2, 3, 4, 5], p=[0.1, 0.1, 0.2, 0.15, 0.25, 0.2])
        price_level = rng.lognormal(7.0, 1.0)
        cp_type = rng.choice([0, 1, 2], p=[0.5, 0.4, 0.1])
        hour = rng.randint(9, 16)
        is_month_end = float(rng.random() < 0.15)
        concurrent = rng.randint(1, 20)
        isin_fail_rate = rng.beta(2, 60)

        X[i] = [
            cp_fail_rate, cp_fail_count, is_t0, log_value, net_qty,
            days_to_settle, status_ord, price_level, cp_type, hour,
            is_month_end, concurrent, isin_fail_rate,
        ]

        # Fail probability is a function of features (logistic model for label generation)
        logit = (
            -4.0
            + 8.0 * cp_fail_rate
            + 0.05 * cp_fail_count
            + 0.8 * is_t0
            + 0.15 * (log_value - 14.0)
            - 0.3 * days_to_settle
            - 0.25 * status_ord
            + 0.0001 * price_level
            + 0.2 * cp_type
            + 0.05 * (hour - 12)
            + 0.5 * is_month_end
            + 0.03 * concurrent
            + 6.0 * isin_fail_rate
        )
        prob = 1.0 / (1.0 + math.exp(-logit))
        y[i] = 1.0 if rng.random() < prob else 0.0

    return X, y


def _compute_model_hmac(model_bytes: bytes) -> str:
    """Compute HMAC-SHA256 digest for model integrity verification."""
    return hmac.new(_MODEL_SIGNING_KEY, model_bytes, hashlib.sha256).hexdigest()


def train_model() -> object:
    """Train a GBM classifier on synthetic historical data."""
    try:
        from sklearn.ensemble import GradientBoostingClassifier
    except ImportError:
        raise ImportError("scikit-learn is required: pip install scikit-learn")

    X, y = _generate_synthetic_training_data(n_samples=5000)

    model = GradientBoostingClassifier(
        n_estimators=100,
        max_depth=4,
        learning_rate=0.1,
        min_samples_leaf=20,
        subsample=0.8,
        random_state=42,
    )
    model.fit(X, y)

    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    model_bytes = pickle.dumps(model)

    with open(MODEL_PATH, "wb") as f:
        f.write(model_bytes)

    digest = _compute_model_hmac(model_bytes)
    with open(MODEL_HMAC_PATH, "w") as f:
        f.write(digest)

    return model


def load_model() -> object:
    """Load the trained model with HMAC integrity verification.

    Refuses to load if the HMAC signature file is missing or does not
    match — protects against tampered or injected pickle files.
    """
    if not MODEL_PATH.exists():
        return train_model()

    if not MODEL_HMAC_PATH.exists():
        raise RuntimeError(
            f"Model HMAC file missing at {MODEL_HMAC_PATH}. "
            "Cannot verify model integrity — refusing to load. "
            "Re-train the model with train_model() to regenerate."
        )

    with open(MODEL_PATH, "rb") as f:
        model_bytes = f.read()

    with open(MODEL_HMAC_PATH, "r") as f:
        stored_hmac = f.read().strip()

    computed_hmac = _compute_model_hmac(model_bytes)
    if not hmac.compare_digest(stored_hmac, computed_hmac):
        raise RuntimeError(
            "Model integrity check FAILED — HMAC mismatch. "
            "The model file may have been tampered with. "
            "Re-train with train_model() to generate a trusted model."
        )

    return pickle.loads(model_bytes)


def predict_fail_risk(
    obligation: Obligation,
    model: object,
    current_time: datetime | None = None,
    concurrent_count: int = 1,
) -> MLFailRiskScore:
    """Predict settlement fail probability for a single obligation."""
    if current_time is None:
        current_time = datetime.utcnow()

    features = extract_features(obligation, current_time, concurrent_count)
    X = features.reshape(1, -1)

    prob = model.predict_proba(X)[0][1]

    if prob >= 0.6:
        tier = "HIGH"
    elif prob >= 0.3:
        tier = "MEDIUM"
    else:
        tier = "LOW"

    importances = model.feature_importances_
    contributions = {}
    for idx, name in enumerate(FEATURE_NAMES):
        contributions[name] = {
            "value": round(float(features[idx]), 4),
            "importance": round(float(importances[idx]), 4),
        }

    model_hash = hashlib.md5(pickle.dumps(model.get_params())).hexdigest()[:8]

    return MLFailRiskScore(
        obligation_id=obligation.obligation_id,
        fail_probability=round(float(prob), 4),
        risk_tier=tier,
        feature_contributions=contributions,
        model_version=f"gbm-v1-{model_hash}",
    )


def predict_fail_risk_batch(
    obligations: list[Obligation],
    current_time: datetime | None = None,
) -> list[MLFailRiskScore]:
    """Predict fail risk for a batch of obligations."""
    if current_time is None:
        current_time = datetime.utcnow()

    model = load_model()

    cp_counts: dict[str, int] = {}
    for ob in obligations:
        cp_counts[ob.counterparty_id] = cp_counts.get(ob.counterparty_id, 0) + 1

    results = []
    for ob in obligations:
        score = predict_fail_risk(
            ob, model, current_time,
            concurrent_count=cp_counts.get(ob.counterparty_id, 1),
        )
        results.append(score)

    return results


def get_ml_high_risk_queue(
    scores: list[MLFailRiskScore],
    threshold: float = 0.3,
) -> list[MLFailRiskScore]:
    """Get obligations above the fail probability threshold, sorted by risk."""
    return sorted(
        [s for s in scores if s.fail_probability >= threshold],
        key=lambda s: s.fail_probability,
        reverse=True,
    )
