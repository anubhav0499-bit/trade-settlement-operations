"""Stress tests for ML settlement fail predictor."""

import hashlib
import hmac
import math
import pickle
import tempfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from src.models.database import Obligation
from src.models.enums import (
    CounterpartyType, Exchange, MatchStatus, NetDirection,
    ObligationStage, ObligationStatus, SettlementCycle,
)
from src.triage.ml_fail_predictor import (
    FEATURE_NAMES,
    _compute_model_hmac,
    _generate_synthetic_training_data,
    _MODEL_SIGNING_KEY,
    extract_features,
    get_ml_high_risk_queue,
    load_model,
    MLFailRiskScore,
    predict_fail_risk,
    train_model,
)


def _make_obligation(
    obligation_id="OB-ML-001",
    counterparty_id="BRK-001",
    isin="INE002A01018",
    net_value=1_000_000,
    settlement_cycle=SettlementCycle.T1,
    status=ObligationStatus.PENDING,
    settlement_date=date(2026, 7, 1),
) -> Obligation:
    return Obligation(
        obligation_id=obligation_id,
        isin=isin,
        security_name="TEST",
        net_quantity=100,
        net_direction=NetDirection.PAY_IN,
        vwap_price=Decimal("2900.00"),
        net_value=Decimal(str(net_value)),
        settlement_date=settlement_date,
        settlement_cycle=settlement_cycle,
        counterparty_id=counterparty_id,
        counterparty_type=CounterpartyType.BROKER,
        exchange=Exchange.NSE,
        obligation_stage=ObligationStage.FINAL,
        status=status,
        source_trade_ids='["T1"]',
    )


class TestFeatureExtraction:
    def test_feature_vector_length(self):
        ob = _make_obligation()
        features = extract_features(ob, datetime(2026, 6, 30, 10, 0))
        assert len(features) == len(FEATURE_NAMES)
        assert len(features) == 13

    def test_t0_flag(self):
        ob = _make_obligation(settlement_cycle=SettlementCycle.T0)
        features = extract_features(ob, datetime(2026, 6, 30, 10, 0))
        assert features[2] == 1.0  # is_t0

    def test_t1_flag(self):
        ob = _make_obligation(settlement_cycle=SettlementCycle.T1)
        features = extract_features(ob, datetime(2026, 6, 30, 10, 0))
        assert features[2] == 0.0

    def test_log_value_positive(self):
        ob = _make_obligation(net_value=1_000_000)
        features = extract_features(ob, datetime(2026, 6, 30, 10, 0))
        assert features[3] == pytest.approx(math.log1p(1_000_000), rel=1e-6)

    def test_days_to_settle_clipped_at_zero(self):
        ob = _make_obligation(settlement_date=date(2026, 6, 28))
        features = extract_features(ob, datetime(2026, 6, 30, 10, 0))
        assert features[5] == 0.0  # clamped to 0

    def test_unknown_counterparty_defaults(self):
        ob = _make_obligation(counterparty_id="UNKNOWN-999")
        features = extract_features(ob, datetime(2026, 6, 30, 10, 0))
        assert features[0] == 0.05  # default fail rate

    def test_month_end_flag(self):
        features_me = extract_features(
            _make_obligation(), datetime(2026, 6, 30, 10, 0)
        )
        features_mid = extract_features(
            _make_obligation(), datetime(2026, 6, 15, 10, 0)
        )
        assert features_me[10] == 1.0
        assert features_mid[10] == 0.0

    def test_hour_of_day(self):
        features = extract_features(
            _make_obligation(), datetime(2026, 6, 15, 14, 30)
        )
        assert features[9] == 14.0

    def test_concurrent_count_passed(self):
        features = extract_features(
            _make_obligation(), datetime(2026, 6, 15, 10, 0), concurrent_count=5
        )
        assert features[11] == 5.0

    def test_feature_dtype(self):
        ob = _make_obligation()
        features = extract_features(ob, datetime(2026, 6, 30, 10, 0))
        assert features.dtype == np.float64


class TestSyntheticDataGeneration:
    def test_correct_shape(self):
        X, y = _generate_synthetic_training_data(n_samples=100)
        assert X.shape == (100, 13)
        assert y.shape == (100,)

    def test_labels_are_binary(self):
        X, y = _generate_synthetic_training_data(n_samples=500)
        assert set(np.unique(y)).issubset({0.0, 1.0})

    def test_deterministic_with_seed(self):
        X1, y1 = _generate_synthetic_training_data(n_samples=50)
        X2, y2 = _generate_synthetic_training_data(n_samples=50)
        np.testing.assert_array_equal(X1, X2)
        np.testing.assert_array_equal(y1, y2)


class TestModelTrainAndPredict:
    @pytest.fixture(autouse=True)
    def _use_temp_model_path(self, tmp_path):
        model_path = tmp_path / "model.pkl"
        hmac_path = tmp_path / "model.hmac"
        with (
            patch("src.triage.ml_fail_predictor.MODEL_PATH", model_path),
            patch("src.triage.ml_fail_predictor.MODEL_HMAC_PATH", hmac_path),
        ):
            yield

    def test_train_model_creates_files(self, tmp_path):
        model = train_model()
        assert model is not None

    def test_predict_returns_score(self, tmp_path):
        model = train_model()
        ob = _make_obligation()
        score = predict_fail_risk(ob, model, datetime(2026, 6, 30, 10, 0))
        assert isinstance(score, MLFailRiskScore)
        assert 0.0 <= score.fail_probability <= 1.0
        assert score.risk_tier in ("LOW", "MEDIUM", "HIGH")
        assert score.obligation_id == "OB-ML-001"

    def test_risk_tiers(self, tmp_path):
        model = train_model()
        ob = _make_obligation()
        score = predict_fail_risk(ob, model, datetime(2026, 6, 30, 10, 0))
        if score.fail_probability >= 0.6:
            assert score.risk_tier == "HIGH"
        elif score.fail_probability >= 0.3:
            assert score.risk_tier == "MEDIUM"
        else:
            assert score.risk_tier == "LOW"

    def test_feature_contributions_present(self, tmp_path):
        model = train_model()
        ob = _make_obligation()
        score = predict_fail_risk(ob, model, datetime(2026, 6, 30, 10, 0))
        assert len(score.feature_contributions) == 13
        for name in FEATURE_NAMES:
            assert name in score.feature_contributions

    def test_model_version_format(self, tmp_path):
        model = train_model()
        ob = _make_obligation()
        score = predict_fail_risk(ob, model, datetime(2026, 6, 30, 10, 0))
        assert score.model_version.startswith("gbm-v1-")


class TestModelIntegrity:
    @pytest.fixture(autouse=True)
    def _use_temp_model_path(self, tmp_path):
        self.model_path = tmp_path / "model.pkl"
        self.hmac_path = tmp_path / "model.hmac"
        with (
            patch("src.triage.ml_fail_predictor.MODEL_PATH", self.model_path),
            patch("src.triage.ml_fail_predictor.MODEL_HMAC_PATH", self.hmac_path),
        ):
            yield

    def test_hmac_verification_passes(self):
        train_model()
        model = load_model()
        assert model is not None

    def test_tampered_model_rejected(self):
        train_model()
        with open(self.model_path, "ab") as f:
            f.write(b"TAMPERED")
        with pytest.raises(RuntimeError, match="HMAC mismatch"):
            load_model()

    def test_missing_hmac_rejected(self):
        train_model()
        self.hmac_path.unlink()
        with pytest.raises(RuntimeError, match="HMAC file missing"):
            load_model()

    def test_hmac_computation(self):
        data = b"test model bytes"
        digest = _compute_model_hmac(data)
        expected = hmac.new(_MODEL_SIGNING_KEY, data, hashlib.sha256).hexdigest()
        assert digest == expected


class TestHighRiskQueue:
    def test_filters_by_threshold(self):
        scores = [
            MLFailRiskScore("OB-1", 0.8, "HIGH", {}, "v1"),
            MLFailRiskScore("OB-2", 0.2, "LOW", {}, "v1"),
            MLFailRiskScore("OB-3", 0.5, "MEDIUM", {}, "v1"),
        ]
        queue = get_ml_high_risk_queue(scores, threshold=0.3)
        assert len(queue) == 2
        assert queue[0].obligation_id == "OB-1"
        assert queue[1].obligation_id == "OB-3"

    def test_empty_when_all_below(self):
        scores = [
            MLFailRiskScore("OB-1", 0.1, "LOW", {}, "v1"),
            MLFailRiskScore("OB-2", 0.05, "LOW", {}, "v1"),
        ]
        assert get_ml_high_risk_queue(scores, threshold=0.3) == []

    def test_sorted_descending(self):
        scores = [
            MLFailRiskScore("OB-1", 0.4, "MEDIUM", {}, "v1"),
            MLFailRiskScore("OB-2", 0.9, "HIGH", {}, "v1"),
            MLFailRiskScore("OB-3", 0.6, "HIGH", {}, "v1"),
        ]
        queue = get_ml_high_risk_queue(scores, threshold=0.3)
        assert [s.fail_probability for s in queue] == [0.9, 0.6, 0.4]
