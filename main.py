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
"""

import json
import os
import signal
import sys
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from src.logging_config import get_logger, setup_logging
from src.settings import (
    DATA_DIR,
    DATABASE_URL,
    ENABLE_CSDR_PENALTIES,
    ENABLE_ISO20022,
    ENABLE_LIQUIDITY_MONITOR,
    ENABLE_ML_PREDICTION,
    ENABLE_SCORECARDS,
    ML_RISK_THRESHOLD,
)
from src.models.database import (
    AgenticAuditLog,
    BreakRecord,
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
    SourceSystem,
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
    obligation_map = compute_all_obligations(session)
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

    updated_breaks = update_break_aging(session, current_time)
    summary = get_break_summary(session)
    logger.info("step.complete", step=8, name="break_detection",
                total_breaks=summary["total"],
                by_type=summary["by_type"],
                by_severity=summary["by_severity"])
    _check_shutdown("step_8")

    # ── Step 9: Auction / Close-out ────────────────────────────────────
    logger.info("step.start", step=9, name="auction")
    from src.auction.close_out import detect_short_deliveries, initiate_auction, execute_auction

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
        from src.penalties.csdr_penalties import (
            compute_penalties_batch,
            aggregate_by_counterparty,
            get_penalty_summary,
        )

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
            cp_penalties = aggregate_by_counterparty(penalty_assessments)
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

        ml_model = train_model()

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
                liquidity_alerts=len(liquidity_report.alerts) if liquidity_report else 0)

    session.close()


if __name__ == "__main__":
    run_pipeline()
