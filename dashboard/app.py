"""
Streamlit Dashboard (§13).

Break queue with filtering by status/type/severity/counterparty,
human approval workflow, fail-risk queue, and reconciliation view.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

# Add project root to path
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
    st.caption("NSE/BSE Equity Settlement — T+1 / T+0")

    session = get_db_session()

    # ── Sidebar Filters ─────────────────────────────────────────────────
    st.sidebar.header("Filters")

    break_df = load_break_data(session)

    # Status filter
    statuses = ["All"] + sorted(break_df["status"].unique().tolist())
    selected_status = st.sidebar.selectbox("Status", statuses)

    # Type filter
    types = ["All"] + sorted(break_df["break_type"].unique().tolist())
    selected_type = st.sidebar.selectbox("Break Type", types)

    # Severity filter
    severities = ["All"] + sorted(break_df["severity"].unique().tolist())
    selected_severity = st.sidebar.selectbox("Severity", severities)

    # Counterparty filter
    counterparties = ["All"] + sorted(break_df["counterparty"].unique().tolist())
    selected_cp = st.sidebar.selectbox("Counterparty", counterparties)

    # Apply filters
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
    col1, col2, col3, col4, col5 = st.columns(5)

    total_trades = session.query(Trade).filter(Trade.source_system == "OMS").count()
    total_obs = session.query(Obligation).filter(
        Obligation.obligation_stage == ObligationStage.FINAL
    ).count()
    stp_count = session.query(Obligation).filter(
        Obligation.obligation_stage == ObligationStage.FINAL,
        Obligation.status.in_([ObligationStatus.SETTLED, ObligationStatus.INSTRUCTED]),
    ).count()
    stp_rate = (stp_count / total_obs * 100) if total_obs > 0 else 0

    col1.metric("Total Trades", f"{total_trades:,}")
    col2.metric("Obligations", f"{total_obs:,}")
    col3.metric("STP Rate", f"{stp_rate:.1f}%")
    col4.metric("Total Breaks", len(break_df))
    col5.metric("High Severity", len(break_df[break_df["severity"] == "HIGH"]))

    st.divider()

    # ── Break Queue ─────────────────────────────────────────────────────
    tab1, tab2, tab3, tab4 = st.tabs([
        "Break Queue", "Break Analysis", "Audit Trail", "Reconciliation"
    ])

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

            # Human Approval Action
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

    with tab3:
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

    with tab4:
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
