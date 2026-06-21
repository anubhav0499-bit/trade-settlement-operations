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
9.  Compute CSDR progressive penalties
10. Handle short deliveries (auction/close-out)
11. Run ML fail-risk prediction
12. Run agentic triage pipeline (dual-path)
13. Compute counterparty risk scorecards
14. Monitor intraday liquidity
15. Reconcile EOD positions
16. Generate reports
"""

import json
import os
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

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
    SettlementCycle,
    SourceSystem,
)


DATA_DIR = Path("data/generated")
DB_PATH = "data/generated/settlement.db"


def run_pipeline():
    print("=" * 70)
    print("  TRADE SETTLEMENT OPERATIONS PIPELINE")
    print("  Industry-Enhanced: ML Prediction | CSDR Penalties | ISO 20022")
    print("=" * 70)
    print()

    db_file = Path(DB_PATH)
    if db_file.exists():
        os.remove(db_file)

    engine = get_engine(DB_PATH)
    create_tables(engine)
    session = get_session(engine)

    # ── Step 1: Ingest & Normalize ──────────────────────────────────────
    print("[1/16] Trade Capture & Normalization")
    from src.ingestion.normalizer import ingest_all
    ingest_all(DATA_DIR, DB_PATH)

    total_trades = session.query(Trade).count()
    print(f"  Total trades in ledger: {total_trades}")
    print()

    # ── Step 2: Netting & Obligations ───────────────────────────────────
    print("[2/16] Netting & Obligation Engine")
    from src.netting.obligation_engine import compute_all_obligations, get_obligations_for_matching
    obligation_map = compute_all_obligations(session)
    total_obligations = session.query(Obligation).count()
    print(f"  Total obligations computed: {total_obligations}")
    print()

    # ── Step 3: SSI Validation ──────────────────────────────────────────
    print("[3/16] SSI Golden-Copy Validation")
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
    print(f"  SSI-validated obligations: {len(valid_obs)}")
    print(f"  SSI breaks detected: {len(ssi_breaks)}")
    print()

    # ── Step 4: Matching Engine ─────────────────────────────────────────
    print("[4/16] Matching Engine")
    from src.matching.engine import match_obligations, create_break_records

    internal_final, broker_final, custodian_final = get_obligations_for_matching(session)
    print(f"  Internal obligations: {len(internal_final)}")
    print(f"  Broker obligations: {len(broker_final)}")
    print(f"  Custodian obligations: {len(custodian_final)}")

    broker_results = match_obligations(internal_final, broker_final)
    matched_broker = sum(1 for r in broker_results if r.status == MatchStatus.MATCHED)
    broken_broker = sum(1 for r in broker_results if r.status == MatchStatus.BREAK)
    unmatched_broker = sum(1 for r in broker_results if r.status == MatchStatus.UNMATCHED)

    print(f"  vs Broker: {matched_broker} matched, {broken_broker} breaks, {unmatched_broker} unmatched")

    ob_by_id = {ob.obligation_id: ob for ob in internal_final + broker_final}
    broker_break_records = create_break_records(session, broker_results, ob_by_id)
    print(f"  Broker break records created: {len(broker_break_records)}")
    print()

    # ── Step 5: Custodian Confirmation ──────────────────────────────────
    print("[5/16] Custodian Confirmation")
    from src.confirmation.custodian_confirm import process_confirmations, simulate_confirmation_responses

    matched_obs = (
        session.query(Obligation)
        .filter(Obligation.status == ObligationStatus.MATCHED)
        .all()
    )
    print(f"  Matched obligations for confirmation: {len(matched_obs)}")

    responses = simulate_confirmation_responses(matched_obs)
    current_time = datetime(2026, 6, 2, 12, 30)

    confirmed, problems, late_breaks = process_confirmations(
        session, matched_obs, responses, current_time
    )
    print(f"  Confirmed: {len(confirmed)}")
    print(f"  Late/rejected: {len(problems)}")
    print(f"  Late confirmation breaks: {len(late_breaks)}")
    print()

    # ── Step 6: Settlement Instructions ─────────────────────────────────
    print("[6/16] Settlement Instruction Generation")
    from src.instruction.settlement_instruction import generate_all_instructions

    confirmed_obs = (
        session.query(Obligation)
        .filter(Obligation.status == ObligationStatus.CONFIRMED)
        .all()
    )
    instructions = generate_all_instructions(session, confirmed_obs)
    print(f"  Instructions generated: {len(instructions)}")
    print()

    # ── Step 7: ISO 20022 Message Formatting ────────────────────────────
    print("[7/16] ISO 20022 Message Formatting (sese.023)")
    from src.instruction.iso20022_formatter import format_batch, get_message_summary

    iso_messages = format_batch(instructions)
    msg_summary = get_message_summary(iso_messages)
    print(f"  ISO 20022 messages generated: {msg_summary['total_messages']}")
    print(f"  DELIVER instructions: {msg_summary['deliver_instructions']}")
    print(f"  RECEIVE instructions: {msg_summary['receive_instructions']}")
    print(f"  Format: {msg_summary['format']} ({msg_summary['message_type']})")
    print()

    # ── Step 8: Break Detection & Classification ────────────────────────
    print("[8/16] Break Detection & Classification")
    from src.breaks.rules_engine import update_break_aging, get_break_summary

    updated_breaks = update_break_aging(session, current_time)
    summary = get_break_summary(session)
    print(f"  Total breaks: {summary['total']}")
    print(f"  By type: {json.dumps(summary['by_type'], indent=4)}")
    print(f"  By severity: {json.dumps(summary['by_severity'], indent=4)}")
    print()

    # ── Step 9: Auction / Close-out ────────────────────────────────────
    print("[9/16] Auction & Close-Out (simulated)")
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

    print(f"  Simulated short deliveries: {len(shorts)}")
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
            print(f"    {ob.isin}: Auction SUCCESS")
        else:
            execute_auction(
                session, auction,
                auction_price=None,
                closing_price_auction_day=Decimal(str(ob.vwap_price)),
                highest_price_trade_to_auction=Decimal(str(ob.vwap_price)) * Decimal("1.03"),
            )
            print(f"    {ob.isin}: CLOSED OUT")
    print()

    # ── Step 10: CSDR Progressive Penalties ─────────────────────────────
    print("[10/16] CSDR Progressive Cash Penalties")
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

        print(f"  Failed obligations assessed: {penalty_summary['total_fails']}")
        print(f"  Total penalties: INR {float(penalty_summary['total_penalties']):,.2f}")
        print(f"  By tier: {penalty_summary['by_tier']}")
        print(f"  By direction: {penalty_summary['by_direction']}")
    else:
        penalty_assessments = []
        print("  No failed obligations to assess")
    print()

    # ── Step 11: ML Fail-Risk Prediction ────────────────────────────────
    print("[11/16] ML-Based Fail-Risk Prediction (Gradient Boosted Classifier)")
    from src.triage.ml_fail_predictor import (
        predict_fail_risk_batch,
        get_ml_high_risk_queue,
        train_model,
    )

    print("  Training GBM model on synthetic historical data (5,000 samples)...")
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
    ml_high_risk = get_ml_high_risk_queue(ml_scores, threshold=0.3)

    print(f"  Obligations scored: {len(ml_scores)}")
    print(f"  High risk (>30% fail probability): {len(ml_high_risk)}")
    if ml_scores:
        avg_prob = sum(s.fail_probability for s in ml_scores) / len(ml_scores)
        print(f"  Avg fail probability: {avg_prob:.1%}")
        print(f"  Model version: {ml_scores[0].model_version}")
    if ml_high_risk:
        top = ml_high_risk[0]
        print(f"  Highest risk: {top.obligation_id[:12]}... @ {top.fail_probability:.1%}")
    print()

    # ── Step 12: Agentic Triage Pipeline ────────────────────────────────
    print("[12/16] Agentic Triage Pipeline (LangGraph)")

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

    print(f"  Path A input: {len(obligation_dicts)} obligations for fail-risk scan")
    print(f"  Path B input: {len(break_dicts)} breaks for triage")

    print("  Building FAISS knowledge base index...")
    from src.triage.knowledge_base import build_index
    build_index()

    from src.triage.pipeline import run_triage
    triage_result = run_triage(
        obligations=obligation_dicts,
        breaks=break_dicts,
    )

    print(f"  Path: {triage_result['path']}")
    print(f"  Fail-risk scores computed: {len(triage_result['fail_risk_scores'])}")
    print(f"  High-risk queue: {len(triage_result['high_risk_queue'])}")
    print(f"  Triage results: {len(triage_result['triage_results'])}")
    print(f"  Audit log entries: {len(triage_result['audit_logs'])}")

    for log in triage_result["audit_logs"]:
        audit = AgenticAuditLog(
            log_id=log["log_id"],
            obligation_id=log.get("obligation_id"),
            break_id=log.get("break_id"),
            node_name=log["node_name"],
            inputs=log["inputs"],
            conclusion=log["conclusion"],
            rationale=log["rationale"],
            timestamp=datetime.fromisoformat(log["timestamp"]),
        )
        session.add(audit)
    session.commit()
    print(f"  Persisted {len(triage_result['audit_logs'])} audit log entries")
    print()

    # ── Step 13: Counterparty Risk Scorecards ───────────────────────────
    print("[13/16] Counterparty Risk Scorecards")
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

    print(f"  Counterparties rated: {sc_summary['total']}")
    print(f"  Grade distribution: {sc_summary['by_grade']}")
    print(f"  Average score: {sc_summary['avg_score']}/100")
    print(f"  Watch list: {len(watch_list)} counterparties")
    for sc in sorted(scorecards, key=lambda s: s.composite_score, reverse=True)[:5]:
        print(f"    {sc.counterparty_id}: {sc.composite_score}/100 (Grade {sc.letter_grade})")
    print()

    # ── Step 14: Intraday Liquidity Monitoring ──────────────────────────
    print("[14/16] Intraday Liquidity Monitoring")
    from src.liquidity.intraday_monitor import generate_intraday_report

    liquidity_report = generate_intraday_report(
        session,
        settlement_date=date(2026, 6, 2),
        current_time=current_time,
    )

    snap = liquidity_report.current_snapshot
    print(f"  Net position: INR {float(snap.net_position):,.0f}")
    print(f"  Gross pay-in: INR {float(snap.gross_pay_in):,.0f}")
    print(f"  Gross pay-out: INR {float(snap.gross_pay_out):,.0f}")
    print(f"  Buffer utilization: {snap.buffer_utilization:.1f}%")
    print(f"  Settlement progress: {liquidity_report.settlement_progress:.1f}%")
    print(f"  Active alerts: {len(liquidity_report.alerts)}")
    for alert in liquidity_report.alerts:
        print(f"    [{alert.severity}] {alert.alert_type}: {alert.message[:80]}")
    print(f"  Counterparty exposures tracked: {len(liquidity_report.counterparty_exposures)}")
    print()

    # ── Step 15: Reconciliation ─────────────────────────────────────────
    print("[15/16] EOD Position Reconciliation")
    from src.reconciliation.position_recon import reconcile_positions, get_recon_summary

    settled_count = 0
    for ob in confirmed_obs[:20]:
        ob.status = ObligationStatus.SETTLED
        settled_count += 1
    session.commit()

    recon_results = reconcile_positions(session, date(2026, 6, 2))
    recon_summary = get_recon_summary(recon_results)
    print(f"  Positions compared: {recon_summary['total_positions']}")
    print(f"  Reconciled: {recon_summary['reconciled']}")
    print(f"  Unreconciled: {recon_summary['unreconciled']}")
    print(f"  Recon rate: {recon_summary['recon_rate']:.1f}%")
    print()

    # ── Step 16: Reporting ──────────────────────────────────────────────
    print("[16/16] Report Generation")
    from src.reporting.report_generator import generate_reports

    generate_reports(
        session=session,
        triage_result=triage_result,
        recon_results=recon_results,
        output_dir=DATA_DIR,
    )

    # ── Final Summary ───────────────────────────────────────────────────
    print()
    print("=" * 70)
    print("  PIPELINE COMPLETE — Industry-Enhanced")
    print("=" * 70)

    final_summary = get_break_summary(session)
    total_obs = session.query(Obligation).filter(
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

    total_final = session.query(Obligation).filter(
        Obligation.obligation_stage == ObligationStage.FINAL
    ).count()

    stp_rate = (stp_eligible / total_final * 100) if total_final > 0 else 0

    print(f"  Total trades ingested: {total_trades}")
    print(f"  Total obligations (final): {total_final}")
    print(f"  Settled: {settled}")
    print(f"  Instructed: {instructed}")
    print(f"  STP rate: {stp_rate:.1f}%")
    print(f"  Total breaks: {final_summary['total']}")
    print(f"  Audit log entries: {session.query(AgenticAuditLog).count()}")
    print()
    print("  Industry Enhancements Active:")
    print(f"    ML fail prediction: {len(ml_scores)} obligations scored")
    print(f"    CSDR penalties: {len(penalty_assessments)} assessments")
    print(f"    ISO 20022 messages: {msg_summary['total_messages']} sese.023 generated")
    print(f"    Counterparty scorecards: {sc_summary['total']} rated (avg {sc_summary['avg_score']}/100)")
    print(f"    Liquidity alerts: {len(liquidity_report.alerts)} active")
    print()

    session.close()


if __name__ == "__main__":
    run_pipeline()
