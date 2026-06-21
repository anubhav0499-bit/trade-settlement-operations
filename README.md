# Trade Settlement Operations Agent

Autonomous post-trade settlement operations system for NSE/BSE equity trades under T+1 (standard) and T+0 (phased rollout for top 500 stocks) settlement cycles.

## Architecture

The system replicates the daily workflow of a settlements operations analyst through a 13-stage deterministic + agentic pipeline:

1. **Trade Capture & Normalization** — Ingests from 3 source formats (OMS, broker confirmation, custodian statement), normalizes into a canonical trade schema
2. **Netting & Obligation Engine** — Multilateral netting at (ISIN, counterparty, settlement_date) grain with VWAP pricing, provisional + final stages
3. **SSI Golden-Copy Validation** — Pre-match validation of standing settlement instructions against a versioned reference table
4. **Matching Engine** — Two-way matching of internal vs counterparty net obligations with configurable price tolerance
5. **Custodian Confirmation** — Tracking confirmation cutoffs per settlement cycle, flagging late confirmations
6. **Settlement Instruction Generation** — SSI-enriched instructions for confirmed obligations
7. **Break Detection & Classification** — Six-type taxonomy with cycle-aware escalation matrices
8. **Auction & Close-Out** — Short delivery resolution per NSE/BSE clearing rules
9. **Agentic Triage Pipeline** — LangGraph dual-path: fail-risk prediction (heuristic) + break triage (RAG-assisted)
10. **Knowledge Base** — FAISS-indexed corpus of 15 break pattern documents for root-cause investigation
11. **Position Reconciliation** — EOD position derivation from settled obligations vs custodian holdings
12. **Reporting** — Multi-tab Excel + narrative DOCX with STP rate, cost-of-exception, counterparty analysis
13. **Dashboard** — Streamlit app with break queue, human approval workflow, audit trail

## Quick Start

```bash
pip install -r requirements.txt

# Generate synthetic data (~1,000 trades, 20 trading days)
python -m generators.synthetic_data

# Run the full pipeline
python main.py

# Launch the dashboard
streamlit run dashboard/app.py

# Run tests
pytest tests/ -v
```

## Project Structure

```
trade_settlement/
├── config/                     # YAML configuration
│   ├── escalation_matrix.yaml  # Severity & aging thresholds
│   ├── matching_tolerances.yaml
│   └── confirmation_cutoffs.yaml
├── data/
│   ├── generated/              # Synthetic CSVs, SQLite DB, reports
│   └── knowledge_base/         # Break pattern corpus + FAISS index
├── src/
│   ├── models/                 # SQLAlchemy schemas + enums
│   ├── ingestion/              # Trade capture & normalization
│   ├── netting/                # Obligation engine (VWAP, prov/final)
│   ├── ssi/                    # SSI golden-copy validation
│   ├── matching/               # Two-way matching engine
│   ├── confirmation/           # Custodian confirmation tracking
│   ├── instruction/            # Settlement instruction generation
│   ├── breaks/                 # Break detection rules engine
│   ├── auction/                # Auction & close-out workflow
│   ├── triage/                 # LangGraph pipeline + FAISS KB
│   ├── reconciliation/         # EOD position reconciliation
│   ├── reporting/              # Excel + DOCX report generation
│   └── utils/                  # Config loader, shared helpers
├── generators/                 # Synthetic data generator
├── tests/                      # pytest unit tests (45 tests)
├── dashboard/                  # Streamlit app
├── main.py                     # Pipeline orchestrator
└── requirements.txt
```

---

## Methodology

### Netting Logic

The netting engine computes multilateral net obligations at the (ISIN, counterparty_id, settlement_date, exchange) grain. For each group, buy quantities are summed and sell quantities subtracted to produce a single net position:

- **Net buyer** (buy - sell > 0): `PAY_OUT` direction — the member receives securities and pays funds
- **Net seller** (buy - sell < 0): `PAY_IN` direction — the member delivers securities and receives funds
- **Zero net**: No obligation generated (positions cancel out)

**VWAP as a modeling simplification**: The net obligation carries a volume-weighted average price (VWAP) computed from its constituent trades. This is used by the matching engine to compare internal vs counterparty obligation prices with a configurable tolerance (default ±0.5%).

NSE Clearing actually computes net fund obligations as the sum of individual trade values (`Σ(price_i × quantity_i)`), not as `VWAP × net_quantity`. The VWAP approach produces a slightly different net fund value due to rounding and the interaction between netting and averaging. We chose VWAP because:
1. It makes the matching engine cleaner — a single price per obligation to compare
2. The difference is negligible for matching purposes (typically < 0.01%)
3. It aligns with how most institutional systems display net positions

**Two-stage obligations**: Obligations are computed twice:
- **Provisional** (end of T day): Based on all trades executed during the session
- **Final** (morning of T+1 for T+1 cycle, same-day for T+0): Incorporates any post-session corrections, cancellations, or corporate action adjustments

The gap between provisional and final is where most SSI-related breaks surface in real-world operations.

### Matching Logic

The matching engine performs a two-way match between internal (OMS-sourced) net obligations and counterparty (broker/custodian-sourced) net obligations. Match keys:

| Key | Comparison |
|-----|-----------|
| ISIN | Exact match |
| Net quantity | Exact match (zero tolerance) |
| VWAP price | Within ±0.5% (configurable) |
| Settlement date | Exact match |
| Counterparty ID | Exact match |
| Net direction | Exact match |

Match results:
- **MATCHED**: All keys match within tolerance → obligation proceeds to confirmation
- **BREAK**: Counterpart found but keys diverge → classified by type (quantity or price mismatch)
- **UNMATCHED**: No counterpart obligation found at all → remains pending

### Custodian Confirmation vs Settlement Instruction

These are distinct pipeline stages, not one step:

1. **Custodian confirmation**: The custodian (clearing member settling on behalf of FPIs, mutual funds, etc.) explicitly confirms it will settle the obligation. Until confirmed, the obligation cannot be instructed. Confirmation cutoffs:
   - T+1: 1:00 PM on settlement day (T+1)
   - T+0: 3:30 PM on trade day (market close)

2. **Settlement instruction**: After confirmation, the system generates an instruction with the counterparty's SSI fields (DP ID, settlement bank, account) for transmission to the depository/clearing bank.

Non-custodial obligations (broker-only) skip confirmation and go directly from MATCHED → CONFIRMED → INSTRUCTED.

### SSI Golden-Copy Approach

SSI mismatches are the #1 cause of settlement fails globally. The system maintains a separate, versioned SSI reference table rather than relying on per-trade SSI data:

- **Effective dating**: Each SSI record has `effective_from` and `effective_to` dates. When a counterparty changes SSI details, the old record is end-dated and a new record created.
- **Pre-match validation**: Every obligation's counterparty SSI is validated against the golden copy *before* the matching engine runs. SSI problems are flagged as `SSI_MISSING_OR_INCORRECT` breaks at the reference-data level, not the trade-data level.
- **Settlement instruction enrichment**: Confirmed obligations pull SSI fields from the golden copy when generating settlement instructions.

### Break Taxonomy

| Type | Trigger | Severity Basis |
|------|---------|---------------|
| `QUANTITY_MISMATCH` | Internal vs counterparty net qty differs | Value at risk |
| `PRICE_MISMATCH` | VWAP diff exceeds tolerance | Value at risk |
| `SSI_MISSING_OR_INCORRECT` | No active SSI or SSI field validation fails | Value at risk |
| `LATE_CONFIRMATION` | Custodian did not confirm by cutoff | Time past cutoff (not VAR) |
| `COUNTERPARTY_FAIL` | Short delivery — sell-side pay-in not delivered | Value at risk |
| `CORPORATE_ACTION_CONFLICT` | Settlement straddles a corporate action ex-date | Value at risk |

**LATE_CONFIRMATION rationale**: This is a timing-based break, not a data mismatch. Since no data is wrong — the confirmation is merely late — severity is based on how far past the cutoff the confirmation is, not on the obligation's monetary value. The thresholds (≤30 min = LOW, ≤2 hours = MEDIUM, >2 hours = HIGH) reflect the escalating fail risk as the settlement deadline approaches.

### Auction & Close-Out Mechanics

When a sell-side obligation is not delivered by settlement day:

1. **Valuation debit**: Closing price on the day preceding pay-in
2. **Buy-in auction**: Conducted on T+1 within a ±20% band around the reference price
3. **Auction settlement**: T+2 (the buyer does not receive shares until T+2 even in the auction scenario)
4. **Close-out**: If the auction sources no shares, cash settlement at the higher of:
   - Highest price from trade date to auction date
   - 20% above the official closing price on auction day

The auction/close-out outcome is persisted as a distinct status (`AUCTION` / `CLOSED_OUT`), separate from a regular break resolution.

### Escalation Matrix

**T+1 cycle (days-based):**

| Age | Min Severity | Escalation Level |
|-----|-------------|-----------------|
| 0-1 days | Per break value | 0 (normal) |
| 2-3 days | MEDIUM | 1 (escalated) |
| 4+ days | HIGH | 2 (critical) |

**T+0 cycle (hours-based):**

| Age | Min Severity | Escalation Level |
|-----|-------------|-----------------|
| 0-4 hours | Per break value | 0 |
| 4-8 hours | MEDIUM | 1 |
| 8+ hours | HIGH | 2 |

### Fail-Risk Prediction

The pre-settlement fail-risk scorer is a weighted heuristic model, not LLM reasoning:

| Factor | Weight | Scoring |
|--------|--------|---------|
| Counterparty historical fail rate | 30% | Normalized against 10% baseline |
| Settlement cycle | 20% | T+0 = 0.7, T+1 = 0.3 |
| Value concentration | 20% | Tiered by obligation value |
| Time pressure | 15% | Days to settlement deadline |
| Obligation status | 15% | PENDING riskier than INSTRUCTED |

The composite score (0-1) is classified into risk tiers: LOW (<0.4), MEDIUM (0.4-0.7), HIGH (≥0.7). High-risk obligations are surfaced in a priority queue for proactive ops intervention.

### Dual-Path Routing Rationale

The triage pipeline has two entry paths because they serve different operational purposes:

- **Path A** (fail-risk scan) runs on obligations that have *not* broken — it is a proactive, pre-settlement intervention tool. The output is a risk queue, not a break resolution.
- **Path B** (break triage) runs on obligations that *have* broken — it is a reactive, post-exception workflow ending in a human approval gate.

Running them as a single sequential pipeline would be incorrect: a PENDING obligation with a high fail-risk score should not go through the break classifier (it hasn't broken), and a BREAK obligation doesn't need fail-risk scoring (it already failed).

### STP Rate Calculation

STP (Straight-Through Processing) rate is defined as:

```
STP Rate = (obligations that reached SETTLED or INSTRUCTED with zero manual touches) / (total final-stage obligations) × 100
```

"Zero manual touches" means: no break detected, no SSI fix required, no late custodian confirmation, no auction. This is the industry-standard definition — it measures the percentage of obligations that flowed through the entire pipeline without human intervention.

### Audit-Logging Rationale

Every agentic decision (fail-risk score, break classification, root-cause investigation, resolution recommendation, escalation assessment) produces a persisted audit log entry containing:
- **Node name**: Which pipeline node made the decision
- **Inputs**: What data the node received
- **Conclusion**: What it decided
- **Rationale**: Why it decided that

This is not a debugging tool — it is a governance artifact. Financial institutions are moving toward requiring explainable reasoning chains for any agentic system that influences operational decisions. The audit log enables a human reviewer to trace any recommendation back to its inputs and understand the agent's reasoning.

### Where LLM Reasoning is Used vs Deliberately Excluded

**LLM/agentic reasoning is used for:**
- Root-cause investigation (RAG retrieval from knowledge base)
- Resolution recommendation drafting
- Natural-language report narrative generation

**LLM reasoning is deliberately excluded from:**
- Trade matching (deterministic, rule-based)
- Netting and obligation computation (arithmetic)
- Break detection and classification (rules engine)
- Severity and escalation assessment (config-driven thresholds)
- Fail-risk scoring (weighted heuristic model)
- Custodian confirmation tracking (deadline comparison)
- Auction/close-out calculations (formula-based)
- Position reconciliation (quantity comparison)

The boundary is intentional: numeric comparison, matching, and financial calculations must be deterministic and auditable. LLM reasoning is reserved for tasks that require natural-language understanding, pattern recognition across unstructured precedents, or human-readable output generation.
