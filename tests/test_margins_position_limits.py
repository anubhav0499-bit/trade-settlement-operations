"""Tests for market-wide, CM-level, and client-level position limits."""

from src.margins.position_limits import (
    check_client_level_limit,
    check_cm_level_limit,
    check_market_wide_limit,
    market_wide_limit,
)


class TestMarketWideLimit:
    def test_limit_is_pct_of_free_float(self):
        # 20% of 1,000,000 lots = 200,000
        assert market_wide_limit(1_000_000) == 200_000

    def test_within_limit_returns_none(self):
        assert check_market_wide_limit(150_000, 1_000_000) is None

    def test_exceeding_limit_returns_violation(self):
        violation = check_market_wide_limit(250_000, 1_000_000)
        assert violation is not None
        assert violation.level == "MARKET_WIDE"
        assert violation.limit == 200_000
        assert violation.actual == 250_000


class TestCmLevelLimit:
    def test_within_limit_returns_none(self):
        # cm limit = 15% of 200,000 = 30,000
        assert check_cm_level_limit("CM-001", 25_000, 1_000_000) is None

    def test_exceeding_limit_returns_violation(self):
        violation = check_cm_level_limit("CM-001", 35_000, 1_000_000)
        assert violation is not None
        assert violation.level == "CM_LEVEL"
        assert violation.entity_id == "CM-001"
        assert violation.limit == 30_000


class TestClientLevelLimit:
    def test_within_limit_returns_none(self):
        # client limit = 1% of 200,000 = 2,000
        assert check_client_level_limit("CLIENT-001", 1_500, 1_000_000) is None

    def test_exceeding_limit_returns_violation(self):
        violation = check_client_level_limit("CLIENT-001", 2_500, 1_000_000)
        assert violation is not None
        assert violation.level == "CLIENT_LEVEL"
        assert violation.limit == 2_000
