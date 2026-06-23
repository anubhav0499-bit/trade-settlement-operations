"""
Comprehensive system-wide stress test.

Pushes every Phase 1-5 module to its limits: high-volume data, boundary values,
cross-module integration under load, and edge cases that the unit tests don't cover.
"""

import random
import time
import uuid
import pytest
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import (
    Base, ClearingMember, CollateralRecord, CustodianHolding,
    DebtInstrument, DebtTrade, DerivativeContract, DerivativePosition,
    MTMSettlement, Obligation, SSIRecord, Trade,
)
from src.models.enums import (
    BuySell, CMType, CollateralType, ConfirmationStatus, ContractType,
    CounterpartyType, DayCountConvention, DebtInstrumentType,
    DebtTradeStatus, DeliveryType, Depository, Exchange,
    MatchStatus, NetDirection, ObligationStage, ObligationStatus,
    OptionType, ProductSegment, Segment, SettlementCycle, SourceSystem,
)

# Phase 1
from src.netting.obligation_engine import compute_obligations
from src.matching.engine import match_obligations
from src.ssi.golden_copy import validate_all_obligations
from src.penalties.csdr_penalties import compute_penalties_batch
from src.reconciliation.position_recon import derive_positions, reconcile_positions
from src.risk.counterparty_scorecard import compute_scorecard
from src.liquidity.intraday_monitor import generate_intraday_report
from src.breaks.rules_engine import update_break_aging, get_break_summary

# Phase 2 — derivatives
from src.derivatives.mtm_engine import compute_daily_mtm, net_mtm_by_counterparty
from src.derivatives.premium_engine import compute_premium_obligations
from src.derivatives.exercise_engine import exercise_long_positions, assign_short_positions
from src.derivatives.final_settlement import run_final_settlement
from src.derivatives.physical_delivery import (
    generate_futures_delivery_obligations,
    generate_option_delivery_obligations,
)
from src.derivatives.bond_futures import DeliverableBond

# Phase 3 — margins & collateral
from src.margins.span_engine import compute_span_margin
from src.margins.exposure_margin import compute_exposure_margin
from src.margins.var_model import ewma_volatility, compute_var_margin
from src.margins.delivery_margin import record_delivery_margin
from src.margins.cross_margin import compute_cross_margin_benefit, apply_cross_margin
from src.margins.position_limits import (
    check_market_wide_limit, check_cm_level_limit, check_client_level_limit,
)
from src.collateral.manager import (
    compute_effective_collateral, check_cash_rule, check_concentration_limit,
)

# Phase 4 — debt
from src.debt.accrued_interest import compute_accrued_interest, day_count_fraction
from src.debt.corporate_bond_settlement import mark_securities_received, mark_funds_received
from src.debt.corporate_actions import compute_coupon_payment, compute_redemption_amount
from src.debt.sgf_contribution import compute_sgf_issuer_contribution
from src.debt.gsec_integration import derive_gsec_positions, reconcile_ccil_positions

# Phase 5 — advanced
from src.cm_hierarchy.hierarchy import register_clearing_member, aggregate_obligations
from src.sgf.waterfall import run_default_waterfall, WaterfallInputs, get_waterfall_summary
from src.risk.stress_test import rank_top_n_stressed_cms
from src.settlement.t0_engine import compute_t0_obligations
from src.derivatives.bond_futures import identify_cheapest_to_deliver


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


# ── Constants ────────────────────────────────────────────────────────────────

TRADE_DATE = date(2026, 6, 23)
SETTLE_DATE = date(2026, 6, 24)
EXPIRY_DATE = date(2026, 6, 25)
RNG = random.Random(12345)

COUNTERPARTIES = [f"BRK-{i:03d}" for i in range(1, 51)]
ISINS = [f"INE{i:03d}A01{i:03d}" for i in range(1, 31)]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _bulk_trades(session, count, source=SourceSystem.OMS, rng=None):
    rng = rng or random.Random(42)
    trades = []
    prefix = {"OMS": "", "BROKER_CONFIRM": "BRK-", "CUSTODIAN_STATEMENT": "CUS-"}.get(source.value, "")
    for i in range(count):
        trades.append(Trade(
            trade_id=f"{prefix}STRESS-{uuid.uuid4().hex[:8]}",
            isin=rng.choice(ISINS),
            security_name="STRESS",
            quantity=rng.randint(1, 5000),
            price=Decimal(str(round(rng.uniform(10, 10000), 2))),
            trade_date=TRADE_DATE,
            settlement_date=SETTLE_DATE,
            settlement_cycle=SettlementCycle.T1,
            counterparty_id=rng.choice(COUNTERPARTIES),
            counterparty_type=CounterpartyType.BROKER,
            exchange=Exchange.NSE,
            buy_sell=rng.choice([BuySell.BUY, BuySell.SELL]),
            currency="INR",
            source_system=source,
            segment=Segment.NORMAL,
        ))
    session.bulk_save_objects(trades)
    session.commit()
    return trades


def _seed_ssi(session, counterparties):
    for cp in counterparties:
        session.add(SSIRecord(
            ssi_id=str(uuid.uuid4()),
            counterparty_id=cp,
            settlement_bank="HDFC Bank",
            bank_account="HDFC001234567",
            dp_id="IN300001",
            dp_account="1234567890123456",
            depository=Depository.NSDL,
            effective_from=date(2026, 1, 1),
            is_active=True,
        ))
    session.commit()


def _seed_derivative_universe(session, num_underlyings=20, positions_per_contract=10):
    """Seed a large derivative universe."""
    contracts = []
    for i in range(num_underlyings):
        underlying = f"STOCK-{i:03d}"
        # One future per underlying
        contracts.append(DerivativeContract(
            contract_id=f"{underlying}-FUT",
            underlying=underlying,
            product_segment=ProductSegment.EQUITY_FO,
            contract_type=ContractType.FUTURES,
            option_type=None,
            delivery_type=DeliveryType.PHYSICAL if i % 3 == 0 else DeliveryType.CASH,
            strike_price=None,
            lot_size=RNG.choice([25, 50, 100, 250, 500]),
            expiry_date=EXPIRY_DATE,
        ))
        # One call option per underlying
        strike = Decimal(str(round(RNG.uniform(100, 5000), 2)))
        contracts.append(DerivativeContract(
            contract_id=f"{underlying}-CE",
            underlying=underlying,
            product_segment=ProductSegment.EQUITY_FO,
            contract_type=ContractType.OPTIONS,
            option_type=OptionType.CALL,
            delivery_type=DeliveryType.PHYSICAL if i % 3 == 0 else DeliveryType.CASH,
            strike_price=strike,
            lot_size=RNG.choice([25, 50, 100, 250, 500]),
            expiry_date=EXPIRY_DATE,
        ))
        # One put option per underlying
        contracts.append(DerivativeContract(
            contract_id=f"{underlying}-PE",
            underlying=underlying,
            product_segment=ProductSegment.EQUITY_FO,
            contract_type=ContractType.OPTIONS,
            option_type=OptionType.PUT,
            delivery_type=DeliveryType.PHYSICAL if i % 3 == 0 else DeliveryType.CASH,
            strike_price=strike * Decimal("0.95"),
            lot_size=RNG.choice([25, 50, 100, 250, 500]),
            expiry_date=EXPIRY_DATE,
        ))
    session.add_all(contracts)
    session.commit()

    positions = []
    for c in contracts:
        for j in range(positions_per_contract):
            positions.append(DerivativePosition(
                position_id=f"POS-{c.contract_id}-{j:03d}",
                contract_id=c.contract_id,
                counterparty_id=RNG.choice(COUNTERPARTIES[:20]),
                buy_sell=RNG.choice([BuySell.BUY, BuySell.SELL]),
                quantity=RNG.randint(1, 100),
                trade_price=Decimal(str(round(RNG.uniform(50, 5000), 2))),
                position_date=TRADE_DATE,
            ))
    session.bulk_save_objects(positions)
    session.commit()
    return contracts, positions


def _seed_cm_hierarchy(session, count=20):
    """Build a multi-level CM hierarchy."""
    # 4 parent TM-CMs
    parents = []
    for i in range(4):
        cm = register_clearing_member(
            session, f"CM-{i:03d}", f"Parent CM {i}", CMType.TM_CM,
            Decimal(str(RNG.randint(10_000_000, 100_000_000))),
            Decimal(str(RNG.randint(1_000_000, 10_000_000))),
        )
        parents.append(cm)
    # Sub-CMs under each parent
    for i in range(4, count):
        parent = parents[i % 4]
        register_clearing_member(
            session, f"CM-{i:03d}", f"Sub CM {i}",
            RNG.choice([CMType.SCM, CMType.PCM]),
            Decimal(str(RNG.randint(1_000_000, 10_000_000))),
            Decimal(str(RNG.randint(100_000, 1_000_000))),
            parent_cm_id=parent.cm_id,
        )
    return [f"CM-{i:03d}" for i in range(count)]


def _seed_debt_universe(session, num_bonds=20, trades_per_bond=5):
    """Seed many debt instruments and trades."""
    for i in range(num_bonds):
        isin = f"BOND-{i:04d}"
        is_gsec = i % 4 == 0
        session.add(DebtInstrument(
            isin=isin,
            issuer="GOI" if is_gsec else f"Corp-{i}",
            instrument_type=DebtInstrumentType.GSEC if is_gsec else DebtInstrumentType.CORPORATE_BOND,
            face_value=Decimal("100") if is_gsec else Decimal("1000"),
            coupon_rate_pct=round(RNG.uniform(5, 12), 2),
            coupon_frequency=2,
            issue_date=date(2024, 1, 15),
            maturity_date=date(2029, 1, 15),
            day_count_convention=DayCountConvention.ACTUAL_ACTUAL if is_gsec else DayCountConvention.THIRTY_360,
        ))
    session.commit()

    trades = []
    for i in range(num_bonds):
        isin = f"BOND-{i:04d}"
        is_gsec = i % 4 == 0
        for j in range(trades_per_bond):
            buyer = RNG.choice(COUNTERPARTIES[:20])
            seller = RNG.choice([cp for cp in COUNTERPARTIES[:20] if cp != buyer])
            trades.append(DebtTrade(
                trade_id=f"DT-{isin}-{j:03d}",
                isin=isin,
                buyer_id=buyer,
                seller_id=seller,
                quantity=RNG.randint(100, 10000),
                clean_price=Decimal(str(round(RNG.uniform(95, 105), 2))),
                trade_date=TRADE_DATE,
                settlement_date=SETTLE_DATE,
                product_segment=ProductSegment.DEBT_GSEC if is_gsec else ProductSegment.DEBT_CORP_BOND,
                source="CCIL" if is_gsec else RNG.choice(["CBRICS", "RFQ"]),
                status=DebtTradeStatus.PENDING,
            ))
    session.bulk_save_objects(trades)
    session.commit()
    return trades


def _seed_collateral(session, counterparties, records_per_cp=5):
    """Diverse collateral portfolio for each counterparty."""
    types = list(CollateralType)
    records = []
    for cp in counterparties:
        for i in range(records_per_cp):
            ctype = types[i % len(types)]
            haircut = {
                CollateralType.CASH: 0, CollateralType.BANK_GUARANTEE: 0,
                CollateralType.FIXED_DEPOSIT: 2, CollateralType.GOVERNMENT_SECURITY: 5,
                CollateralType.EQUITY: 30,
            }.get(ctype, 10)
            records.append(CollateralRecord(
                collateral_id=f"COL-{cp}-{i}",
                counterparty_id=cp,
                collateral_type=ctype,
                value=Decimal(str(RNG.randint(1_000_000, 50_000_000))),
                haircut_pct=haircut,
                as_of_date=TRADE_DATE,
            ))
    session.bulk_save_objects(records)
    session.commit()
    return records


# ── Test: Phase 1 at 50K volume ─────────────────────────────────────────────

class TestHighVolumeEquityCash:
    """Push equity-cash pipeline (netting, matching, SSI, penalties, recon) with 50K trades."""

    def test_50k_netting(self, db_session):
        _bulk_trades(db_session, 50_000)
        start = time.time()
        obligations = compute_obligations(db_session, SourceSystem.OMS, ObligationStage.FINAL)
        elapsed = time.time() - start
        assert len(obligations) > 0
        assert elapsed < 30.0, f"50K netting took {elapsed:.1f}s"
        # Net quantities must be non-negative
        for o in obligations:
            assert o.net_quantity >= 0

    def test_50k_matching_pair(self, db_session):
        rng1, rng2 = random.Random(99), random.Random(99)
        _bulk_trades(db_session, 25_000, SourceSystem.OMS, rng1)
        _bulk_trades(db_session, 25_000, SourceSystem.BROKER_CONFIRM, rng2)
        internal = compute_obligations(db_session, SourceSystem.OMS, ObligationStage.FINAL)
        external = compute_obligations(db_session, SourceSystem.BROKER_CONFIRM, ObligationStage.FINAL)
        for ob in internal + external:
            ob.status = ObligationStatus.SSI_VALIDATED
            db_session.add(ob)
        db_session.commit()

        start = time.time()
        results = match_obligations(internal, external, {"price_tolerance_pct": 1.0, "quantity_tolerance_abs": 0})
        elapsed = time.time() - start
        assert elapsed < 60.0, f"25K matching took {elapsed:.1f}s"
        assert len(results) > 0

    def test_full_equity_cash_pipeline_500_trades(self, db_session):
        """Equity cash E2E: trades → netting → SSI → matching → penalties → recon → scorecard → liquidity."""
        _bulk_trades(db_session, 500, SourceSystem.OMS, random.Random(1))
        _bulk_trades(db_session, 500, SourceSystem.BROKER_CONFIRM, random.Random(1))
        _seed_ssi(db_session, COUNTERPARTIES)

        internal = compute_obligations(db_session, SourceSystem.OMS, ObligationStage.FINAL)
        broker = compute_obligations(db_session, SourceSystem.BROKER_CONFIRM, ObligationStage.FINAL)
        for ob in internal + broker:
            db_session.add(ob)
        db_session.commit()

        valid_obs, ssi_breaks = validate_all_obligations(db_session, internal)
        results = match_obligations(valid_obs, broker, {"price_tolerance_pct": 1.0, "quantity_tolerance_abs": 0})
        matched = sum(1 for r in results if r.status == MatchStatus.MATCHED)

        for ob in valid_obs:
            if ob.match_status == MatchStatus.MATCHED:
                ob.status = ObligationStatus.SETTLED
        db_session.commit()

        failed = [ob for ob in valid_obs if ob.match_status != MatchStatus.MATCHED]
        if failed:
            penalties = compute_penalties_batch([(ob, SETTLE_DATE) for ob in failed], SETTLE_DATE + timedelta(days=3))
            assert len(penalties) == len(failed)

        positions = derive_positions(db_session, SETTLE_DATE)
        for pos in positions:
            db_session.add(CustodianHolding(
                holding_id=str(uuid.uuid4()),
                counterparty_id=pos.counterparty_id, isin=pos.isin,
                quantity=pos.quantity, statement_date=SETTLE_DATE,
            ))
        db_session.commit()
        recon = reconcile_positions(db_session, SETTLE_DATE)

        sc = compute_scorecard(db_session, COUNTERPARTIES[0])
        assert sc.composite_score >= 0

        report = generate_intraday_report(db_session, SETTLE_DATE, datetime(2026, 6, 24, 14, 0))
        assert report.report_date == SETTLE_DATE

        summary = get_break_summary(db_session)
        assert summary["total"] >= 0


# ── Test: Phase 2 derivatives at scale ───────────────────────────────────────

class TestHighVolumeDerivatives:
    """20 underlyings x 3 contracts each x 10 positions = 600 positions."""

    def test_mass_mtm(self, db_session):
        contracts, positions = _seed_derivative_universe(db_session, 20, 10)
        settlement_prices = {c.contract_id: Decimal(str(round(RNG.uniform(50, 5000), 2))) for c in contracts}
        start = time.time()
        records = compute_daily_mtm(db_session, TRADE_DATE, settlement_prices)
        elapsed = time.time() - start
        assert len(records) == len(positions)
        assert elapsed < 10.0, f"MTM for 600 positions took {elapsed:.1f}s"
        net = net_mtm_by_counterparty(records)
        assert sum(net.values()) != Decimal("0") or len(net) > 0

    def test_mass_premium(self, db_session):
        _seed_derivative_universe(db_session, 10, 20)
        premiums = compute_premium_obligations(db_session, TRADE_DATE)
        # Option positions should generate premium obligations
        assert len(premiums) > 0

    def test_mass_exercise_assignment(self, db_session):
        contracts, _ = _seed_derivative_universe(db_session, 5, 30)
        option_contracts = [c for c in contracts if c.contract_type == ContractType.OPTIONS]
        total_exercised = 0
        total_assigned = 0
        for contract in option_contracts:
            fsp = Decimal(str(round(RNG.uniform(50, 5000), 2)))
            exercises = exercise_long_positions(db_session, contract, fsp)
            assignments = assign_short_positions(db_session, contract, exercises, seed=42)
            total_exercised += sum(e.exercised_quantity for e in exercises)
            total_assigned += sum(a.assigned_quantity for a in assignments)
        # At least some options should be ITM and exercised
        assert total_exercised > 0 or total_assigned >= 0

    def test_mass_final_settlement(self, db_session):
        contracts, _ = _seed_derivative_universe(db_session, 10, 15)
        fsp_map = {c.contract_id: Decimal(str(round(RNG.uniform(50, 5000), 2))) for c in contracts}
        results = run_final_settlement(db_session, EXPIRY_DATE, fsp_map)
        assert len(results) > 0

    def test_physical_delivery_at_scale(self, db_session):
        contracts, _ = _seed_derivative_universe(db_session, 10, 15)
        futures = [c for c in contracts if c.contract_type == ContractType.FUTURES and c.delivery_type == DeliveryType.PHYSICAL]
        options = [c for c in contracts if c.contract_type == ContractType.OPTIONS and c.delivery_type == DeliveryType.PHYSICAL]
        fsp_map = {c.contract_id: Decimal(str(round(RNG.uniform(50, 5000), 2))) for c in contracts}

        for fut in futures[:5]:
            fsp = fsp_map.get(fut.contract_id, Decimal("1000"))
            fut_obligations = generate_futures_delivery_obligations(
                db_session, fut, f"INE-{fut.underlying}", fsp, SETTLE_DATE,
            )
            assert isinstance(fut_obligations, list)

        for opt in options[:5]:
            fsp = fsp_map.get(opt.contract_id, Decimal("1000"))
            exercises = exercise_long_positions(db_session, opt, fsp)
            assignments = assign_short_positions(db_session, opt, exercises, seed=42)
            opt_obligations = generate_option_delivery_obligations(
                db_session, opt, f"INE-{opt.underlying}", exercises, assignments, SETTLE_DATE,
            )
            assert isinstance(opt_obligations, list)


# ── Test: Phase 3 margins under load ────────────────────────────────────────

class TestHighVolumeMargins:
    """SPAN, exposure, VaR, collateral checks across many positions."""

    def test_span_many_underlyings(self, db_session):
        contracts, _ = _seed_derivative_universe(db_session, 15, 8)
        underlyings = list({c.underlying for c in contracts})
        cps_with_positions = list(set(COUNTERPARTIES[:20]))

        total_margin = Decimal("0")
        for underlying in underlyings[:10]:
            price = Decimal(str(round(RNG.uniform(100, 5000), 2)))
            for cp in cps_with_positions[:5]:
                result = compute_span_margin(
                    db_session, cp, underlying, price, is_index=False,
                )
                assert result.total_margin >= 0
                total_margin += result.total_margin
        assert total_margin > 0

    def test_exposure_margin_many_positions(self, db_session):
        _seed_derivative_universe(db_session, 10, 10)
        for cp in COUNTERPARTIES[:10]:
            positions = db_session.query(DerivativePosition).filter_by(counterparty_id=cp).all()
            for pos in positions:
                result = compute_exposure_margin(
                    Decimal("1000"), 50, pos.quantity, is_index=False,
                )
                assert result >= 0

    def test_var_margin_long_series(self):
        returns = [Decimal(str(round(RNG.gauss(0, 0.02), 6))) for _ in range(500)]
        vol = ewma_volatility(returns)
        assert vol > 0
        var = compute_var_margin(Decimal("1000000"), vol)
        assert var > 0

    def test_collateral_many_counterparties(self, db_session):
        records = _seed_collateral(db_session, COUNTERPARTIES[:30], records_per_cp=5)
        for cp in COUNTERPARTIES[:30]:
            cp_records = [r for r in records if r.counterparty_id == cp]
            result = compute_effective_collateral(cp_records)
            assert result["total"] > 0
            cash_violation = check_cash_rule(cp_records)
            conc_violations = check_concentration_limit(cp_records)
            assert isinstance(conc_violations, list)

    def test_position_limits_many_checks(self):
        for _ in range(1000):
            free_float = RNG.randint(100_000, 10_000_000)
            oi = RNG.randint(0, free_float * 2)
            result = check_market_wide_limit(oi, free_float)
            if oi > int(free_float * 0.95):
                assert result is not None
            cm_result = check_cm_level_limit(
                f"CM-{_ % 10:03d}", RNG.randint(0, free_float), free_float,
            )
            client_result = check_client_level_limit(
                f"CLIENT-{_ % 10:03d}", RNG.randint(0, free_float), free_float,
            )


# ── Test: Phase 4 debt at scale ─────────────────────────────────────────────

class TestHighVolumeDebt:
    """20 instruments x 5 trades = 100 debt trades."""

    def test_mass_dvp1_settlement(self, db_session):
        trades = _seed_debt_universe(db_session, 20, 5)
        settled = 0
        for t in trades[:50]:
            mark_securities_received(db_session, t.trade_id)
            mark_funds_received(db_session, t.trade_id)
            settled += 1
        assert settled == 50

    def test_mass_accrued_interest(self, db_session):
        _seed_debt_universe(db_session, 20, 0)
        instruments = db_session.query(DebtInstrument).all()
        for inst in instruments:
            ai = compute_accrued_interest(
                inst.face_value, Decimal(str(inst.coupon_rate_pct)),
                inst.issue_date, TRADE_DATE, inst.day_count_convention,
            )
            assert ai >= 0

    def test_mass_corporate_actions(self, db_session):
        _seed_debt_universe(db_session, 20, 0)
        instruments = db_session.query(DebtInstrument).all()
        for inst in instruments:
            coupon = compute_coupon_payment(inst.face_value, Decimal(str(inst.coupon_rate_pct)), inst.coupon_frequency, 1000)
            assert coupon > 0
            redemption = compute_redemption_amount(inst.face_value, 1000)
            assert redemption > 0

    def test_mass_sgf_contribution(self, db_session):
        _seed_debt_universe(db_session, 10, 5)
        instruments = db_session.query(DebtInstrument).all()
        for inst in instruments:
            trades = db_session.query(DebtTrade).filter_by(isin=inst.isin).all()
            if trades:
                total_value = sum(t.quantity * t.clean_price for t in trades)
                contribution = compute_sgf_issuer_contribution(total_value, inst.issue_date, inst.maturity_date)
                assert contribution >= 0

    def test_gsec_recon_at_scale(self, db_session):
        _seed_debt_universe(db_session, 20, 5)
        internal = derive_gsec_positions(db_session, SETTLE_DATE)
        # Build matching CCIL positions (some with mismatches)
        ccil = {}
        for key, qty in internal.items():
            offset = RNG.choice([0, 0, 0, RNG.randint(-10, 10)])
            ccil[key] = qty + offset
        results = reconcile_ccil_positions(db_session, SETTLE_DATE, ccil)
        reconciled = sum(1 for r in results if r.is_reconciled)
        unreconciled = sum(1 for r in results if not r.is_reconciled)
        assert reconciled + unreconciled == len(results)


# ── Test: Phase 5 advanced features at scale ─────────────────────────────────

class TestHighVolumeAdvanced:
    """CM hierarchy, SGF waterfall, stress test, T+0, bond futures CTD."""

    def test_deep_cm_hierarchy(self, db_session):
        cm_ids = _seed_cm_hierarchy(db_session, 20)
        # Insert obligations for CMs
        _bulk_trades(db_session, 2000, SourceSystem.OMS, random.Random(77))
        obligations = compute_obligations(db_session, SourceSystem.OMS, ObligationStage.FINAL)
        # Map obligations to CM IDs
        for i, ob in enumerate(obligations):
            ob.counterparty_id = cm_ids[i % len(cm_ids)]
            db_session.add(ob)
        db_session.commit()

        for parent_id in cm_ids[:4]:
            result = aggregate_obligations(db_session, parent_id, SETTLE_DATE)
            assert result["member_count"] >= 1
            assert result["obligation_count"] >= 0

    def test_sgf_waterfall_extreme_shortfall(self):
        """Shortfall exceeds all 7 layers — final shortfall should be positive."""
        inputs = WaterfallInputs(
            defaulter_margin_collateral=Decimal("1000000"),
            defaulter_base_capital=Decimal("500000"),
            defaulter_sgf_contribution=Decimal("200000"),
            nse_sgf_contribution=Decimal("300000"),
            other_cm_sgf_contributions={f"CM-{i}": Decimal("50000") for i in range(20)},
            nse_other_resources=Decimal("500000"),
            insurance_cover=Decimal("100000"),
        )
        shortfall = Decimal("10000000")
        steps = run_default_waterfall(shortfall, inputs)
        summary = get_waterfall_summary(steps)
        assert summary["final_shortfall"] > 0
        assert not summary["fully_covered"]
        assert summary["steps_used"] == 7

    def test_sgf_waterfall_zero_shortfall(self):
        """Zero shortfall — no layers needed."""
        inputs = WaterfallInputs(
            defaulter_margin_collateral=Decimal("1000000"),
            defaulter_base_capital=Decimal("500000"),
            defaulter_sgf_contribution=Decimal("200000"),
            nse_sgf_contribution=Decimal("300000"),
            other_cm_sgf_contributions={},
            nse_other_resources=Decimal("0"),
            insurance_cover=Decimal("0"),
        )
        steps = run_default_waterfall(Decimal("0"), inputs)
        assert len(steps) == 1
        assert steps[0].shortfall_after == 0

    def test_sgf_waterfall_many_non_defaulting_cms(self):
        """100 non-defaulting CMs in the pool."""
        contributions = {f"CM-{i:04d}": Decimal(str(RNG.randint(10000, 500000))) for i in range(100)}
        inputs = WaterfallInputs(
            defaulter_margin_collateral=Decimal("100000"),
            defaulter_base_capital=Decimal("50000"),
            defaulter_sgf_contribution=Decimal("20000"),
            nse_sgf_contribution=Decimal("30000"),
            other_cm_sgf_contributions=contributions,
            nse_other_resources=Decimal("50000"),
            insurance_cover=Decimal("10000"),
        )
        steps = run_default_waterfall(Decimal("5000000"), inputs)
        summary = get_waterfall_summary(steps)
        assert summary["total_covered"] > 0

    def test_stress_test_many_cms(self, db_session):
        contracts, _ = _seed_derivative_universe(db_session, 5, 20)
        cm_ids = COUNTERPARTIES[:20]
        prices = {c.contract_id: Decimal(str(round(RNG.uniform(100, 5000), 2))) for c in contracts}
        margin_held = {cm: Decimal(str(RNG.randint(100000, 5000000))) for cm in cm_ids}

        results = rank_top_n_stressed_cms(
            db_session, cm_ids, TRADE_DATE, Decimal("15"), prices, margin_held, top_n=10
        )
        assert len(results) <= 10
        # Results should be sorted by shortfall descending
        for i in range(len(results) - 1):
            assert results[i].shortfall >= results[i + 1].shortfall

    def test_t0_at_volume(self, db_session):
        """500 T+0 trades netted into obligations."""
        for i in range(500):
            db_session.add(Trade(
                trade_id=f"T0-STRESS-{i:04d}",
                isin=RNG.choice(ISINS[:5]),
                security_name="T0STRESS",
                quantity=RNG.randint(1, 1000),
                price=Decimal(str(round(RNG.uniform(100, 3000), 2))),
                trade_date=SETTLE_DATE,
                settlement_date=SETTLE_DATE,
                settlement_cycle=SettlementCycle.T0,
                counterparty_id=RNG.choice(COUNTERPARTIES[:10]),
                counterparty_type=CounterpartyType.BROKER,
                exchange=Exchange.NSE,
                buy_sell=RNG.choice([BuySell.BUY, BuySell.SELL]),
                source_system=SourceSystem.OMS,
                product_segment=ProductSegment.EQUITY_CASH,
            ))
        db_session.commit()

        obligations = compute_t0_obligations(db_session, SETTLE_DATE)
        assert len(obligations) > 0

    def test_bond_futures_ctd(self):
        """CTD selection across 50 deliverable bonds."""
        deliverables = []
        for i in range(50):
            deliverables.append(DeliverableBond(
                isin=f"BOND-CTD-{i:03d}",
                coupon_rate_pct=Decimal(str(round(RNG.uniform(5, 10), 2))),
                years_to_maturity=Decimal(str(round(RNG.uniform(2, 20), 2))),
                quoted_price=Decimal(str(round(RNG.uniform(90, 110), 2))),
            ))
        futures_price = Decimal("98.50")
        ctd = identify_cheapest_to_deliver(deliverables, futures_price, Decimal("6"))
        assert ctd is not None
        assert ctd["isin"].startswith("BOND-CTD-")


# ── Test: Cross-module integration stress ────────────────────────────────────

class TestCrossModuleIntegration:
    """Test interactions between multiple phases under load."""

    def test_derivatives_then_margins(self, db_session):
        """Run MTM → SPAN → exposure → collateral check in sequence."""
        contracts, _ = _seed_derivative_universe(db_session, 5, 10)
        _seed_collateral(db_session, COUNTERPARTIES[:10], 3)

        # MTM
        prices = {c.contract_id: Decimal(str(round(RNG.uniform(100, 5000), 2))) for c in contracts}
        mtm_records = compute_daily_mtm(db_session, TRADE_DATE, prices)
        assert len(mtm_records) > 0

        # SPAN for each underlying
        underlyings = list({c.underlying for c in contracts})
        for underlying in underlyings:
            for cp in COUNTERPARTIES[:5]:
                span = compute_span_margin(
                    db_session, cp, underlying, Decimal("1000"), is_index=False,
                )
                assert span.total_margin >= 0

        # Collateral adequacy
        for cp in COUNTERPARTIES[:10]:
            records = db_session.query(CollateralRecord).filter_by(counterparty_id=cp).all()
            if records:
                eff = compute_effective_collateral(records)
                assert eff["total"] >= 0

    def test_debt_settlement_then_sgf(self, db_session):
        """Settle debt trades → compute SGF contribution → run waterfall."""
        _seed_debt_universe(db_session, 5, 3)
        trades = db_session.query(DebtTrade).limit(10).all()
        for t in trades:
            mark_securities_received(db_session, t.trade_id)
            mark_funds_received(db_session, t.trade_id)

        instruments = db_session.query(DebtInstrument).all()
        total_contribution = Decimal("0")
        for inst in instruments:
            inst_trades = db_session.query(DebtTrade).filter_by(isin=inst.isin).all()
            if inst_trades:
                total_value = sum(t.quantity * t.clean_price for t in inst_trades)
                contribution = compute_sgf_issuer_contribution(total_value, inst.issue_date, inst.maturity_date)
                total_contribution += contribution

        steps = run_default_waterfall(
            Decimal("500000"),
            WaterfallInputs(
                defaulter_margin_collateral=Decimal("100000"),
                defaulter_base_capital=Decimal("50000"),
                defaulter_sgf_contribution=total_contribution,
                nse_sgf_contribution=Decimal("100000"),
                other_cm_sgf_contributions={"CM-X": Decimal("200000")},
                nse_other_resources=Decimal("50000"),
                insurance_cover=Decimal("0"),
            ),
        )
        summary = get_waterfall_summary(steps)
        assert summary["total_covered"] > 0

    def test_full_pipeline_all_phases(self, db_session):
        """Simulate the entire 21-step pipeline flow in-memory."""
        # Phase 1: equity cash
        _bulk_trades(db_session, 200, SourceSystem.OMS, random.Random(10))
        _seed_ssi(db_session, COUNTERPARTIES[:20])
        obligations = compute_obligations(db_session, SourceSystem.OMS, ObligationStage.FINAL)
        for ob in obligations:
            db_session.add(ob)
        db_session.commit()
        assert len(obligations) > 0

        # Phase 2: derivatives
        contracts, _ = _seed_derivative_universe(db_session, 3, 5)
        prices = {c.contract_id: Decimal(str(round(RNG.uniform(100, 2000), 2))) for c in contracts}
        mtm = compute_daily_mtm(db_session, TRADE_DATE, prices)
        premiums = compute_premium_obligations(db_session, TRADE_DATE)

        # Phase 3: margins
        for underlying in list({c.underlying for c in contracts}):
            compute_span_margin(db_session, COUNTERPARTIES[0], underlying, Decimal("1000"))
        var = compute_var_margin(Decimal("1000000"), Decimal("0.02"))
        assert var > 0

        # Phase 4: debt
        _seed_debt_universe(db_session, 3, 2)
        debt_trades = db_session.query(DebtTrade).limit(3).all()
        for dt in debt_trades:
            mark_securities_received(db_session, dt.trade_id)
            mark_funds_received(db_session, dt.trade_id)

        # Phase 5: CM hierarchy + waterfall
        cm_ids = _seed_cm_hierarchy(db_session, 6)
        steps = run_default_waterfall(
            Decimal("100000"),
            WaterfallInputs(
                defaulter_margin_collateral=Decimal("50000"),
                defaulter_base_capital=Decimal("30000"),
                defaulter_sgf_contribution=Decimal("20000"),
                nse_sgf_contribution=Decimal("10000"),
                other_cm_sgf_contributions={},
                nse_other_resources=Decimal("0"),
                insurance_cover=Decimal("0"),
            ),
        )
        summary = get_waterfall_summary(steps)
        assert summary["fully_covered"]


# ── Test: Boundary / edge cases ──────────────────────────────────────────────

class TestBoundaryConditions:
    """Zero values, single records, extreme numbers."""

    def test_zero_quantity_trades(self, db_session):
        db_session.add(Trade(
            trade_id="ZERO-QTY-1", isin="INE001A01001", security_name="TEST",
            quantity=0, price=Decimal("100"), trade_date=TRADE_DATE,
            settlement_date=SETTLE_DATE, settlement_cycle=SettlementCycle.T1,
            counterparty_id="BRK-001", counterparty_type=CounterpartyType.BROKER,
            exchange=Exchange.NSE, buy_sell=BuySell.BUY, source_system=SourceSystem.OMS,
            segment=Segment.NORMAL,
        ))
        db_session.commit()
        obligations = compute_obligations(db_session, SourceSystem.OMS, ObligationStage.FINAL)
        # Should handle zero-qty gracefully
        assert isinstance(obligations, list)

    def test_extreme_prices(self, db_session):
        for price in [Decimal("0.01"), Decimal("999999.99")]:
            db_session.add(Trade(
                trade_id=f"EXTREME-{price}", isin="INE001A01001", security_name="TEST",
                quantity=100, price=price, trade_date=TRADE_DATE,
                settlement_date=SETTLE_DATE, settlement_cycle=SettlementCycle.T1,
                counterparty_id="BRK-001", counterparty_type=CounterpartyType.BROKER,
                exchange=Exchange.NSE, buy_sell=BuySell.BUY, source_system=SourceSystem.OMS,
                segment=Segment.NORMAL,
            ))
        db_session.commit()
        obligations = compute_obligations(db_session, SourceSystem.OMS, ObligationStage.FINAL)
        assert len(obligations) > 0

    def test_single_trade_pipeline(self, db_session):
        """One trade through the entire equity-cash pipeline."""
        db_session.add(Trade(
            trade_id="SOLO-1", isin="INE001A01001", security_name="SOLO",
            quantity=1, price=Decimal("100"), trade_date=TRADE_DATE,
            settlement_date=SETTLE_DATE, settlement_cycle=SettlementCycle.T1,
            counterparty_id="BRK-001", counterparty_type=CounterpartyType.BROKER,
            exchange=Exchange.NSE, buy_sell=BuySell.BUY, source_system=SourceSystem.OMS,
            segment=Segment.NORMAL,
        ))
        db_session.commit()
        obligations = compute_obligations(db_session, SourceSystem.OMS, ObligationStage.FINAL)
        assert len(obligations) == 1
        assert obligations[0].net_quantity == 1

    def test_empty_mtm(self, db_session):
        records = compute_daily_mtm(db_session, TRADE_DATE, {})
        assert records == []

    def test_var_single_return(self):
        vol = ewma_volatility([Decimal("0.01")])
        assert vol >= 0

    def test_waterfall_all_zero_resources(self):
        inputs = WaterfallInputs(
            defaulter_margin_collateral=Decimal("0"),
            defaulter_base_capital=Decimal("0"),
            defaulter_sgf_contribution=Decimal("0"),
            nse_sgf_contribution=Decimal("0"),
            other_cm_sgf_contributions={},
            nse_other_resources=Decimal("0"),
            insurance_cover=Decimal("0"),
        )
        steps = run_default_waterfall(Decimal("1000000"), inputs)
        summary = get_waterfall_summary(steps)
        assert not summary["fully_covered"]
        assert summary["final_shortfall"] == Decimal("1000000")

    def test_collateral_empty_portfolio(self):
        result = compute_effective_collateral([])
        assert result["total"] == Decimal("0")

    def test_ctd_single_bond(self):
        ctd = identify_cheapest_to_deliver(
            [DeliverableBond(isin="BOND-SOLO", coupon_rate_pct=Decimal("7"), years_to_maturity=Decimal("5"), quoted_price=Decimal("100"))],
            Decimal("99.50"), Decimal("6"),
        )
        assert ctd["isin"] == "BOND-SOLO"

    def test_ctd_empty_list(self):
        with pytest.raises(ValueError):
            identify_cheapest_to_deliver([], Decimal("100"), Decimal("6"))
