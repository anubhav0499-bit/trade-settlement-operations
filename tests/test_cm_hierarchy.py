"""Tests for multi-CM hierarchy and obligation aggregation."""

import pytest
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import Base, Obligation
from src.models.enums import (
    CMType,
    ConfirmationStatus,
    CounterpartyType,
    Exchange,
    MatchStatus,
    NetDirection,
    ObligationStage,
    ObligationStatus,
    ProductSegment,
    SettlementCycle,
)
from src.cm_hierarchy.hierarchy import (
    aggregate_obligations,
    get_all_descendant_ids,
    get_sub_tms,
    register_clearing_member,
)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _add_obligation(session, cp_id, value, settlement_date):
    session.add(Obligation(
        obligation_id=f"OB-{cp_id}-{value}",
        isin="INE001A01036",
        security_name="TestCo",
        net_quantity=10,
        net_direction=NetDirection.PAY_IN,
        vwap_price=Decimal("100"),
        net_value=Decimal(str(value)),
        settlement_date=settlement_date,
        settlement_cycle=SettlementCycle.T1,
        counterparty_id=cp_id,
        counterparty_type=CounterpartyType.BROKER,
        exchange=Exchange.NSE,
        obligation_stage=ObligationStage.FINAL,
        product_segment=ProductSegment.EQUITY_CASH,
        status=ObligationStatus.PENDING,
        match_status=MatchStatus.UNMATCHED,
        confirmation_status=ConfirmationStatus.NOT_REQUIRED,
        source_trade_ids="[]",
    ))
    session.commit()


class TestRegisterAndHierarchy:
    def test_register_and_get_sub_tms(self, db_session):
        register_clearing_member(db_session, "TM-CM-1", "Parent CM", CMType.TM_CM, Decimal("30000000"), Decimal("5000000"))
        register_clearing_member(db_session, "SUB-1", "Sub TM A", CMType.TM_CM, Decimal("1000000"), Decimal("0"), parent_cm_id="TM-CM-1")
        register_clearing_member(db_session, "SUB-2", "Sub TM B", CMType.TM_CM, Decimal("1000000"), Decimal("0"), parent_cm_id="TM-CM-1")

        subs = get_sub_tms(db_session, "TM-CM-1")
        assert {s.cm_id for s in subs} == {"SUB-1", "SUB-2"}

    def test_descendant_ids_recursive(self, db_session):
        register_clearing_member(db_session, "TM-CM-1", "Parent", CMType.TM_CM, Decimal("30000000"), Decimal("5000000"))
        register_clearing_member(db_session, "SUB-1", "Sub A", CMType.TM_CM, Decimal("1000000"), Decimal("0"), parent_cm_id="TM-CM-1")
        register_clearing_member(db_session, "SUB-1-1", "Grandchild", CMType.TM_CM, Decimal("500000"), Decimal("0"), parent_cm_id="SUB-1")

        descendants = get_all_descendant_ids(db_session, "TM-CM-1")
        assert set(descendants) == {"TM-CM-1", "SUB-1", "SUB-1-1"}


class TestAggregateObligations:
    def test_aggregates_across_hierarchy(self, db_session):
        register_clearing_member(db_session, "TM-CM-1", "Parent", CMType.TM_CM, Decimal("30000000"), Decimal("5000000"))
        register_clearing_member(db_session, "SUB-1", "Sub A", CMType.TM_CM, Decimal("1000000"), Decimal("0"), parent_cm_id="TM-CM-1")

        d = date(2026, 6, 25)
        _add_obligation(db_session, "TM-CM-1", 1000, d)
        _add_obligation(db_session, "SUB-1", 500, d)

        result = aggregate_obligations(db_session, "TM-CM-1", d)
        assert result["member_count"] == 2
        assert result["obligation_count"] == 2
        assert result["total_value"] == Decimal("1500")

    def test_excludes_unrelated_counterparty(self, db_session):
        register_clearing_member(db_session, "TM-CM-1", "Parent", CMType.TM_CM, Decimal("30000000"), Decimal("5000000"))
        d = date(2026, 6, 25)
        _add_obligation(db_session, "TM-CM-1", 1000, d)
        _add_obligation(db_session, "OTHER-CM", 9999, d)

        result = aggregate_obligations(db_session, "TM-CM-1", d)
        assert result["total_value"] == Decimal("1000")
