"""
Pipeline orchestrator — runs the full trade settlement operations pipeline.

Execution order:
1.  Ingest & normalize trades from 3 sources
2.  Compute net obligations (provisional + final)
3.  Validate SSI against golden copy
4.  Match internal vs counterparty obligations
5.  Process custodian confirmations
6.  Generate settlement instructions
7.  Format ISO 20022 messages (sese.023)
8.  Detect breaks & classify
9.  Handle short deliveries (auction/close-out)
10. Compute CSDR progressive penalties
11. Run ML fail-risk prediction
12. Run agentic triage pipeline (dual-path)
13. Compute counterparty risk scorecards
14. Monitor intraday liquidity
15. Reconcile EOD positions
16. Generate reports
17. Seed multi-segment demo data (derivatives, debt, collateral, CM hierarchy, T+0)
18. Run derivatives settlement (MTM, premium, exercise/assignment, final, delivery)
19. Run margin & collateral framework (SPAN, exposure, VaR, delivery, cross, limits)
20. Run debt & fixed income settlement (DvP-I, accrued interest, corp actions, SGF, G-Sec recon)
21. Run advanced features (CM hierarchy, SGF waterfall, stress test, T+0, bond futures CTD)
"""

import os
import signal
import sys
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path


from src.logging_config import get_logger, setup_logging
from src.settings import (
    DATA_DIR,
    DATABASE_URL,
    ENABLE_ADVANCED_FEATURES,
    ENABLE_CSDR_PENALTIES,
    ENABLE_DEBT,
    ENABLE_DERIVATIVES,
    ENABLE_ISO20022,
    ENABLE_LIQUIDITY_MONITOR,
    ENABLE_MARGINS,
    ENABLE_ML_PREDICTION,
    ENABLE_SCORECARDS,
    ML_RISK_THRESHOLD,
)
from src.models.database import (
    AgenticAuditLog,
    BreakRecord,
    CollateralRecord,
    DebtInstrument,
    DerivativeContract,
    Obligation,
    Trade,
    create_tables,
    get_engine,
    get_session,
)
from src.models.enums import (
    BreakStatus,
    MatchStatus,
    ObligationStage,
    ObligationStatus,
    ProductSegment,
    SettlementCycle,
)

logger = get_logger(__name__)

# ── Graceful shutdown ──────────────────────────────────────────────────────
_shutdown_requested = False


def _signal_handler(signum: int, frame) -> None:
    global _shutdown_requested
    sig_name = signal.Signals(signum).name
    logger.warning("shutdown.requested", signal=sig_name)
    _shutdown_requested = True


def _check_shutdown(step: str) -> None:
    if _shutdown_requested:
        logger.info("shutdown.graceful", interrupted_at=step)
        sys.exit(0)


def run_pipeline(product_segment: ProductSegment = ProductSegment.EQUITY_CASH):
    """Dispatch the settlement pipeline for the given NSE product segment.

    Only EQUITY_CASH is implemented today. The other segments (equity F&O,
    currency derivatives, IRD, debt) get their own pipelines in later phases
    per docs/NSE_CLEARING_SETTLEMENT_PLAN.md.
    """
    if product_segment != ProductSegment.EQUITY_CASH:
        setup_logging()
        get_logger(__name__).info(
            "pipeline.segment_not_implemented", segment=product_segment.value
        )
        raise NotImplementedError(
            f"Pipeline for segment {product_segment.value} is not implemented yet"
        )
    _run_equity_cash_pipeline()


def _run_equity_cash_pipeline():
    setup_logging()
    start_time = time.monotonic()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    logger.info("pipeline.start", version="1.0.0", database=DATABASE_URL)

    # Resolve DB path from URL for SQLite
    if DATABASE_URL.startswith("sqlite"):
        db_path = DATABASE_URL.replace("sqlite:///", "")
        db_file = Path(db_path)
        if db_file.exists():
            os.remove(db_file)
        engine = get_engine(db_path)
    else:
        db_path = DATABASE_URL
        engine = get_engine(db_path)

    create_tables(engine)
    session = get_session(engine)

    # ── Step 1: Ingest & Normalize ──────────────────────────────────────
    logger.info("step.start", step=1, name="trade_capture")
    from src.ingestion.normalizer import ingest_all
    ingest_all(DATA_DIR, db_path)
    total_trades = session.query(Trade).count()
    logger.info("step.complete", step=1, name="trade_capture", trades=total_trades)
    _check_shutdown("step_1")

    # ── Step 2: Netting & Obligations ───────────────────────────────────
    logger.info("step.start", step=2, name="netting")
    from src.netting.obligation_engine import compute_all_obligations, get_obligations_for_matching
    compute_all_obligations(session)
    total_obligations = session.query(Obligation).count()
    logger.info("step.complete", step=2, name="netting", obligations=total_obligations)
    _check_shutdown("step_2")

    # ── Step 3: SSI Validation ──────────────────────────────────────────
    logger.info("step.start", step=3, name="ssi_validation")
    from src.ssi.golden_copy import validate_all_obligations

    internal_obs = (
        session.query(Obligation)
        .filter(
            Obligation.obligation_stage == ObligationStage.FINAL,
            Obligation.status == ObligationStatus.PENDING,
        )
        .all()
    )
    valid_obs, ssi_breaks = validate_all_obligations(session, internal_obs)
    logger.info("step.complete", step=3, name="ssi_validation",
                validated=len(valid_obs), ssi_breaks=len(ssi_breaks))
    _check_shutdown("step_3")

    # ── Step 4: Matching Engine ─────────────────────────────────────────
    logger.info("step.start", step=4, name="matching")
    from src.matching.engine import match_obligations, create_break_records

    internal_final, broker_final, custodian_final = get_obligations_for_matching(session)
    broker_results = match_obligations(internal_final, broker_final)
    matched_broker = sum(1 for r in broker_results if r.status == MatchStatus.MATCHED)
    broken_broker = sum(1 for r in broker_results if r.status == MatchStatus.BREAK)
    unmatched_broker = sum(1 for r in broker_results if r.status == MatchStatus.UNMATCHED)

    ob_by_id = {ob.obligation_id: ob for ob in internal_final + broker_final}
    broker_break_records = create_break_records(session, broker_results, ob_by_id)
    logger.info("step.complete", step=4, name="matching",
                matched=matched_broker, breaks=broken_broker,
                unmatched=unmatched_broker, break_records=len(broker_break_records))
    _check_shutdown("step_4")

    # ── Step 5: Custodian Confirmation ──────────────────────────────────
    logger.info("step.start", step=5, name="confirmation")
    from src.confirmation.custodian_confirm import process_confirmations, simulate_confirmation_responses

    matched_obs = (
        session.query(Obligation)
        .filter(Obligation.status == ObligationStatus.MATCHED)
        .all()
    )
    responses = simulate_confirmation_responses(matched_obs)
    current_time = datetime(2026, 6, 2, 12, 30)

    confirmed, problems, late_breaks = process_confirmations(
        session, matched_obs, responses, current_time
    )
    logger.info("step.complete", step=5, name="confirmation",
                confirmed=len(confirmed), problems=len(problems),
                late_breaks=len(late_breaks))
    _check_shutdown("step_5")

    # ── Step 6: Settlement Instructions ─────────────────────────────────
    logger.info("step.start", step=6, name="instructions")
    from src.instruction.settlement_instruction import generate_all_instructions

    confirmed_obs = (
        session.query(Obligation)
        .filter(Obligation.status == ObligationStatus.CONFIRMED)
        .all()
    )
    instructions = generate_all_instructions(session, confirmed_obs)
    logger.info("step.complete", step=6, name="instructions", generated=len(instructions))
    _check_shutdown("step_6")

    # ── Step 7: ISO 20022 Message Formatting ────────────────────────────
    iso_messages = []
    msg_summary = {"total_messages": 0, "deliver_instructions": 0,
                   "receive_instructions": 0, "format": "ISO 20022 XML",
                   "message_type": "sese.023.001.09"}
    if ENABLE_ISO20022:
        logger.info("step.start", step=7, name="iso20022")
        from src.instruction.iso20022_formatter import format_batch, get_message_summary
        iso_messages = format_batch(instructions)
        msg_summary = get_message_summary(iso_messages)
        logger.info("step.complete", step=7, name="iso20022",
                    messages=msg_summary["total_messages"],
                    deliver=msg_summary["deliver_instructions"],
                    receive=msg_summary["receive_instructions"])
    else:
        logger.info("step.skipped", step=7, name="iso20022", reason="disabled")
    _check_shutdown("step_7")

    # ── Step 8: Break Detection & Classification ────────────────────────
    logger.info("step.start", step=8, name="break_detection")
    from src.breaks.rules_engine import update_break_aging, get_break_summary

    update_break_aging(session, current_time)
    summary = get_break_summary(session)
    logger.info("step.complete", step=8, name="break_detection",
                total_breaks=summary["total"],
                by_type=summary["by_type"],
                by_severity=summary["by_severity"])
    _check_shutdown("step_8")

    # ── Step 9: Auction / Close-out ────────────────────────────────────
    logger.info("step.start", step=9, name="auction")
    from src.auction.close_out import initiate_auction, execute_auction

    instructed_obs = (
        session.query(Obligation)
        .filter(Obligation.status == ObligationStatus.INSTRUCTED)
        .all()
    )

    import random
    random.seed(77)
    short_candidates = [ob for ob in instructed_obs if ob.net_direction.value == "PAY_IN"]
    n_short = min(3, len(short_candidates))
    shorts = random.sample(short_candidates, n_short) if short_candidates else []

    for ob in shorts:
        ob.status = ObligationStatus.FAILED
    session.commit()

    auction_results = []
    for ob in shorts:
        auction = initiate_auction(
            session, ob,
            valuation_price=Decimal(str(ob.vwap_price)) * Decimal("0.99"),
            auction_date=date(2026, 6, 3),
        )
        if random.random() > 0.5:
            execute_auction(
                session, auction,
                auction_price=Decimal(str(ob.vwap_price)) * Decimal("1.02"),
                closing_price_auction_day=Decimal(str(ob.vwap_price)),
                highest_price_trade_to_auction=Decimal(str(ob.vwap_price)) * Decimal("1.05"),
            )
            auction_results.append({"isin": ob.isin, "outcome": "AUCTION_SUCCESS"})
        else:
            execute_auction(
                session, auction,
                auction_price=None,
                closing_price_auction_day=Decimal(str(ob.vwap_price)),
                highest_price_trade_to_auction=Decimal(str(ob.vwap_price)) * Decimal("1.03"),
            )
            auction_results.append({"isin": ob.isin, "outcome": "CLOSED_OUT"})

    logger.info("step.complete", step=9, name="auction",
                short_deliveries=len(shorts), results=auction_results)
    _check_shutdown("step_9")

    # ── Step 10: CSDR Progressive Penalties ─────────────────────────────
    penalty_assessments = []
    if ENABLE_CSDR_PENALTIES:
        logger.info("step.start", step=10, name="csdr_penalties")
        from src.penalties.csdr_penalties import compute_penalties_batch, get_penalty_summary

        failed_obs = (
            session.query(Obligation)
            .filter(
                Obligation.obligation_stage == ObligationStage.FINAL,
                Obligation.status.in_([
                    ObligationStatus.FAILED,
                    ObligationStatus.AUCTION,
                    ObligationStatus.CLOSED_OUT,
                ]),
            )
            .all()
        )

        if failed_obs:
            assessment_date = date(2026, 6, 5)
            fail_pairs = [(ob, ob.settlement_date) for ob in failed_obs]
            penalty_assessments = compute_penalties_batch(fail_pairs, assessment_date)
            penalty_summary = get_penalty_summary(penalty_assessments)
            logger.info("step.complete", step=10, name="csdr_penalties",
                        assessed=penalty_summary["total_fails"],
                        total_penalties=float(penalty_summary["total_penalties"]))
        else:
            logger.info("step.complete", step=10, name="csdr_penalties",
                        assessed=0, total_penalties=0)
    else:
        logger.info("step.skipped", step=10, name="csdr_penalties", reason="disabled")
    _check_shutdown("step_10")

    # ── Step 11: ML Fail-Risk Prediction ────────────────────────────────
    ml_scores = []
    ml_high_risk = []
    if ENABLE_ML_PREDICTION:
        logger.info("step.start", step=11, name="ml_prediction")
        from src.triage.ml_fail_predictor import (
            predict_fail_risk_batch,
            get_ml_high_risk_queue,
            train_model,
        )

        train_model()

        ml_pending_obs = (
            session.query(Obligation)
            .filter(Obligation.status.in_([
                ObligationStatus.PENDING,
                ObligationStatus.SSI_VALIDATED,
                ObligationStatus.CONFIRMED,
                ObligationStatus.INSTRUCTED,
            ]))
            .limit(100)
            .all()
        )

        ml_scores = predict_fail_risk_batch(ml_pending_obs, current_time)
        ml_high_risk = get_ml_high_risk_queue(ml_scores, threshold=ML_RISK_THRESHOLD)

        avg_prob = sum(s.fail_probability for s in ml_scores) / len(ml_scores) if ml_scores else 0
        logger.info("step.complete", step=11, name="ml_prediction",
                    scored=len(ml_scores), high_risk=len(ml_high_risk),
                    avg_probability=round(avg_prob, 4),
                    model_version=ml_scores[0].model_version if ml_scores else "N/A")
    else:
        logger.info("step.skipped", step=11, name="ml_prediction", reason="disabled")
    _check_shutdown("step_11")

    # ── Step 12: Agentic Triage Pipeline ────────────────────────────────
    logger.info("step.start", step=12, name="triage")

    pending_obs = (
        session.query(Obligation)
        .filter(Obligation.status.in_([
            ObligationStatus.PENDING,
            ObligationStatus.SSI_VALIDATED,
            ObligationStatus.CONFIRMED,
            ObligationStatus.INSTRUCTED,
        ]))
        .limit(50)
        .all()
    )

    obligation_dicts = []
    for ob in pending_obs:
        obligation_dicts.append({
            "obligation_id": ob.obligation_id,
            "isin": ob.isin,
            "counterparty_id": ob.counterparty_id,
            "settlement_cycle": ob.settlement_cycle.value,
            "net_value": str(ob.net_value),
            "status": ob.status.value,
        })

    open_breaks = (
        session.query(BreakRecord)
        .filter(BreakRecord.status.in_([BreakStatus.OPEN, BreakStatus.IN_PROGRESS]))
        .all()
    )

    break_dicts = []
    for brk in open_breaks:
        ob = session.query(Obligation).filter(
            Obligation.obligation_id == brk.obligation_id
        ).first()
        break_dicts.append({
            "break_id": brk.break_id,
            "obligation_id": brk.obligation_id,
            "break_type": brk.break_type.value,
            "severity": brk.severity.value,
            "value_at_risk": str(brk.value_at_risk) if brk.value_at_risk else "0",
            "age_days": brk.age_days or 0,
            "age_hours": brk.age_hours or 0,
            "isin": ob.isin if ob else "",
            "counterparty_id": ob.counterparty_id if ob else "",
            "settlement_cycle": ob.settlement_cycle.value if ob else "T1",
        })

    logger.info("triage.input", obligations=len(obligation_dicts), breaks=len(break_dicts))

    from src.triage.knowledge_base import build_index
    build_index()

    from src.triage.pipeline import run_triage
    triage_result = run_triage(
        obligations=obligation_dicts,
        breaks=break_dicts,
    )

    for log_entry in triage_result["audit_logs"]:
        audit = AgenticAuditLog(
            log_id=log_entry["log_id"],
            obligation_id=log_entry.get("obligation_id"),
            break_id=log_entry.get("break_id"),
            node_name=log_entry["node_name"],
            inputs=log_entry["inputs"],
            conclusion=log_entry["conclusion"],
            rationale=log_entry["rationale"],
            timestamp=datetime.fromisoformat(log_entry["timestamp"]),
        )
        session.add(audit)
    session.commit()

    logger.info("step.complete", step=12, name="triage",
                path=triage_result["path"],
                risk_scores=len(triage_result["fail_risk_scores"]),
                high_risk=len(triage_result["high_risk_queue"]),
                triage_results=len(triage_result["triage_results"]),
                audit_entries=len(triage_result["audit_logs"]))
    _check_shutdown("step_12")

    # ── Step 13: Counterparty Risk Scorecards ───────────────────────────
    scorecards = []
    sc_summary = {"total": 0, "by_grade": {}, "avg_score": 0}
    if ENABLE_SCORECARDS:
        logger.info("step.start", step=13, name="scorecards")
        from src.risk.counterparty_scorecard import (
            compute_all_scorecards,
            get_scorecard_summary,
            get_watch_list,
        )

        cp_ids = list({
            r[0] for r in
            session.query(Obligation.counterparty_id)
            .filter(Obligation.obligation_stage == ObligationStage.FINAL)
            .distinct()
            .all()
        })

        scorecards = compute_all_scorecards(session, cp_ids, date(2026, 6, 2))
        sc_summary = get_scorecard_summary(scorecards)
        watch_list = get_watch_list(scorecards)

        logger.info("step.complete", step=13, name="scorecards",
                    rated=sc_summary["total"],
                    avg_score=sc_summary["avg_score"],
                    watch_list=len(watch_list))
    else:
        logger.info("step.skipped", step=13, name="scorecards", reason="disabled")
    _check_shutdown("step_13")

    # ── Step 14: Intraday Liquidity Monitoring ──────────────────────────
    liquidity_report = None
    if ENABLE_LIQUIDITY_MONITOR:
        logger.info("step.start", step=14, name="liquidity")
        from src.liquidity.intraday_monitor import generate_intraday_report

        liquidity_report = generate_intraday_report(
            session,
            settlement_date=date(2026, 6, 2),
            current_time=current_time,
        )

        snap = liquidity_report.current_snapshot
        logger.info("step.complete", step=14, name="liquidity",
                    net_position=float(snap.net_position),
                    buffer_utilization=snap.buffer_utilization,
                    alerts=len(liquidity_report.alerts),
                    exposures=len(liquidity_report.counterparty_exposures))
    else:
        logger.info("step.skipped", step=14, name="liquidity", reason="disabled")
    _check_shutdown("step_14")

    # ── Step 15: Reconciliation ─────────────────────────────────────────
    logger.info("step.start", step=15, name="reconciliation")
    from src.reconciliation.position_recon import reconcile_positions, get_recon_summary

    settled_count = 0
    for ob in confirmed_obs[:20]:
        ob.status = ObligationStatus.SETTLED
        settled_count += 1
    session.commit()

    recon_results = reconcile_positions(session, date(2026, 6, 2))
    recon_summary = get_recon_summary(recon_results)
    logger.info("step.complete", step=15, name="reconciliation",
                positions=recon_summary["total_positions"],
                reconciled=recon_summary["reconciled"],
                unreconciled=recon_summary["unreconciled"],
                recon_rate=recon_summary["recon_rate"])
    _check_shutdown("step_15")

    # ── Step 16: Reporting ──────────────────────────────────────────────
    logger.info("step.start", step=16, name="reporting")
    from src.reporting.report_generator import generate_reports

    generate_reports(
        session=session,
        triage_result=triage_result,
        recon_results=recon_results,
        output_dir=DATA_DIR,
    )
    logger.info("step.complete", step=16, name="reporting")

    # ── Step 17: Multi-Segment Seed Data ────────────────────────────────
    logger.info("step.start", step=17, name="segment_seed")
    from src.segments.demo_seed import (
        CM_IDS,
        CORP_BOND_ISIN,
        EXPIRY_DATE,
        NIFTY_CE,
        NIFTY_FUT,
        RELIANCE_CE,
        RELIANCE_FUT,
        seed_clearing_members,
        seed_collateral_records,
        seed_debt_instruments_and_trades,
        seed_derivative_contracts_and_positions,
        seed_t0_equity_trades,
    )

    demo_date = date(2026, 6, 2)
    debt_settle_date = date(2026, 6, 3)
    seed_clearing_members(session)
    seed_derivative_contracts_and_positions(session, demo_date)
    seed_collateral_records(session, demo_date)
    seed_debt_instruments_and_trades(session, demo_date, debt_settle_date)
    seed_t0_equity_trades(session, demo_date)
    logger.info("step.complete", step=17, name="segment_seed",
                clearing_members=len(CM_IDS), derivative_contracts=4, debt_instruments=2)
    _check_shutdown("step_17")

    # ── Step 18: Derivatives Settlement (Equity F&O) ────────────────────
    deriv_summary = {}
    if ENABLE_DERIVATIVES:
        logger.info("step.start", step=18, name="derivatives")
        from src.derivatives.exercise_engine import assign_short_positions, exercise_long_positions
        from src.derivatives.final_settlement import run_final_settlement
        from src.derivatives.mtm_engine import compute_daily_mtm, get_mtm_summary
        from src.derivatives.physical_delivery import (
            generate_futures_delivery_obligations,
            generate_option_delivery_obligations,
            get_delivery_summary,
        )
        from src.derivatives.premium_engine import compute_premium_obligations, get_premium_summary

        settlement_prices = {NIFTY_FUT: Decimal("23850"), NIFTY_CE: Decimal("190")}
        mtm_records = compute_daily_mtm(session, demo_date, settlement_prices)
        mtm_summary = get_mtm_summary(mtm_records)

        net_premiums = compute_premium_obligations(session, demo_date)
        premium_summary = get_premium_summary(net_premiums)

        reliance_ce_contract = session.query(DerivativeContract).filter_by(contract_id=RELIANCE_CE).first()
        reliance_fut_contract = session.query(DerivativeContract).filter_by(contract_id=RELIANCE_FUT).first()
        fsp_nifty = Decimal("24100")
        fsp_reliance = Decimal("1420")

        exercise_results = exercise_long_positions(session, reliance_ce_contract, fsp_reliance)
        assignment_results = assign_short_positions(session, reliance_ce_contract, exercise_results, seed=7)

        final_mtm_records = run_final_settlement(
            session, EXPIRY_DATE, {NIFTY_FUT: fsp_nifty, NIFTY_CE: fsp_nifty}
        )

        futures_delivery_obs = generate_futures_delivery_obligations(
            session, reliance_fut_contract, "INE002A01018", fsp_reliance, EXPIRY_DATE
        )
        option_delivery_obs = generate_option_delivery_obligations(
            session, reliance_ce_contract, "INE002A01018", exercise_results, assignment_results, EXPIRY_DATE
        )
        delivery_summary = get_delivery_summary(futures_delivery_obs + option_delivery_obs)

        deriv_summary = {
            "mtm_positions": mtm_summary["total_positions"],
            "premium_counterparties": premium_summary["counterparties"],
            "exercised_lots": sum(r.exercised_quantity for r in exercise_results),
            "final_mtm_legs": len(final_mtm_records),
            "delivery_obligations": delivery_summary["total"],
        }
        logger.info("step.complete", step=18, name="derivatives", **deriv_summary)
    else:
        logger.info("step.skipped", step=18, name="derivatives", reason="disabled")
    _check_shutdown("step_18")

    # ── Step 19: Margin & Collateral Framework ──────────────────────────
    margin_summary = {}
    if ENABLE_MARGINS:
        logger.info("step.start", step=19, name="margins")
        from src.collateral.manager import check_cash_rule, check_concentration_limit, compute_effective_collateral
        from src.collateral.optimizer import AvailableAsset, optimize_collateral_pledge
        from src.models.enums import CollateralType
        from src.margins.cross_margin import apply_cross_margin, compute_cross_margin_benefit
        from src.margins.delivery_margin import record_delivery_margin
        from src.margins.exposure_margin import compute_exposure_margin
        from src.margins.position_limits import (
            check_client_level_limit,
            check_cm_level_limit,
            check_market_wide_limit,
        )
        from src.margins.span_engine import compute_span_margin
        from src.margins.var_model import compute_var_margin, ewma_volatility

        span_nifty = compute_span_margin(session, "BRK-001", "NIFTY", Decimal("23850"), is_index=True)
        span_reliance = compute_span_margin(session, "BRK-002", "RELIANCE", Decimal("1390"), is_index=False)

        exposure_nifty = compute_exposure_margin(Decimal("23850"), 50, 10, is_index=True)
        exposure_reliance = compute_exposure_margin(
            Decimal("1390"), 250, 5, is_index=False, std_dev_pct=Decimal("2.1")
        )

        equity_returns = [Decimal(v) for v in ["0.012", "-0.008", "0.015", "-0.004", "0.009"]]
        volatility = ewma_volatility(equity_returns)
        var_margin = compute_var_margin(Decimal("1650.50"), volatility)

        delivery_margin_record = record_delivery_margin(
            session, "BRK-002", ProductSegment.EQUITY_FO, EXPIRY_DATE, date(2026, 6, 23),
            notional_value=Decimal("1390") * 250 * 5,
        )

        cross_benefit = compute_cross_margin_benefit(span_nifty.total_margin, Decimal("50000"), is_hedged=True)
        net_after_hedge = apply_cross_margin(span_nifty.total_margin, cross_benefit)

        mw_violation = check_market_wide_limit(open_interest_lots=18000, free_float_lots=80000)
        cm_violation = check_cm_level_limit("BRK-001", cm_open_interest_lots=2500, free_float_lots=80000)
        client_violation = check_client_level_limit("CLIENT-001", client_open_interest_lots=120, free_float_lots=80000)

        collateral_records = session.query(CollateralRecord).filter_by(counterparty_id="BRK-001").all()
        collateral_breakdown = compute_effective_collateral(collateral_records)
        cash_violation = check_cash_rule(collateral_records)
        concentration_violations = check_concentration_limit(collateral_records)

        # Simulate a margin call exceeding BRK-001's current effective
        # collateral by 5M — recommend the cheapest compliant top-up from
        # its unencumbered asset pool rather than leaving "what to pledge"
        # to manual choice.
        available_pool = [
            AvailableAsset(CollateralType.CASH, Decimal("5000000")),
            AvailableAsset(CollateralType.GOVERNMENT_SECURITY, Decimal("3000000")),
        ]
        simulated_margin_call = collateral_breakdown["total"] + Decimal("5000000")
        optimization = optimize_collateral_pledge(
            collateral_records, available_pool, required_margin=simulated_margin_call,
        )

        margin_summary = {
            "span_nifty_total": float(span_nifty.total_margin),
            "span_reliance_total": float(span_reliance.total_margin),
            "exposure_nifty": float(exposure_nifty),
            "exposure_reliance": float(exposure_reliance),
            "var_margin": float(var_margin),
            "delivery_margin_recorded": delivery_margin_record is not None,
            "net_margin_after_hedge": float(net_after_hedge),
            "position_limit_violations": sum(1 for v in (mw_violation, cm_violation, client_violation) if v),
            "effective_collateral": float(collateral_breakdown["total"]),
            "collateral_violations": (1 if cash_violation else 0) + len(concentration_violations),
            "collateral_optimization_shortfall": float(optimization.shortfall_before),
            "collateral_optimization_pledges_recommended": len(optimization.recommendations),
            "collateral_optimization_shortfall_remaining": float(optimization.shortfall_remaining),
        }
        logger.info("step.complete", step=19, name="margins", **margin_summary)
    else:
        logger.info("step.skipped", step=19, name="margins", reason="disabled")
    _check_shutdown("step_19")

    # ── Step 20: Debt & Fixed Income Settlement ─────────────────────────
    debt_summary = {}
    if ENABLE_DEBT:
        logger.info("step.start", step=20, name="debt")
        from src.debt.accrued_interest import compute_accrued_interest
        from src.debt.corporate_actions import compute_coupon_payment, compute_redemption_amount
        from src.debt.corporate_bond_settlement import (
            get_settlement_summary,
            mark_funds_received,
            mark_securities_received,
            settle_dvp_atomic,
        )
        from src.debt.gsec_integration import (
            derive_gsec_positions,
            get_gsec_recon_summary,
            reconcile_ccil_positions,
        )
        from src.debt.sgf_contribution import compute_sgf_issuer_contribution

        mark_securities_received(session, "DEBT-T001")
        mark_funds_received(session, "DEBT-T001")
        mark_securities_received(session, "DEBT-T003")
        mark_funds_received(session, "DEBT-T003")
        # DEBT-T002 settles via the atomic mode instead — both legs are
        # confirmed available simultaneously here, so it settles in one
        # decision rather than via the two-call async pattern above.
        settle_dvp_atomic(session, "DEBT-T002", securities_available=True, funds_available=True)
        session.commit()
        settlement_summary = get_settlement_summary(session, debt_settle_date)

        corp_bond = session.query(DebtInstrument).filter_by(isin=CORP_BOND_ISIN).first()
        coupon_rate = Decimal(str(corp_bond.coupon_rate_pct))
        face_value = Decimal(str(corp_bond.face_value))
        accrued = compute_accrued_interest(
            face_value, coupon_rate, date(2026, 1, 15), debt_settle_date, corp_bond.day_count_convention
        )
        coupon = compute_coupon_payment(face_value, coupon_rate, corp_bond.coupon_frequency, 1000)
        redemption = compute_redemption_amount(face_value, 1000)
        sgf_contrib = compute_sgf_issuer_contribution(
            face_value * 100000, corp_bond.issue_date, corp_bond.maturity_date
        )

        internal_positions = derive_gsec_positions(session, debt_settle_date)
        ccil_positions = dict(internal_positions)
        if ccil_positions:
            first_key = next(iter(ccil_positions))
            ccil_positions[first_key] += 500  # simulate a CCIL discrepancy for the demo
        recon_results = reconcile_ccil_positions(session, debt_settle_date, ccil_positions)
        recon_summary = get_gsec_recon_summary(recon_results)

        debt_summary = {
            "settled_trades": settlement_summary["SETTLED"],
            "pending_trades": settlement_summary["PENDING"],
            "accrued_interest": float(accrued),
            "coupon_payment": float(coupon),
            "redemption_amount": float(redemption),
            "sgf_issuer_contribution": float(sgf_contrib),
            "gsec_recon_reconciled": recon_summary["reconciled"],
            "gsec_recon_unreconciled": recon_summary["unreconciled"],
        }
        logger.info("step.complete", step=20, name="debt", **debt_summary)
    else:
        logger.info("step.skipped", step=20, name="debt", reason="disabled")
    _check_shutdown("step_20")

    # ── Step 21: Advanced Features (Phase 5) ────────────────────────────
    advanced_summary = {}
    if ENABLE_ADVANCED_FEATURES:
        logger.info("step.start", step=21, name="advanced_features")
        from datetime import time as clock_time

        from src.cm_hierarchy.hierarchy import aggregate_obligations
        from src.derivatives.bond_futures import DeliverableBond, identify_cheapest_to_deliver
        from src.risk.stress_test import (
            get_contagion_summary,
            get_stress_summary,
            identify_contagion_clusters,
            rank_top_n_stressed_cms,
        )
        from src.settlement.t0_engine import (
            compute_t0_obligations,
            get_t0_summary,
            partition_t0_eligible_trades,
            settle_t0_funds,
        )
        from src.sgf.waterfall import WaterfallInputs, get_waterfall_summary, run_default_waterfall

        sample_obligation = (
            session.query(Obligation)
            .filter(Obligation.product_segment == ProductSegment.EQUITY_CASH)
            .first()
        )
        cm_settlement_date = sample_obligation.settlement_date if sample_obligation else demo_date
        cm_aggregation = aggregate_obligations(session, "BRK-001", cm_settlement_date)

        # T0-eligible tier is caller-supplied (an exchange-published list in
        # reality, not a policy percentage). Only NIFTY-style large-caps are
        # in NSE's current T0 tranche, so INE040A01034 is eligible but
        # INE062A01020 (also seeded as T0 by demo_seed) gets redirected back
        # to T+1 — demonstrating the tier filter actually excludes something.
        t0_trades = session.query(Trade).filter(Trade.settlement_cycle == SettlementCycle.T0).all()
        t0_eligible_trades, t0_redirected_trades = partition_t0_eligible_trades(
            t0_trades, eligible_isins={"INE040A01034"},
        )
        # Redirect: ineligible trades are relabeled T1 rather than vanishing
        # (this run's T+1 netting already happened in step 2, so a redirected
        # trade joins tomorrow's T+1 cycle, not today's already-computed one).
        for t in t0_redirected_trades:
            t.settlement_cycle = SettlementCycle.T1
        session.commit()

        # 14:00 — inside the 14:30 obligation cutoff, so the window is open.
        # compute_t0_obligations re-queries Trade itself, so it now only sees
        # the trades left tagged T0 after the redirect above.
        t0_obligations = compute_t0_obligations(session, current_time=clock_time(14, 0))
        # 16:00 — inside the 16:30 funds settlement cutoff, so these settle.
        settle_t0_funds(t0_obligations, clock_time(16, 0))
        session.commit()
        t0_summary = get_t0_summary(t0_obligations)
        t0_summary["redirected_to_t1"] = len(t0_redirected_trades)

        reference_prices = {
            NIFTY_FUT: Decimal("23850"), NIFTY_CE: Decimal("190"),
            RELIANCE_FUT: Decimal("1390"), RELIANCE_CE: Decimal("45"),
        }
        margin_held = {"BRK-001": Decimal("1200000"), "BRK-002": Decimal("900000")}
        stress_results = rank_top_n_stressed_cms(
            session, ["BRK-001", "BRK-002"], demo_date, Decimal("15"), reference_prices, margin_held, top_n=2
        )
        stress_summary = get_stress_summary(stress_results)
        worst_shortfall = stress_results[0].shortfall if stress_results else Decimal("0")

        # rank_top_n_stressed_cms above ranks each CM's stress loss alone —
        # it can't see that BRK-001 and BRK-002 are both stressed by the
        # SAME NIFTY move. identify_contagion_clusters surfaces that shared
        # exposure as a systemic concentration the SGF would face all at
        # once, not the uncorrelated sum the per-CM ranking implies.
        contagion_clusters = identify_contagion_clusters(
            session, ["BRK-001", "BRK-002"], demo_date, Decimal("15"), reference_prices,
        )
        contagion_summary = get_contagion_summary(contagion_clusters)

        waterfall_inputs = WaterfallInputs(
            defaulter_margin_collateral=Decimal("5000000"),
            defaulter_base_capital=Decimal("2000000"),
            defaulter_sgf_contribution=Decimal("1000000"),
            nse_sgf_contribution=Decimal("3000000"),
            other_cm_sgf_contributions={"BRK-002": Decimal("2000000"), "BRK-003": Decimal("500000")},
            nse_other_resources=Decimal("5000000"),
            insurance_cover=Decimal("1000000"),
        )
        waterfall_steps = run_default_waterfall(max(worst_shortfall, Decimal("15000000")), waterfall_inputs)
        waterfall_summary = get_waterfall_summary(waterfall_steps)

        ctd_bonds = [
            DeliverableBond("BOND-A", Decimal("7.1"), Decimal("9"), Decimal("99.50")),
            DeliverableBond("BOND-B", Decimal("8.0"), Decimal("9"), Decimal("105.20")),
            DeliverableBond("BOND-C", Decimal("6.5"), Decimal("9"), Decimal("95.80")),
        ]
        ctd = identify_cheapest_to_deliver(ctd_bonds, Decimal("100"), Decimal("7"))

        advanced_summary = {
            "cm_aggregated_obligations": cm_aggregation["obligation_count"],
            "cm_aggregated_value": float(cm_aggregation["total_value"]),
            "t0_obligations": t0_summary["total"],
            "t0_settled": t0_summary["settled"],
            "t0_failed": t0_summary["failed"],
            "t0_redirected_to_t1": t0_summary["redirected_to_t1"],
            "stress_cms_with_shortfall": stress_summary["cms_with_shortfall"],
            "contagion_cluster_count": contagion_summary["cluster_count"],
            "contagion_largest_cluster_underlying": contagion_summary["largest_cluster_underlying"],
            "waterfall_fully_covered": waterfall_summary["fully_covered"],
            "waterfall_final_shortfall": float(waterfall_summary["final_shortfall"]),
            "ctd_isin": ctd["isin"],
        }
        logger.info("step.complete", step=21, name="advanced_features", **advanced_summary)
    else:
        logger.info("step.skipped", step=21, name="advanced_features", reason="disabled")
    _check_shutdown("step_21")

    # ── Final Summary ───────────────────────────────────────────────────
    total_final = session.query(Obligation).filter(
        Obligation.obligation_stage == ObligationStage.FINAL
    ).count()
    settled = session.query(Obligation).filter(
        Obligation.status == ObligationStatus.SETTLED
    ).count()
    instructed = session.query(Obligation).filter(
        Obligation.status == ObligationStatus.INSTRUCTED
    ).count()
    stp_eligible = session.query(Obligation).filter(
        Obligation.obligation_stage == ObligationStage.FINAL,
        Obligation.status.in_([ObligationStatus.SETTLED, ObligationStatus.INSTRUCTED]),
    ).count()
    stp_rate = (stp_eligible / total_final * 100) if total_final > 0 else 0

    elapsed = round(time.monotonic() - start_time, 2)

    logger.info("pipeline.complete",
                elapsed_seconds=elapsed,
                total_trades=total_trades,
                total_obligations=total_final,
                settled=settled,
                instructed=instructed,
                stp_rate=round(stp_rate, 1),
                total_breaks=summary["total"],
                audit_entries=session.query(AgenticAuditLog).count(),
                ml_scored=len(ml_scores),
                penalties=len(penalty_assessments),
                iso20022_messages=msg_summary["total_messages"],
                scorecards=sc_summary["total"],
                liquidity_alerts=len(liquidity_report.alerts) if liquidity_report else 0,
                derivatives_mtm_positions=deriv_summary.get("mtm_positions", 0),
                margin_span_nifty=margin_summary.get("span_nifty_total", 0),
                debt_settled_trades=debt_summary.get("settled_trades", 0),
                advanced_t0_obligations=advanced_summary.get("t0_obligations", 0))

    session.close()


if __name__ == "__main__":
    run_pipeline()
