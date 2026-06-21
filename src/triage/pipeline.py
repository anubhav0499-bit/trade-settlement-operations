"""
Agentic Triage Pipeline (§9) — LangGraph state graph.

Two distinct entry paths:
  Path A — Pre-settlement fail-risk scan (heuristic, not LLM)
  Path B — Post-break triage (classifier → root-cause → recommender → escalation → human gate)

Every node writes a reasoning chain to the agentic audit log.
"""

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, TypedDict

from langgraph.graph import END, StateGraph

from src.models.database import AgenticAuditLog, BreakRecord, Obligation
from src.models.enums import (
    BreakStatus,
    BreakType,
    MatchStatus,
    ObligationStatus,
    Severity,
)
from src.triage.fail_risk import compute_fail_risk_batch, get_high_risk_queue, FailRiskScore
from src.triage.knowledge_base import query_knowledge_base
from src.utils.config_loader import get_escalation_config


class TriageState(TypedDict):
    """State flowing through the triage pipeline."""
    # Input
    obligations: list[dict]
    breaks: list[dict]
    # Path A output
    fail_risk_scores: list[dict]
    high_risk_queue: list[dict]
    # Path B output
    triage_results: list[dict]
    # Audit
    audit_logs: list[dict]
    # Routing
    path: str  # "A", "B", or "BOTH"


def _log_entry(
    node_name: str,
    obligation_id: str | None,
    break_id: str | None,
    inputs: dict,
    conclusion: str,
    rationale: str,
) -> dict:
    return {
        "log_id": str(uuid.uuid4()),
        "obligation_id": obligation_id,
        "break_id": break_id,
        "node_name": node_name,
        "inputs": json.dumps(inputs),
        "conclusion": conclusion,
        "rationale": rationale,
        "timestamp": datetime.utcnow().isoformat(),
    }


# ── Router ──────────────────────────────────────────────────────────────────

def route_entry(state: TriageState) -> TriageState:
    """Determine which path(s) to take based on input."""
    has_obligations = bool(state.get("obligations"))
    has_breaks = bool(state.get("breaks"))

    if has_obligations and has_breaks:
        state["path"] = "BOTH"
    elif has_obligations:
        state["path"] = "A"
    elif has_breaks:
        state["path"] = "B"
    else:
        state["path"] = "NONE"

    state.setdefault("audit_logs", [])
    state.setdefault("fail_risk_scores", [])
    state.setdefault("high_risk_queue", [])
    state.setdefault("triage_results", [])
    return state


# ── Path A: Fail-Risk Prediction ────────────────────────────────────────────

def fail_risk_node(state: TriageState) -> TriageState:
    """Score fail risk on pending obligations (heuristic, not LLM)."""
    obligations_data = state.get("obligations", [])
    if not obligations_data:
        return state

    # Reconstruct lightweight obligation objects for scoring
    scores = []
    for ob_dict in obligations_data:
        score = _score_obligation_from_dict(ob_dict)
        scores.append(score)

        state["audit_logs"].append(_log_entry(
            node_name="fail_risk_scorer",
            obligation_id=ob_dict.get("obligation_id"),
            break_id=None,
            inputs={
                "counterparty_id": ob_dict.get("counterparty_id"),
                "settlement_cycle": ob_dict.get("settlement_cycle"),
                "net_value": str(ob_dict.get("net_value")),
                "status": ob_dict.get("status"),
            },
            conclusion=f"Risk score: {score['risk_score']}, Tier: {score['risk_tier']}",
            rationale=f"Weighted composite of counterparty fail rate, cycle risk, value concentration, time pressure, and status. Factors: {json.dumps(score['factors'])}",
        ))

    state["fail_risk_scores"] = scores

    high_risk = [s for s in scores if s["risk_score"] >= 0.5]
    high_risk.sort(key=lambda s: s["risk_score"], reverse=True)
    state["high_risk_queue"] = high_risk

    return state


def _score_obligation_from_dict(ob_dict: dict) -> dict:
    """Compute a fail-risk score from an obligation dict (no ORM dependency)."""
    from src.triage.fail_risk import COUNTERPARTY_FAIL_RATES

    cp_id = ob_dict.get("counterparty_id", "")
    cp_fail_rate = COUNTERPARTY_FAIL_RATES.get(cp_id, 0.05)
    cp_score = min(cp_fail_rate / 0.10, 1.0)

    cycle = ob_dict.get("settlement_cycle", "T1")
    cycle_score = 0.7 if cycle == "T0" else 0.3

    val = float(ob_dict.get("net_value", 0))
    if val > 5_000_000:
        val_score = 0.9
    elif val > 1_000_000:
        val_score = 0.6
    elif val > 500_000:
        val_score = 0.4
    else:
        val_score = 0.2

    status = ob_dict.get("status", "PENDING")
    status_scores = {
        "PENDING": 0.8, "SSI_VALIDATED": 0.6, "MATCHED": 0.5,
        "PENDING_CONFIRMATION": 0.7, "CONFIRMED": 0.3, "INSTRUCTED": 0.2,
    }
    status_score = status_scores.get(status, 0.5)

    total = (
        cp_score * 0.30
        + cycle_score * 0.20
        + val_score * 0.20
        + 0.5 * 0.15  # default time pressure
        + status_score * 0.15
    )
    total = round(min(total, 1.0), 3)

    tier = "HIGH" if total >= 0.7 else ("MEDIUM" if total >= 0.4 else "LOW")

    return {
        "obligation_id": ob_dict.get("obligation_id"),
        "risk_score": total,
        "risk_tier": tier,
        "factors": {
            "counterparty_fail_rate": round(cp_score, 3),
            "settlement_cycle": cycle_score,
            "value_concentration": val_score,
            "status": status_score,
        },
    }


# ── Path B: Break Triage ────────────────────────────────────────────────────

def classifier_node(state: TriageState) -> TriageState:
    """Confirm/refine break type and severity from rules engine output."""
    breaks = state.get("breaks", [])
    config = get_escalation_config()

    for brk in breaks:
        original_type = brk.get("break_type")
        original_severity = brk.get("severity")

        # Refine severity based on value_at_risk and aging
        var = float(brk.get("value_at_risk", 0))
        if original_type == "LATE_CONFIRMATION":
            refined_severity = original_severity  # uses time-based, not VAR
        else:
            var_config = config["value_at_risk_severity"]
            if var < var_config["low_max"]:
                refined_severity = "LOW"
            elif var < var_config["medium_max"]:
                refined_severity = "MEDIUM"
            else:
                refined_severity = "HIGH"

        brk["refined_severity"] = refined_severity
        brk["classification_confirmed"] = True

        state["audit_logs"].append(_log_entry(
            node_name="classifier",
            obligation_id=brk.get("obligation_id"),
            break_id=brk.get("break_id"),
            inputs={
                "break_type": original_type,
                "original_severity": original_severity,
                "value_at_risk": str(var),
            },
            conclusion=f"Confirmed type={original_type}, refined severity={refined_severity}",
            rationale=f"Severity assessed using value-at-risk thresholds: LOW < {config['value_at_risk_severity']['low_max']}, MEDIUM < {config['value_at_risk_severity']['medium_max']}, else HIGH. LATE_CONFIRMATION uses time-based severity instead.",
        ))

    return state


def root_cause_node(state: TriageState) -> TriageState:
    """Use RAG knowledge base to suggest likely root cause."""
    breaks = state.get("breaks", [])

    for brk in breaks:
        query = f"{brk.get('break_type', '')} break for ISIN {brk.get('isin', 'unknown')} with counterparty {brk.get('counterparty_id', 'unknown')}"

        try:
            results = query_knowledge_base(query, top_k=2)
        except Exception:
            results = []

        if results:
            top_match = results[0]
            brk["likely_root_cause"] = top_match.get("root_cause", "Unknown")
            brk["kb_reference"] = top_match.get("id", "")
            brk["kb_title"] = top_match.get("title", "")
            brk["similarity_score"] = top_match.get("similarity_score", 0)
        else:
            brk["likely_root_cause"] = "No matching pattern found in knowledge base"
            brk["kb_reference"] = ""
            brk["similarity_score"] = 0

        state["audit_logs"].append(_log_entry(
            node_name="root_cause_investigator",
            obligation_id=brk.get("obligation_id"),
            break_id=brk.get("break_id"),
            inputs={"query": query, "top_k": 2},
            conclusion=f"Root cause: {brk.get('likely_root_cause', '')[:200]}",
            rationale=f"Retrieved from KB document '{brk.get('kb_reference', '')}' with similarity {brk.get('similarity_score', 0):.3f}. Query matched against {len(results)} documents.",
        ))

    return state


# Resolution templates by break type
RESOLUTION_TEMPLATES = {
    "QUANTITY_MISMATCH": "Request counterparty to reissue confirmation with correct quantity. Cross-check against exchange trade file (NSCCL/ICCL member trade report). If internal quantity is incorrect, amend OMS entries.",
    "PRICE_MISMATCH": "Compare VWAP against exchange official VWAP for the session. Request counterparty to re-confirm using agreed price methodology. If methodologies differ, escalate to settlements manager.",
    "SSI_MISSING_OR_INCORRECT": "Contact counterparty operations to obtain/verify SSI details (DP ID, settlement bank, account). Update SSI golden copy. Re-validate and re-generate settlement instruction.",
    "LATE_CONFIRMATION": "Contact custodian operations desk for immediate confirmation. If cutoff has passed, prepare for obligation reversion to trading member. Coordinate with trading desk for re-instruction.",
    "COUNTERPARTY_FAIL": "Initiate auction/close-out workflow per NSE Clearing rules. Compute valuation debit. Notify buyer of expected delivery delay. Monitor auction outcome on T+2.",
    "CORPORATE_ACTION_CONFLICT": "Check corporate actions calendar for the ISIN. Verify entitlement based on ex-date vs trade/settlement dates. Generate market claim if applicable. Coordinate with counterparty on adjusted quantity/price.",
}


def resolution_recommender_node(state: TriageState) -> TriageState:
    """Draft recommended resolution action for each break."""
    breaks = state.get("breaks", [])

    for brk in breaks:
        break_type = brk.get("break_type", "")
        template = RESOLUTION_TEMPLATES.get(break_type, "Escalate to settlements operations manager for manual review.")

        # Customize recommendation with specific details
        recommendation = template
        if brk.get("likely_root_cause"):
            recommendation += f"\n\nLikely root cause: {brk['likely_root_cause'][:300]}"

        brk["recommended_action"] = recommendation
        brk["status"] = "PENDING_APPROVAL"

        state["audit_logs"].append(_log_entry(
            node_name="resolution_recommender",
            obligation_id=brk.get("obligation_id"),
            break_id=brk.get("break_id"),
            inputs={
                "break_type": break_type,
                "root_cause": brk.get("likely_root_cause", "")[:200],
            },
            conclusion=f"Recommended: {recommendation[:200]}",
            rationale=f"Applied standard resolution template for {break_type}. Enriched with root-cause analysis from KB document {brk.get('kb_reference', '')}.",
        ))

    return state


def escalation_node(state: TriageState) -> TriageState:
    """Check break age against escalation matrix and flag for review."""
    breaks = state.get("breaks", [])
    config = get_escalation_config()

    for brk in breaks:
        age_days = brk.get("age_days", 0)
        age_hours = brk.get("age_hours", 0)
        severity = brk.get("refined_severity", brk.get("severity", "LOW"))
        cycle = brk.get("settlement_cycle", "T1")

        if cycle == "T0":
            thresholds = config["t0_cycle"]["aging_thresholds"]
            for threshold in thresholds:
                max_hours = threshold.get("max_age_hours")
                if max_hours is not None and age_hours <= max_hours:
                    brk["escalation_level"] = threshold["escalation_level"]
                    break
            else:
                brk["escalation_level"] = thresholds[-1]["escalation_level"]
        else:
            thresholds = config["t1_cycle"]["aging_thresholds"]
            for threshold in thresholds:
                max_days = threshold.get("max_age_days")
                if max_days is not None and age_days <= max_days:
                    brk["escalation_level"] = threshold["escalation_level"]
                    break
            else:
                brk["escalation_level"] = thresholds[-1]["escalation_level"]

        brk["needs_human_review"] = True

        state["audit_logs"].append(_log_entry(
            node_name="escalation_checker",
            obligation_id=brk.get("obligation_id"),
            break_id=brk.get("break_id"),
            inputs={
                "age_days": age_days,
                "age_hours": age_hours,
                "severity": severity,
                "cycle": cycle,
            },
            conclusion=f"Escalation level: {brk.get('escalation_level', 0)}, requires human review",
            rationale=f"Applied {cycle} escalation matrix. Age: {age_days}d/{age_hours:.1f}h. Severity: {severity}.",
        ))

    state["triage_results"] = breaks
    return state


def human_approval_gate(state: TriageState) -> TriageState:
    """Terminal node — nothing resolves without human approval."""
    for brk in state.get("triage_results", []):
        brk["awaiting_human_approval"] = True
        brk["auto_resolved"] = False

        state["audit_logs"].append(_log_entry(
            node_name="human_approval_gate",
            obligation_id=brk.get("obligation_id"),
            break_id=brk.get("break_id"),
            inputs={"recommended_action": brk.get("recommended_action", "")[:200]},
            conclusion="Queued for human approval — no auto-resolution permitted",
            rationale="Per system policy, all break resolutions require explicit human approval before status changes to RESOLVED.",
        ))

    return state


# ── Graph routing ───────────────────────────────────────────────────────────

def _should_run_path_a(state: TriageState) -> str:
    path = state.get("path", "NONE")
    if path in ("A", "BOTH"):
        return "fail_risk"
    return "end"


def _should_run_path_b(state: TriageState) -> str:
    path = state.get("path", "NONE")
    if path in ("B", "BOTH"):
        return "classifier"
    return "end"


def _after_fail_risk(state: TriageState) -> str:
    if state.get("path") == "BOTH":
        return "classifier"
    return "end"


def build_triage_graph() -> StateGraph:
    """Build the LangGraph state graph for the triage pipeline."""
    graph = StateGraph(TriageState)

    # Add nodes
    graph.add_node("router", route_entry)
    graph.add_node("fail_risk", fail_risk_node)
    graph.add_node("classifier", classifier_node)
    graph.add_node("root_cause", root_cause_node)
    graph.add_node("recommender", resolution_recommender_node)
    graph.add_node("escalation", escalation_node)
    graph.add_node("human_gate", human_approval_gate)

    # Set entry point
    graph.set_entry_point("router")

    # Router → Path A or Path B (or both)
    graph.add_conditional_edges(
        "router",
        _should_run_path_a,
        {"fail_risk": "fail_risk", "end": END},
    )

    # Path A → Path B (if BOTH) or END
    graph.add_conditional_edges(
        "fail_risk",
        _after_fail_risk,
        {"classifier": "classifier", "end": END},
    )

    # Path B: linear chain
    graph.add_edge("classifier", "root_cause")
    graph.add_edge("root_cause", "recommender")
    graph.add_edge("recommender", "escalation")
    graph.add_edge("escalation", "human_gate")
    graph.add_edge("human_gate", END)

    return graph.compile()


def run_triage(
    obligations: list[dict] | None = None,
    breaks: list[dict] | None = None,
) -> TriageState:
    """Run the triage pipeline.

    Args:
        obligations: Dicts of pending/confirmed/instructed obligations (Path A)
        breaks: Dicts of break records (Path B)

    Returns:
        Final triage state with risk scores, recommendations, and audit logs
    """
    graph = build_triage_graph()

    initial_state: TriageState = {
        "obligations": obligations or [],
        "breaks": breaks or [],
        "fail_risk_scores": [],
        "high_risk_queue": [],
        "triage_results": [],
        "audit_logs": [],
        "path": "",
    }

    result = graph.invoke(initial_state)
    return result
