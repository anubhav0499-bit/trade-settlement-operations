"""High-volume stress tests and end-to-end integration test."""

import time
import uuid
import pytest
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import (
    Base, CustodianHolding, SSIRecord, Trade,
)
from src.models.enums import (
    BuySell, CounterpartyType, Depository, Exchange,
    MatchStatus, ObligationStage, ObligationStatus,
    Segment, SettlementCycle, SourceSystem,
)
from src.netting.obligation_engine import compute_obligations
from src.matching.engine import match_obligations
from src.penalties.csdr_penalties import compute_penalties_batch
from src.reconciliation.position_recon import derive_positions, reconcile_positions
from src.ssi.golden_copy import validate_all_obligations
from src.risk.counterparty_scorecard import compute_scorecard
from src.liquidity.intraday_monitor import generate_intraday_report
from src.breaks.rules_engine import get_break_summary


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


COUNTERPARTIES = [f"BRK-{i:03d}" for i in range(1, 21)]
ISINS = [
    "INE002A01018", "INE009A01021", "INE467B01029", "INE062A01020",
    "INE154A01025", "INE040A01034", "INE585B01010", "INE090A01021",
    "INE526F01014", "INE020B01018",
]


def _bulk_insert_trades(session, count, source_system=SourceSystem.OMS):
    """Insert `count` trades with realistic distribution."""
    import random
    rng = random.Random(42)

    trades = []
    for i in range(count):
        cp = rng.choice(COUNTERPARTIES)
        isin = rng.choice(ISINS)
        qty = rng.randint(10, 1000)
        price = Decimal(str(round(rng.uniform(100, 5000), 2)))
        side = rng.choice([BuySell.BUY, BuySell.SELL])

        prefix = ""
        if source_system == SourceSystem.BROKER_CONFIRM:
            prefix = "BRK-"
        elif source_system == SourceSystem.CUSTODIAN_STATEMENT:
            prefix = "CUS-"

        trade = Trade(
            trade_id=f"{prefix}T-{i:06d}",
            isin=isin,
            security_name=f"SECURITY-{isin[-4:]}",
            quantity=qty,
            price=price,
            trade_date=date(2026, 6, 14),
            settlement_date=date(2026, 6, 15),
            settlement_cycle=SettlementCycle.T1,
            counterparty_id=cp,
            counterparty_type=CounterpartyType.BROKER,
            exchange=rng.choice([Exchange.NSE, Exchange.BSE]),
            buy_sell=side,
            currency="INR",
            source_system=source_system,
            segment=Segment.NORMAL,
        )
        trades.append(trade)

    session.bulk_save_objects(trades)
    session.commit()
    return trades


class TestHighVolumeNetting:
    def test_10k_trades_netting(self, db_session):
        """10,000 trades netted into obligations in under 10 seconds."""
        _bulk_insert_trades(db_session, 10_000)
        start = time.time()
        obligations = compute_obligations(
            db_session, SourceSystem.OMS, ObligationStage.FINAL
        )
        elapsed = time.time() - start

        assert len(obligations) > 0
        assert elapsed < 10.0, f"Netting took {elapsed:.1f}s — exceeds 10s budget"

        total_netted_qty = sum(o.net_quantity for o in obligations)
        assert total_netted_qty > 0

    def test_netting_deterministic(self, db_session):
        """Same input produces same output."""
        _bulk_insert_trades(db_session, 1000)
        obs1 = compute_obligations(db_session, SourceSystem.OMS, ObligationStage.FINAL)
        obs2 = compute_obligations(db_session, SourceSystem.OMS, ObligationStage.FINAL)
        assert len(obs1) == len(obs2)
        vals1 = sorted([(o.isin, o.counterparty_id, o.net_quantity) for o in obs1])
        vals2 = sorted([(o.isin, o.counterparty_id, o.net_quantity) for o in obs2])
        assert vals1 == vals2


class TestHighVolumeMatching:
    def test_matching_1k_obligations(self, db_session):
        """Match 1,000 obligation pairs."""
        _bulk_insert_trades(db_session, 5000, SourceSystem.OMS)
        _bulk_insert_trades(db_session, 5000, SourceSystem.BROKER_CONFIRM)

        internal = compute_obligations(
            db_session, SourceSystem.OMS, ObligationStage.FINAL
        )
        external = compute_obligations(
            db_session, SourceSystem.BROKER_CONFIRM, ObligationStage.FINAL
        )

        for ob in internal + external:
            ob.status = ObligationStatus.SSI_VALIDATED
            db_session.add(ob)
        db_session.commit()

        config = {"price_tolerance_pct": 1.0, "quantity_tolerance_abs": 0}
        start = time.time()
        results = match_obligations(internal, external, config)
        elapsed = time.time() - start

        assert len(results) > 0
        assert elapsed < 15.0, f"Matching took {elapsed:.1f}s — exceeds 15s budget"

        matched = sum(1 for r in results if r.status == MatchStatus.MATCHED)
        breaks = sum(1 for r in results if r.status == MatchStatus.BREAK)
        unmatched = sum(1 for r in results if r.status == MatchStatus.UNMATCHED)
        assert matched + breaks + unmatched == len(results)


class TestEndToEndIntegration:
    """End-to-end: trades → netting → SSI → matching → penalties → recon → scorecard."""

    def test_full_pipeline_subset(self, db_session):
        # 1. Insert trades from OMS and broker
        _bulk_insert_trades(db_session, 500, SourceSystem.OMS)
        _bulk_insert_trades(db_session, 500, SourceSystem.BROKER_CONFIRM)

        # 2. Netting
        internal_obs = compute_obligations(
            db_session, SourceSystem.OMS, ObligationStage.FINAL
        )
        broker_obs = compute_obligations(
            db_session, SourceSystem.BROKER_CONFIRM, ObligationStage.FINAL
        )
        assert len(internal_obs) > 0
        assert len(broker_obs) > 0

        for ob in internal_obs + broker_obs:
            db_session.add(ob)
        db_session.commit()

        # 3. SSI validation (add SSI records for all counterparties)
        for cp in COUNTERPARTIES:
            ssi = SSIRecord(
                ssi_id=str(uuid.uuid4()),
                counterparty_id=cp,
                settlement_bank="HDFC Bank",
                bank_account="HDFC001234567",
                dp_id="IN300001",
                dp_account="1234567890123456",
                depository=Depository.NSDL,
                effective_from=date(2026, 1, 1),
                is_active=True,
            )
            db_session.add(ssi)
        db_session.commit()

        valid_obs, ssi_breaks = validate_all_obligations(db_session, internal_obs)
        assert len(valid_obs) + len(ssi_breaks) == len(internal_obs)

        # 4. Matching
        config = {"price_tolerance_pct": 1.0, "quantity_tolerance_abs": 0}
        match_obligations(valid_obs, broker_obs, config)

        # 5. Settle matched obligations
        for ob in valid_obs:
            if ob.match_status == MatchStatus.MATCHED:
                ob.status = ObligationStatus.SETTLED
        db_session.commit()

        # 6. Penalties on failed/unmatched
        failed_obs = [
            ob for ob in valid_obs
            if ob.match_status != MatchStatus.MATCHED
        ]
        if failed_obs:
            penalty_input = [(ob, date(2026, 6, 15)) for ob in failed_obs]
            penalties = compute_penalties_batch(penalty_input, date(2026, 6, 18))
            assert len(penalties) == len(failed_obs)

        # 7. Position derivation and reconciliation
        positions = derive_positions(db_session, date(2026, 6, 15))

        # Add matching custodian holdings for settled positions
        for pos in positions:
            h = CustodianHolding(
                holding_id=str(uuid.uuid4()),
                counterparty_id=pos.counterparty_id,
                isin=pos.isin,
                quantity=pos.quantity,
                statement_date=date(2026, 6, 15),
            )
            db_session.add(h)
        db_session.commit()

        reconcile_positions(db_session, date(2026, 6, 15))

        # 8. Scorecard
        sc = compute_scorecard(db_session, COUNTERPARTIES[0])
        assert sc.composite_score >= 0
        assert sc.letter_grade in ("A", "B", "C", "D", "F")

        # 9. Liquidity report
        report = generate_intraday_report(
            db_session, date(2026, 6, 15), datetime(2026, 6, 15, 14, 0)
        )
        assert report.report_date == date(2026, 6, 15)

        # 10. Break summary
        summary = get_break_summary(db_session)
        assert summary["total"] >= 0
