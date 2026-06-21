"""
Pipeline orchestrator — runs the full trade settlement operations pipeline.

Execution order:
1. Ingest & normalize trades from 3 sources
2. Compute net obligations (provisional + final)
3. Validate SSI against golden copy
4. Match internal vs counterparty obligations
5. Process custodian confirmations
6. Generate settlement instructions
7. Detect breaks & classify
8. Handle short deliveries (auction/close-out)
9. Run agentic triage pipeline (dual-path)
10. Reconcile EOD positions
11. Generate reports
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
    print("=" * 70)
    print()

    # Remove existing DB to start fresh
    db_file = Path(DB_PATH)
    if db_file.exists():
        os.remove(db_file)

    engine = get_engine(DB_PATH)
    create_tables(engine)
    session = get_session(engine)

    # ── Step 1: Ingest & Normalize ──────────────────────────────────────
    print("[1/11] Trade Capture & Normalization")
    from src.ingestion.normalizer import ingest_all
    ingest_all(DATA_DIR, DB_PATH)

    total_trades = session.query(Trade).count()
    print(f"  Total trades in ledger: {total_trades}")
    print()

    # ── Step 2: Netting & Obligations ───────────────────────────────────
    print("[2/11] Netting & Obligation Engine")
    from src.netting.obligation_engine import compute_all_obligations, get_obligations_for_matching
    obligation_map = compute_all_obligations(session)
    total_obligations = session.query(Obligation).count()
    print(f"  Total obligations computed: {total_obligations}")
    print()

    # ── Step 3: SSI Validation ──────────────────────────────────────────
    print("[3/11] SSI Golden-Copy Validation")
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
    print("[4/11] Matching Engine")
    from src.matching.engine import match_obligations, create_break_records

    internal_final, broker_final, custodian_final = get_obligations_for_matching(session)
    print(f"  Internal obligations: {len(internal_final)}")
    print(f"  Broker obligations: {len(broker_final)}")
    print(f"  Custodian obligations: {len(custodian_final)}")

    # Match internal vs broker
    broker_results = match_obligations(internal_final, broker_final)
    matched_broker = sum(1 for r in broker_results if r.status == MatchStatus.MATCHED)
    broken_broker = sum(1 for r in broker_results if r.status == MatchStatus.BREAK)
    unmatched_broker = sum(1 for r in broker_results if r.status == MatchStatus.UNMATCHED)

    print(f"  vs Broker: {matched_broker} matched, {broken_broker} breaks, {unmatched_broker} unmatched")

    # Create break records for broker matches
    ob_by_id = {ob.obligation_id: ob for ob in internal_final + broker_final}
    broker_break_records = create_break_records(session, broker_results, ob_by_id)
    print(f"  Broker break records created: {len(broker_break_records)}")
    print()

    # ── Step 5: Custodian Confirmation ──────────────────────────────────
    print("[5/11] Custodian Confirmation")
    from src.confirmation.custodian_confirm import process_confirmations, simulate_confirmation_responses

    matched_obs = (
        session.query(Obligation)
        .filter(Obligation.status == ObligationStatus.MATCHED)
        .all()
    )
    print(f"  Matched obligations for confirmation: {len(matched_obs)}")

    responses = simulate_confirmation_responses(matched_obs)
    current_time = datetime(2026, 6, 2, 12, 30)  # simulated current time

    confirmed, problems, late_breaks = process_confirmations(
        session, matched_obs, responses, current_time
    )
    print(f"  Confirmed: {len(confirmed)}")
    print(f"  Late/rejected: {len(problems)}")
    print(f"  Late confirmation breaks: {len(late_breaks)}")
    print()

    # ── Step 6: Settlement Instructions ─────────────────────────────────
    print("[6/11] Settlement Instruction Generation")
    from src.instruction.settlement_instruction import generate_all_instructions

    confirmed_obs = (
        session.query(Obligation)
        .filter(Obligation.status == ObligationStatus.CONFIRMED)
        .all()
    )
    instructions = generate_all_instructions(session, confirmed_obs)
    print(f"  Instructions generated: {len(instructions)}")
    print()

    # ── Step 7: Break Detection & Classification ────────────────────────
    print("[7/11] Break Detection & Classification")
    from src.breaks.rules_engine import update_break_aging, get_break_summary

    updated_breaks = update_break_aging(session, current_time)
    summary = get_break_summary(session)
    print(f"  Total breaks: {summary['total']}")
    print(f"  By type: {json.dumps(summary['by_type'], indent=4)}")
    print(f"  By severity: {json.dumps(summary['by_severity'], indent=4)}")
    print()

    # ── Step 8: Auction / Close-out ─────────────────────────────────────
    print("[8/11] Auction & Close-Out (simulated)")
    from src.auction.close_out import detect_short_deliveries, initiate_auction, execute_auction

    instructed_obs = (
        session.query(Obligation)
        .filter(Obligation.status == ObligationStatus.INSTRUCTED)
        .all()
    )

    # Simulate some short deliveries (mark a few as failed)
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
        # Simulate auction execution (50% success rate)
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

    # ── Step 9: Agentic Triage Pipeline ─────────────────────────────────
    print("[9/11] Agentic Triage Pipeline (LangGraph)")

    # Prepare obligations for Path A (fail-risk scan)
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

    # Prepare breaks for Path B (triage)
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

    # Build KB index
    print("  Building FAISS knowledge base index...")
    from src.triage.knowledge_base import build_index
    build_index()

    # Run triage
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

    # Persist audit logs
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

    # ── Step 10: Reconciliation ─────────────────────────────────────────
    print("[10/11] EOD Position Reconciliation")
    from src.reconciliation.position_recon import reconcile_positions, get_recon_summary

    # Simulate some settled obligations for recon
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

    # ── Step 11: Reporting ──────────────────────────────────────────────
    print("[11/11] Report Generation")
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
    print("  PIPELINE COMPLETE")
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

    # STP rate: obligations that reached settlement with zero manual touches
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

    session.close()


if __name__ == "__main__":
    run_pipeline()
