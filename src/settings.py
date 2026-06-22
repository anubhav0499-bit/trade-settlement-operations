"""
Centralized application settings — 12-factor style.

All configuration is read from environment variables with sensible
defaults for local development.  In production, inject via Docker
env, Kubernetes ConfigMap/Secret, or a .env file loaded by the
entrypoint.
"""

import os
from pathlib import Path


def _bool(val: str) -> bool:
    return val.strip().lower() in ("1", "true", "yes")


# ── Paths ──────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("SETTLE_DATA_DIR", str(PROJECT_ROOT / "data" / "generated")))
CONFIG_DIR = Path(os.getenv("SETTLE_CONFIG_DIR", str(PROJECT_ROOT / "config")))
KB_DIR = Path(os.getenv("SETTLE_KB_DIR", str(PROJECT_ROOT / "data" / "knowledge_base")))

# ── Database ───────────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("SETTLE_DATABASE_URL", f"sqlite:///{DATA_DIR / 'settlement.db'}")

# ── Logging ────────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("SETTLE_LOG_LEVEL", "INFO").upper()
LOG_FORMAT = os.getenv("SETTLE_LOG_FORMAT", "json")  # "json" | "console"

# ── ML model ───────────────────────────────────────────────────────────────
MODEL_PATH = Path(os.getenv("SETTLE_MODEL_PATH", str(DATA_DIR / "fail_predictor_model.pkl")))
MODEL_HMAC_PATH = Path(os.getenv("SETTLE_MODEL_HMAC_PATH", str(DATA_DIR / "fail_predictor_model.hmac")))
MODEL_SIGNING_KEY = os.getenv("SETTLE_MODEL_SIGNING_KEY", "trade-settlement-model-integrity-v1").encode()

# ── Embedding / knowledge base ─────────────────────────────────────────────
EMBEDDING_MODEL_NAME = os.getenv("SETTLE_EMBEDDING_MODEL", "all-MiniLM-L6-v2")

# ── Pipeline tuning ────────────────────────────────────────────────────────
ML_RISK_THRESHOLD = float(os.getenv("SETTLE_ML_RISK_THRESHOLD", "0.3"))
ML_TRAINING_SAMPLES = int(os.getenv("SETTLE_ML_TRAINING_SAMPLES", "5000"))
CSDR_BASE_RATE_BPS = float(os.getenv("SETTLE_CSDR_BASE_RATE_BPS", "1.0"))
LIQUIDITY_BUFFER_INR = float(os.getenv("SETTLE_LIQUIDITY_BUFFER_INR", "500000000"))

# ── Retry / resilience ─────────────────────────────────────────────────────
RETRY_MAX_ATTEMPTS = int(os.getenv("SETTLE_RETRY_MAX_ATTEMPTS", "3"))
RETRY_BASE_DELAY = float(os.getenv("SETTLE_RETRY_BASE_DELAY", "1.0"))
CIRCUIT_BREAKER_THRESHOLD = int(os.getenv("SETTLE_CB_THRESHOLD", "5"))
CIRCUIT_BREAKER_RESET_TIMEOUT = float(os.getenv("SETTLE_CB_RESET_TIMEOUT", "60.0"))

# ── Dashboard ──────────────────────────────────────────────────────────────
DASHBOARD_PORT = int(os.getenv("SETTLE_DASHBOARD_PORT", "8501"))

# ── Feature flags ──────────────────────────────────────────────────────────
ENABLE_ML_PREDICTION = _bool(os.getenv("SETTLE_ENABLE_ML", "true"))
ENABLE_CSDR_PENALTIES = _bool(os.getenv("SETTLE_ENABLE_CSDR", "true"))
ENABLE_ISO20022 = _bool(os.getenv("SETTLE_ENABLE_ISO20022", "true"))
ENABLE_LIQUIDITY_MONITOR = _bool(os.getenv("SETTLE_ENABLE_LIQUIDITY", "true"))
ENABLE_SCORECARDS = _bool(os.getenv("SETTLE_ENABLE_SCORECARDS", "true"))
