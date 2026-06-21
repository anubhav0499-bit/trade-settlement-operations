"""
Streamlit Dashboard — Trade Settlement Operations.

Tabs:
  1. Break Queue — filterable break list with human approval workflow
  2. Break Analysis — charts by type, severity, counterparty
  3. ML Risk Scores — fail-probability predictions from the GBM model
  4. Counterparty Scorecard — composite risk ratings per counterparty
  5. Penalty Tracker — CSDR-style progressive cash penalties
  6. Liquidity Monitor — intraday fund flows, velocity, and alerts
  7. Audit Trail — agentic reasoning chain log
  8. Reconciliation — EOD position recon + auctions
"""

import json
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.models.database import (
    AgenticAuditLog,
    AuctionRecord,
    BreakRecord,
    Obligation,
    Trade,
    create_tables,
    get_engine,
    get_session,
)
from src.models.enums import (
    BreakStatus,
    BreakType,
    MatchStatus,
    ObligationStage,
    ObligationStatus,
    Severity,
)


DB_PATH = "data/generated/settlement.db"


@st.cache_resource
def get_db_session():
    engine = get_engine(DB_PATH)
    return get_session(engine)


def load_break_data(session) -> pd.DataFrame:
    breaks = session.query(BreakRecord).all()
    rows = []
    for b in breaks:
        ob = session.query(Obligation).filter(
            Obligation.obligation_id == b.obligation_id
        ).first()
        rows.append({
            "break_id": b.break_id[:12],
            "obligation_id": b.obligation_id[:12],
            "isin": ob.isin if ob else "",
            "security": ob.security_name if ob else "",
            "counterparty": ob.counterparty_id if ob else "",
            "break_type": b.break_type.value,
            "severity": b.severity.value,
            "value_at_risk": float(b.value_at_risk or 0),
            "age_days": b.age_days or 0,
            "age_hours": round(b.age_hours or 0, 1),
            "status": b.status.value,
            "escalation": b.escalation_level,
            "recommended_action": b.recommended_action or "",
            "full_break_id": b.break_id,
        })
    return pd.DataFrame(rows)


def main():
    st.set_page_config(
        page_title="Settlement Ops Dashboard",
        page_icon="📊",
        layout="wide",
    )

    st.title("Trade Settlement Operations Dashboard")
    st.caption("NSE/BSE Equity Settlement — T+1 / T+0 | Industry-Enhanced")

    session = get_db_session()

    # ── Sidebar Filters ─────────────────────────────────────────────────
    st.sidebar.header("Filters")

    break_df = load_break_data(session)

    statuses = ["All"] + sorted(break_df["status"].unique().tolist()) if not break_df.empty else ["All"]
    selected_status = st.sidebar.selectbox("Status", statuses)

    types = ["All"] + sorted(break_df["break_type"].unique().tolist()) if not break_df.empty else ["All"]
    selected_type = st.sidebar.selectbox("Break Type", types)

    severities = ["All"] + sorted(break_df["severity"].unique().tolist()) if not break_df.empty else ["All"]
    selected_severity = st.sidebar.selectbox("Severity", severities)

    counterparties = ["All"] + sorted(break_df["counterparty"].unique().tolist()) if not break_df.empty else ["All"]
    selected_cp = st.sidebar.selectbox("Counterparty", counterparties)

    filtered = break_df.copy()
    if selected_status != "All":
        filtered = filtered[filtered["status"] == selected_status]
    if selected_type != "All":
        filtered = filtered[filtered["break_type"] == selected_type]
    if selected_severity != "All":
        filtered = filtered[filtered["severity"] == selected_severity]
    if selected_cp != "All":
        filtered = filtered[filtered["counterparty"] == selected_cp]

    # ── KPI Row ─────────────────────────────────────────────────────────
    col1, col2, col3, col4, col5, col6 = st.columns(6)

    total_trades = session.query(Trade).filter(Trade.source_system == "OMS").count()
    total_obs = session.query(Obligation).filter(
        Obligation.obligation_stage == ObligationStage.FINAL
    ).count()
    stp_count = session.query(Obligation).filter(
        Obligation.obligation_stage == ObligationStage.FINAL,
        Obligation.status.in_([ObligationStatus.SETTLED, ObligationStatus.INSTRUCTED]),
    ).count()
    stp_rate = (stp_count / total_obs * 100) if total_obs > 0 else 0

    failed_count = session.query(Obligation).filter(
        Obligation.obligation_stage == ObligationStage.FINAL,
        Obligation.status.in_([ObligationStatus.FAILED, ObligationStatus.AUCTION, ObligationStatus.CLOSED_OUT]),
    ).count()

    col1.metric("Total Trades", f"{total_trades:,}")
    col2.metric("Obligations", f"{total_obs:,}")
    col3.metric("STP Rate", f"{stp_rate:.1f}%")
    col4.metric("Total Breaks", len(break_df))
    col5.metric("High Severity", len(break_df[break_df["severity"] == "HIGH"]) if not break_df.empty else 0)
    col6.metric("Settlement Fails", failed_count)

    st.divider()

    # ── Tabs ────────────────────────────────────────────────────────────
    tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8 = st.tabs([
        "Break Queue",
        "Break Analysis",
        "ML Risk Scores",
        "Counterparty Scorecard",
        "Penalty Tracker",
        "Liquidity Monitor",
        "Audit Trail",
        "Reconciliation",
    ])

    # ── Tab 1: Break Queue ──────────────────────────────────────────────
    with tab1:
        st.subheader(f"Break Queue ({len(filtered)} breaks)")

        if not filtered.empty:
            display_cols = [
                "break_type", "severity", "isin", "security",
                "counterparty", "value_at_risk", "age_days",
                "status", "escalation",
            ]
            st.dataframe(
                filtered[display_cols],
                use_container_width=True,
                height=400,
            )

            st.subheader("Human Approval")
            pending = filtered[filtered["status"] == "OPEN"]
            if not pending.empty:
                selected_break = st.selectbox(
                    "Select break to resolve",
                    pending["full_break_id"].tolist(),
                    format_func=lambda x: f"{x[:12]}... — {pending[pending['full_break_id']==x].iloc[0]['break_type']}",
                )

                if selected_break:
                    brk_row = pending[pending["full_break_id"] == selected_break].iloc[0]
                    st.write(f"**Type:** {brk_row['break_type']}")
                    st.write(f"**Severity:** {brk_row['severity']}")
                    st.write(f"**Recommended Action:** {brk_row['recommended_action'][:300]}")

                    resolution = st.text_area("Resolution Notes")
                    if st.button("Approve Resolution", type="primary"):
                        brk_record = session.query(BreakRecord).filter(
                            BreakRecord.break_id == selected_break
                        ).first()
                        if brk_record:
                            brk_record.status = BreakStatus.RESOLVED
                            brk_record.resolution_notes = resolution
                            brk_record.resolved_by = "dashboard_user"
                            brk_record.resolved_at = datetime.utcnow()
                            session.commit()
                            st.success("Break resolved successfully!")
                            st.rerun()
            else:
                st.info("No pending breaks to approve.")
        else:
            st.info("No breaks match the selected filters.")

    # ── Tab 2: Break Analysis ───────────────────────────────────────────
    with tab2:
        st.subheader("Break Analysis")

        if not break_df.empty:
            col_a, col_b = st.columns(2)

            with col_a:
                st.write("**Breaks by Type**")
                type_counts = break_df["break_type"].value_counts()
                st.bar_chart(type_counts)

            with col_b:
                st.write("**Breaks by Severity**")
                sev_counts = break_df["severity"].value_counts()
                st.bar_chart(sev_counts)

            st.write("**Top Counterparties by Break Volume**")
            cp_counts = break_df["counterparty"].value_counts().head(10)
            st.bar_chart(cp_counts)

    # ── Tab 3: ML Risk Scores ───────────────────────────────────────────
    with tab3:
        st.subheader("ML-Based Settlement Fail Prediction")
        st.caption("Gradient Boosted Classifier — 13-feature model trained on synthetic historical data")

        try:
            from src.triage.ml_fail_predictor import predict_fail_risk_batch, get_ml_high_risk_queue

            pending_obs = (
                session.query(Obligation)
                .filter(Obligation.status.in_([
                    ObligationStatus.PENDING,
                    ObligationStatus.SSI_VALIDATED,
                    ObligationStatus.MATCHED,
                    ObligationStatus.CONFIRMED,
                    ObligationStatus.INSTRUCTED,
                ]))
                .limit(100)
                .all()
            )

            if pending_obs:
                scores = predict_fail_risk_batch(pending_obs, datetime(2026, 6, 2, 12, 30))
                high_risk = get_ml_high_risk_queue(scores, threshold=0.3)

                ml_col1, ml_col2, ml_col3 = st.columns(3)
                ml_col1.metric("Obligations Scored", len(scores))
                ml_col2.metric("High Risk (>30%)", len(high_risk))
                avg_prob = sum(s.fail_probability for s in scores) / len(scores) if scores else 0
                ml_col3.metric("Avg Fail Probability", f"{avg_prob:.1%}")

                risk_rows = []
                for s in scores:
                    ob = session.query(Obligation).filter(
                        Obligation.obligation_id == s.obligation_id
                    ).first()
                    risk_rows.append({
                        "obligation_id": s.obligation_id[:12],
                        "isin": ob.isin if ob else "",
                        "counterparty": ob.counterparty_id if ob else "",
                        "fail_probability": f"{s.fail_probability:.1%}",
                        "risk_tier": s.risk_tier,
                        "model_version": s.model_version,
                    })

                risk_df = pd.DataFrame(risk_rows)
                risk_df = risk_df.sort_values("fail_probability", ascending=False)
                st.dataframe(risk_df, use_container_width=True, height=400)

                st.write("**Feature Importance (top contributors)**")
                if scores:
                    importances = scores[0].feature_contributions
                    imp_df = pd.DataFrame([
                        {"Feature": k, "Importance": v["importance"]}
                        for k, v in importances.items()
                    ]).sort_values("Importance", ascending=False)
                    st.bar_chart(imp_df.set_index("Feature")["Importance"])
            else:
                st.info("No pending obligations to score.")
        except Exception as e:
            st.warning(f"ML predictor not available: {e}")

    # ── Tab 4: Counterparty Scorecard ───────────────────────────────────
    with tab4:
        st.subheader("Counterparty Risk Scorecard")
        st.caption("Composite scoring: Settlement Efficiency + Fail History + Break Frequency + Timeliness + Concentration")

        try:
            from src.risk.counterparty_scorecard import compute_all_scorecards, get_scorecard_summary

            cp_ids = [
                r[0] for r in
                session.query(Obligation.counterparty_id)
                .filter(Obligation.obligation_stage == ObligationStage.FINAL)
                .distinct()
                .all()
            ]

            if cp_ids:
                scorecards = compute_all_scorecards(session, cp_ids)
                summary = get_scorecard_summary(scorecards)

                sc_col1, sc_col2, sc_col3, sc_col4 = st.columns(4)
                sc_col1.metric("Counterparties Rated", summary["total"])
                sc_col2.metric("Avg Score", f"{summary['avg_score']:.0f}/100")
                sc_col3.metric("Watch List", summary["watch_list_count"])
                grade_a = summary["by_grade"].get("A", 0)
                sc_col4.metric("Grade A", grade_a)

                sc_rows = []
                for sc in sorted(scorecards, key=lambda s: s.composite_score, reverse=True):
                    sc_rows.append({
                        "counterparty": sc.counterparty_id,
                        "score": sc.composite_score,
                        "grade": sc.letter_grade,
                        "exposure_multiplier": f"{sc.exposure_limit_multiplier:.1f}x",
                        "watch_list": "YES" if sc.watch_list else "",
                        "settlement_eff": next(d.score for d in sc.dimensions if d.name == "Settlement Efficiency"),
                        "fail_history": next(d.score for d in sc.dimensions if d.name == "Fail History"),
                        "break_freq": next(d.score for d in sc.dimensions if d.name == "Break Frequency"),
                    })

                sc_df = pd.DataFrame(sc_rows)
                st.dataframe(sc_df, use_container_width=True, height=400)

                st.write("**Grade Distribution**")
                grade_df = pd.DataFrame([
                    {"Grade": g, "Count": c}
                    for g, c in sorted(summary["by_grade"].items())
                ])
                if not grade_df.empty:
                    st.bar_chart(grade_df.set_index("Grade")["Count"])
            else:
                st.info("No counterparty data available.")
        except Exception as e:
            st.warning(f"Scorecard module not available: {e}")

    # ── Tab 5: Penalty Tracker ──────────────────────────────────────────
    with tab5:
        st.subheader("CSDR-Style Progressive Settlement Penalties")
        st.caption("Daily escalating cash penalties for settlement fails — rates increase with aging")

        try:
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
                fail_pairs = [
                    (ob, ob.settlement_date) for ob in failed_obs
                ]
                assessments = compute_penalties_batch(fail_pairs, assessment_date)
                summary = get_penalty_summary(assessments)
                cp_agg = aggregate_by_counterparty(assessments)

                p_col1, p_col2, p_col3 = st.columns(3)
                p_col1.metric("Total Penalties", f"INR {float(summary['total_penalties']):,.2f}")
                p_col2.metric("Fail Count", summary["total_fails"])
                p_col3.metric("Avg per Fail", f"INR {float(summary['avg_penalty_per_fail']):,.2f}")

                pen_rows = []
                for a in assessments:
                    pen_rows.append({
                        "obligation": a.obligation_id[:12],
                        "counterparty": a.counterparty_id,
                        "isin": a.isin,
                        "direction": a.fail_direction,
                        "fail_days": a.total_fail_days,
                        "penalty_inr": float(a.total_penalty),
                        "tier": a.penalty_tier,
                    })

                pen_df = pd.DataFrame(pen_rows)
                st.dataframe(pen_df, use_container_width=True)

                st.write("**Penalties by Counterparty**")
                cp_rows = []
                for cp_id, data in cp_agg.items():
                    cp_rows.append({
                        "counterparty": cp_id,
                        "total_penalty": float(data["total_penalty"]),
                        "fail_count": data["fail_count"],
                        "avg_fail_days": data["avg_fail_days"],
                    })
                if cp_rows:
                    cp_df = pd.DataFrame(cp_rows).sort_values("total_penalty", ascending=False)
                    st.bar_chart(cp_df.set_index("counterparty")["total_penalty"])
            else:
                st.info("No settlement fails to assess penalties for.")
        except Exception as e:
            st.warning(f"Penalty module not available: {e}")

    # ── Tab 6: Liquidity Monitor ────────────────────────────────────────
    with tab6:
        st.subheader("Intraday Liquidity Monitor")
        st.caption("Real-time settlement flow tracking with programmable alerts")

        try:
            from src.liquidity.intraday_monitor import generate_intraday_report

            report = generate_intraday_report(
                session,
                settlement_date=date(2026, 6, 2),
                current_time=datetime(2026, 6, 2, 14, 30),
            )

            snap = report.current_snapshot

            lq_col1, lq_col2, lq_col3, lq_col4 = st.columns(4)
            lq_col1.metric("Net Position", f"INR {float(snap.net_position):,.0f}")
            lq_col2.metric("Buffer Usage", f"{snap.buffer_utilization:.1f}%")
            lq_col3.metric("Settlement Progress", f"{report.settlement_progress:.1f}%")
            lq_col4.metric("Active Alerts", len(report.alerts))

            flow_col1, flow_col2 = st.columns(2)
            with flow_col1:
                st.write("**Settlement Flows**")
                flow_data = pd.DataFrame([
                    {"Flow": "Gross Pay-In (Deliver)", "Value": float(snap.gross_pay_in)},
                    {"Flow": "Gross Pay-Out (Receive)", "Value": float(snap.gross_pay_out)},
                    {"Flow": "Settled", "Value": float(snap.settled_value)},
                    {"Flow": "Pending", "Value": float(snap.pending_value)},
                ])
                st.dataframe(flow_data, use_container_width=True, hide_index=True)

            with flow_col2:
                st.write("**Top Counterparty Exposures**")
                exp_rows = []
                for exp in report.counterparty_exposures[:10]:
                    exp_rows.append({
                        "counterparty": exp.counterparty_id,
                        "gross_exposure": float(exp.gross_exposure),
                        "net_exposure": float(exp.net_exposure),
                        "pending": exp.pending_count,
                    })
                if exp_rows:
                    st.dataframe(pd.DataFrame(exp_rows), use_container_width=True, hide_index=True)

            if report.alerts:
                st.write("**Active Alerts**")
                alert_rows = []
                for a in report.alerts:
                    alert_rows.append({
                        "alert_id": a.alert_id,
                        "type": a.alert_type,
                        "severity": a.severity,
                        "message": a.message,
                    })
                st.dataframe(pd.DataFrame(alert_rows), use_container_width=True, hide_index=True)

        except Exception as e:
            st.warning(f"Liquidity monitor not available: {e}")

    # ── Tab 7: Audit Trail ──────────────────────────────────────────────
    with tab7:
        st.subheader("Agentic Audit Trail")

        audit_logs = session.query(AgenticAuditLog).order_by(
            AgenticAuditLog.timestamp.desc()
        ).limit(100).all()

        if audit_logs:
            audit_rows = []
            for log in audit_logs:
                audit_rows.append({
                    "timestamp": log.timestamp.strftime("%H:%M:%S"),
                    "node": log.node_name,
                    "obligation": (log.obligation_id or "")[:12],
                    "conclusion": log.conclusion[:100],
                    "rationale": log.rationale[:150],
                })
            st.dataframe(pd.DataFrame(audit_rows), use_container_width=True, height=400)

    # ── Tab 8: Reconciliation ───────────────────────────────────────────
    with tab8:
        st.subheader("Position Reconciliation")
        st.info(
            "Reconciliation compares internally derived positions (from settled obligations) "
            "against custodian EOD holding statements. Run the pipeline to generate recon data."
        )

        auctions = session.query(AuctionRecord).all()
        if auctions:
            st.subheader("Auctions & Close-Outs")
            auction_rows = []
            for a in auctions:
                auction_rows.append({
                    "isin": a.isin,
                    "short_qty": a.short_quantity,
                    "valuation_price": float(a.valuation_price),
                    "auction_price": float(a.auction_price) if a.auction_price else None,
                    "close_out_price": float(a.close_out_price) if a.close_out_price else None,
                    "penalty": float(a.penalty_amount),
                    "outcome": a.outcome.value if a.outcome else "PENDING",
                })
            st.dataframe(pd.DataFrame(auction_rows), use_container_width=True)


if __name__ == "__main__":
    main()
