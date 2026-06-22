"""Stress tests for position reconciliation module."""

import uuid
import pytest
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import Base, CustodianHolding, Obligation, PositionRecord
from src.models.enums import (
    CounterpartyType, Exchange, NetDirection,
    ObligationStage, ObligationStatus, SettlementCycle,
)
from src.reconciliation.position_recon import (
    derive_positions,
    get_recon_summary,
    reconcile_positions,
)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _add_obligation(
    session,
    counterparty_id="BRK-001",
    isin="INE002A01018",
    net_quantity=100,
    net_direction=NetDirection.PAY_OUT,
    status=ObligationStatus.SETTLED,
    settlement_date=date(2026, 6, 15),
) -> Obligation:
    ob = Obligation(
        obligation_id=str(uuid.uuid4()),
        isin=isin,
        security_name="TEST",
        net_quantity=net_quantity,
        net_direction=net_direction,
        vwap_price=Decimal("2900.00"),
        net_value=Decimal(str(net_quantity * 2900)),
        settlement_date=settlement_date,
        settlement_cycle=SettlementCycle.T1,
        counterparty_id=counterparty_id,
        counterparty_type=CounterpartyType.BROKER,
        exchange=Exchange.NSE,
        obligation_stage=ObligationStage.FINAL,
        status=status,
        source_trade_ids='["T1"]',
    )
    session.add(ob)
    session.commit()
    return ob


def _add_holding(
    session,
    counterparty_id="BRK-001",
    isin="INE002A01018",
    quantity=100,
    statement_date=date(2026, 6, 15),
):
    h = CustodianHolding(
        holding_id=str(uuid.uuid4()),
        counterparty_id=counterparty_id,
        isin=isin,
        quantity=quantity,
        statement_date=statement_date,
    )
    session.add(h)
    session.commit()
    return h


class TestDerivePositions:
    def test_pay_out_adds_quantity(self, db_session):
        _add_obligation(db_session, net_direction=NetDirection.PAY_OUT, net_quantity=100)
        positions = derive_positions(db_session, date(2026, 6, 15))
        assert len(positions) == 1
        assert positions[0].quantity == 100

    def test_pay_in_subtracts_quantity(self, db_session):
        _add_obligation(db_session, net_direction=NetDirection.PAY_IN, net_quantity=100)
        positions = derive_positions(db_session, date(2026, 6, 15))
        assert len(positions) == 1
        assert positions[0].quantity == -100

    def test_netting_to_zero_excluded(self, db_session):
        _add_obligation(db_session, net_direction=NetDirection.PAY_OUT, net_quantity=100)
        _add_obligation(db_session, net_direction=NetDirection.PAY_IN, net_quantity=100)
        positions = derive_positions(db_session, date(2026, 6, 15))
        assert len(positions) == 0

    def test_multiple_isins(self, db_session):
        _add_obligation(db_session, isin="INE001A01018", net_quantity=100)
        _add_obligation(db_session, isin="INE002A01018", net_quantity=200)
        positions = derive_positions(db_session, date(2026, 6, 15))
        assert len(positions) == 2

    def test_ignores_non_settled(self, db_session):
        _add_obligation(db_session, status=ObligationStatus.PENDING)
        _add_obligation(db_session, status=ObligationStatus.FAILED)
        positions = derive_positions(db_session, date(2026, 6, 15))
        assert len(positions) == 0

    def test_positions_persisted(self, db_session):
        _add_obligation(db_session, net_quantity=100)
        derive_positions(db_session, date(2026, 6, 15))
        stored = db_session.query(PositionRecord).all()
        assert len(stored) == 1

    def test_future_date_excluded(self, db_session):
        _add_obligation(db_session, settlement_date=date(2026, 6, 20))
        positions = derive_positions(db_session, date(2026, 6, 15))
        assert len(positions) == 0


class TestReconcilePositions:
    def test_perfect_match(self, db_session):
        _add_obligation(db_session, net_direction=NetDirection.PAY_OUT, net_quantity=100)
        _add_holding(db_session, quantity=100)
        results = reconcile_positions(db_session, date(2026, 6, 15))
        assert len(results) == 1
        assert results[0].is_reconciled is True
        assert results[0].difference == 0

    def test_quantity_mismatch(self, db_session):
        _add_obligation(db_session, net_direction=NetDirection.PAY_OUT, net_quantity=100)
        _add_holding(db_session, quantity=90)
        results = reconcile_positions(db_session, date(2026, 6, 15))
        assert len(results) == 1
        assert results[0].is_reconciled is False
        assert results[0].difference == 10

    def test_missing_custodian_holding(self, db_session):
        _add_obligation(db_session, net_direction=NetDirection.PAY_OUT, net_quantity=100)
        results = reconcile_positions(db_session, date(2026, 6, 15))
        assert len(results) == 1
        assert results[0].custodian_quantity == 0
        assert results[0].is_reconciled is False

    def test_extra_custodian_holding(self, db_session):
        _add_holding(db_session, counterparty_id="BRK-002", quantity=200)
        results = reconcile_positions(db_session, date(2026, 6, 15))
        assert len(results) == 1
        assert results[0].internal_quantity == 0
        assert results[0].custodian_quantity == 200
        assert results[0].is_reconciled is False

    def test_multiple_counterparties(self, db_session):
        _add_obligation(db_session, counterparty_id="BRK-001", net_quantity=100)
        _add_obligation(db_session, counterparty_id="BRK-002", net_quantity=200)
        _add_holding(db_session, counterparty_id="BRK-001", quantity=100)
        _add_holding(db_session, counterparty_id="BRK-002", quantity=200)
        results = reconcile_positions(db_session, date(2026, 6, 15))
        assert len(results) == 2
        assert all(r.is_reconciled for r in results)


class TestReconSummary:
    def test_all_reconciled(self):
        from src.reconciliation.position_recon import ReconResult
        results = [
            ReconResult("BRK-001", "INE001", date(2026, 6, 15), 100, 100, 0, True),
            ReconResult("BRK-002", "INE002", date(2026, 6, 15), 200, 200, 0, True),
        ]
        summary = get_recon_summary(results)
        assert summary["total_positions"] == 2
        assert summary["reconciled"] == 2
        assert summary["unreconciled"] == 0
        assert summary["recon_rate"] == 100.0

    def test_partial_reconciliation(self):
        from src.reconciliation.position_recon import ReconResult
        results = [
            ReconResult("BRK-001", "INE001", date(2026, 6, 15), 100, 100, 0, True),
            ReconResult("BRK-002", "INE002", date(2026, 6, 15), 200, 150, 50, False),
        ]
        summary = get_recon_summary(results)
        assert summary["reconciled"] == 1
        assert summary["unreconciled"] == 1
        assert summary["recon_rate"] == 50.0
        assert summary["total_absolute_difference"] == 50

    def test_empty_results(self):
        summary = get_recon_summary([])
        assert summary["total_positions"] == 0
        assert summary["recon_rate"] == 0
