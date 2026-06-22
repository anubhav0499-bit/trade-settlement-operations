"""Stress tests for CSDR progressive settlement penalty calculator."""

import pytest
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.models.database import Base, Obligation
from src.models.enums import (
    CounterpartyType, Exchange, MatchStatus, NetDirection,
    ObligationStage, ObligationStatus, SettlementCycle,
)
from src.penalties.csdr_penalties import (
    DEFAULT_PENALTY_RATES,
    FAIL_TO_DELIVER_MULTIPLIER,
    ILLIQUID_ISINS,
    PenaltyRate,
    _get_daily_rate,
    aggregate_by_counterparty,
    compute_penalty,
    compute_penalties_batch,
    get_penalty_summary,
)


def _make_obligation(
    obligation_id="OB-001",
    isin="INE002A01018",
    net_value=1_000_000,
    net_direction=NetDirection.PAY_IN,
    counterparty_id="BRK-001",
    settlement_cycle=SettlementCycle.T1,
) -> Obligation:
    return Obligation(
        obligation_id=obligation_id,
        isin=isin,
        security_name="TEST",
        net_quantity=100,
        net_direction=net_direction,
        vwap_price=Decimal("1000.00"),
        net_value=Decimal(str(net_value)),
        settlement_date=date(2026, 6, 1),
        settlement_cycle=settlement_cycle,
        counterparty_id=counterparty_id,
        counterparty_type=CounterpartyType.BROKER,
        exchange=Exchange.NSE,
        obligation_stage=ObligationStage.FINAL,
        status=ObligationStatus.FAILED,
        source_trade_ids='["T1"]',
    )


class TestDailyRate:
    def test_liquid_base_rate(self):
        rate = _get_daily_rate(1, is_illiquid=False, is_fail_to_deliver=False,
                               rates=DEFAULT_PENALTY_RATES)
        assert rate == Decimal("1.0")

    def test_illiquid_base_rate(self):
        rate = _get_daily_rate(1, is_illiquid=True, is_fail_to_deliver=False,
                               rates=DEFAULT_PENALTY_RATES)
        assert rate == Decimal("0.5")

    def test_escalation_2x_at_day_4(self):
        rate = _get_daily_rate(4, is_illiquid=False, is_fail_to_deliver=False,
                               rates=DEFAULT_PENALTY_RATES)
        assert rate == Decimal("2.0")

    def test_escalation_3x_at_day_8(self):
        rate = _get_daily_rate(8, is_illiquid=False, is_fail_to_deliver=False,
                               rates=DEFAULT_PENALTY_RATES)
        assert rate == Decimal("3.0")

    def test_day_3_is_still_1x(self):
        rate = _get_daily_rate(3, is_illiquid=False, is_fail_to_deliver=False,
                               rates=DEFAULT_PENALTY_RATES)
        assert rate == Decimal("1.0")

    def test_day_7_is_still_2x(self):
        rate = _get_daily_rate(7, is_illiquid=False, is_fail_to_deliver=False,
                               rates=DEFAULT_PENALTY_RATES)
        assert rate == Decimal("2.0")

    def test_ftd_multiplier(self):
        rate = _get_daily_rate(1, is_illiquid=False, is_fail_to_deliver=True,
                               rates=DEFAULT_PENALTY_RATES)
        assert rate == Decimal("1.0") * FAIL_TO_DELIVER_MULTIPLIER

    def test_ftd_plus_escalation(self):
        rate = _get_daily_rate(8, is_illiquid=False, is_fail_to_deliver=True,
                               rates=DEFAULT_PENALTY_RATES)
        assert rate == Decimal("3.0") * FAIL_TO_DELIVER_MULTIPLIER

    def test_illiquid_plus_ftd_plus_escalation(self):
        rate = _get_daily_rate(8, is_illiquid=True, is_fail_to_deliver=True,
                               rates=DEFAULT_PENALTY_RATES)
        expected = Decimal("0.5") * Decimal("3") * FAIL_TO_DELIVER_MULTIPLIER
        assert rate == expected

    def test_very_late_day_30(self):
        rate = _get_daily_rate(30, is_illiquid=False, is_fail_to_deliver=False,
                               rates=DEFAULT_PENALTY_RATES)
        assert rate == Decimal("3.0")


class TestComputePenalty:
    def test_zero_fail_days(self):
        ob = _make_obligation()
        result = compute_penalty(ob, date(2026, 6, 1), date(2026, 6, 1))
        assert result.total_fail_days == 0
        assert result.total_penalty == Decimal("0")
        assert result.penalty_tier == "STANDARD"
        assert len(result.daily_breakdown) == 0

    def test_one_day_fail_liquid_ftd(self):
        ob = _make_obligation(net_value=1_000_000)
        result = compute_penalty(ob, date(2026, 6, 1), date(2026, 6, 2))
        assert result.total_fail_days == 1
        # 1M * 1.0 bps * 1.5 (FTD) / 10000 = 150.00
        assert result.total_penalty == Decimal("150.00")

    def test_three_day_fail_standard_tier(self):
        ob = _make_obligation(net_value=1_000_000)
        result = compute_penalty(ob, date(2026, 6, 1), date(2026, 6, 4))
        assert result.total_fail_days == 3
        assert result.penalty_tier == "STANDARD"

    def test_five_day_fail_escalated_tier(self):
        ob = _make_obligation(net_value=1_000_000)
        result = compute_penalty(ob, date(2026, 6, 1), date(2026, 6, 6))
        assert result.total_fail_days == 5
        assert result.penalty_tier == "ESCALATED"

    def test_ten_day_fail_critical_tier(self):
        ob = _make_obligation(net_value=1_000_000)
        result = compute_penalty(ob, date(2026, 6, 1), date(2026, 6, 11))
        assert result.total_fail_days == 10
        assert result.penalty_tier == "CRITICAL"

    def test_receive_fail_no_ftd_multiplier(self):
        ob = _make_obligation(net_value=1_000_000, net_direction=NetDirection.PAY_OUT)
        result = compute_penalty(ob, date(2026, 6, 1), date(2026, 6, 2))
        # 1M * 1.0 bps / 10000 = 100.00 (no FTD multiplier)
        assert result.total_penalty == Decimal("100.00")
        assert result.fail_direction == "RECEIVE"

    def test_illiquid_isin_lower_rate(self):
        illiquid_isin = list(ILLIQUID_ISINS)[0]
        ob = _make_obligation(net_value=1_000_000, isin=illiquid_isin,
                              net_direction=NetDirection.PAY_OUT)
        result = compute_penalty(ob, date(2026, 6, 1), date(2026, 6, 2))
        # 1M * 0.5 bps / 10000 = 50.00
        assert result.total_penalty == Decimal("50.00")

    def test_daily_breakdown_length(self):
        ob = _make_obligation()
        result = compute_penalty(ob, date(2026, 6, 1), date(2026, 6, 8))
        assert len(result.daily_breakdown) == 7
        assert result.daily_breakdown[0].day == 1
        assert result.daily_breakdown[-1].day == 7

    def test_cumulative_increases_monotonically(self):
        ob = _make_obligation()
        result = compute_penalty(ob, date(2026, 6, 1), date(2026, 6, 11))
        for i in range(1, len(result.daily_breakdown)):
            assert result.daily_breakdown[i].cumulative > result.daily_breakdown[i-1].cumulative

    def test_assessment_before_fail_start(self):
        ob = _make_obligation()
        result = compute_penalty(ob, date(2026, 6, 5), date(2026, 6, 1))
        assert result.total_fail_days == 0
        assert result.total_penalty == Decimal("0")

    def test_custom_rates(self):
        custom = PenaltyRate(
            base_rate_bps=Decimal("5.0"),
            illiquid_rate_bps=Decimal("2.5"),
            escalation_2x_day=2,
            escalation_3x_day=4,
        )
        ob = _make_obligation(net_value=1_000_000, net_direction=NetDirection.PAY_OUT)
        result = compute_penalty(ob, date(2026, 6, 1), date(2026, 6, 2), rates=custom)
        # 1M * 5.0 bps / 10000 = 500.00
        assert result.total_penalty == Decimal("500.00")


class TestBatchPenalties:
    def test_batch_computes_all(self):
        obs = [
            (_make_obligation(obligation_id=f"OB-{i}"), date(2026, 6, 1))
            for i in range(5)
        ]
        results = compute_penalties_batch(obs, date(2026, 6, 3))
        assert len(results) == 5
        for r in results:
            assert r.total_fail_days == 2

    def test_batch_empty_list(self):
        assert compute_penalties_batch([], date(2026, 6, 3)) == []


class TestAggregateByCounterparty:
    def test_single_counterparty(self):
        ob1 = _make_obligation(obligation_id="OB-1", counterparty_id="BRK-001")
        ob2 = _make_obligation(obligation_id="OB-2", counterparty_id="BRK-001")
        p1 = compute_penalty(ob1, date(2026, 6, 1), date(2026, 6, 3))
        p2 = compute_penalty(ob2, date(2026, 6, 1), date(2026, 6, 5))
        agg = aggregate_by_counterparty([p1, p2])
        assert len(agg) == 1
        assert agg["BRK-001"]["fail_count"] == 2

    def test_multiple_counterparties(self):
        ob1 = _make_obligation(obligation_id="OB-1", counterparty_id="BRK-001")
        ob2 = _make_obligation(obligation_id="OB-2", counterparty_id="BRK-002")
        p1 = compute_penalty(ob1, date(2026, 6, 1), date(2026, 6, 3))
        p2 = compute_penalty(ob2, date(2026, 6, 1), date(2026, 6, 10))
        agg = aggregate_by_counterparty([p1, p2])
        assert len(agg) == 2
        assert agg["BRK-002"]["by_tier"]["CRITICAL"] == 1


class TestPenaltySummary:
    def test_empty_assessments(self):
        summary = get_penalty_summary([])
        assert summary["total_penalties"] == Decimal("0")
        assert summary["total_fails"] == 0

    def test_summary_counts(self):
        ob1 = _make_obligation(obligation_id="OB-1")
        ob2 = _make_obligation(obligation_id="OB-2", net_direction=NetDirection.PAY_OUT)
        p1 = compute_penalty(ob1, date(2026, 6, 1), date(2026, 6, 3))
        p2 = compute_penalty(ob2, date(2026, 6, 1), date(2026, 6, 3))
        summary = get_penalty_summary([p1, p2])
        assert summary["total_fails"] == 2
        assert summary["by_direction"]["DELIVER"] == 1
        assert summary["by_direction"]["RECEIVE"] == 1
