# Architecture

Deskpilot is an ADK multi-agent system on Gemini, served on Cloud Run, with a
Firestore memory bank. It reasons over a portfolio snapshot produced by the
deterministic `broker-portfolio-sync` pipeline.

## Agent graph + infrastructure

```mermaid
flowchart TB
    subgraph Sync["broker-portfolio-sync (deterministic, no LLM)"]
        BR["Longbridge · Tiger · MooMoo<br/>→ common schema → FIFO P/L → FX → SGD"]
        SNAP["portfolio_snapshot.json"]
        BR --> SNAP
    end

    subgraph CR["Cloud Run (ADK FastAPI app · server.py)"]
        ORCH["Orchestrator: deskpilot<br/>(LlmAgent · Gemini ≥3.5)"]
        RISK["RiskOfficer<br/>(LlmAgent)"]
        MKT["MarketAnalyst<br/>(LlmAgent)"]
        OPT["OptionsStrategist<br/>(LlmAgent)"]
        ORCH -- "AgentTool (call & return)" --> RISK
        ORCH -- "AgentTool (call & return)" --> MKT
        ORCH -- "AgentTool (call & return)" --> OPT
    end

    subgraph Tools["Function tools (read-only)"]
        LP["load_portfolio"]
        GQ["get_quote"]
        EM["get_expected_move"]
        MEM["remember / recall<br/>save_daily_plan / get_last_plan"]
    end

    GEM["Gemini API / Vertex AI<br/>(Gemini ≥ 3.5)"]
    FS["Firestore<br/>(memory bank)"]
    YF["Public market data<br/>(yfinance)"]

    SNAP --> LP
    RISK --> LP
    MKT --> GQ
    OPT --> GQ
    OPT --> EM
    ORCH --> LP
    ORCH --> MEM
    GQ --> YF
    EM --> YF
    MEM <--> FS
    ORCH <--> GEM
    RISK <--> GEM
    MKT <--> GEM
    OPT <--> GEM

    ORCH --> PLAN["Prioritized daily plan"]
    PLAN --> FS
```

## Daily run sequence

```mermaid
sequenceDiagram
    participant U as Trigger (user / Cloud Scheduler)
    participant O as Orchestrator (deskpilot)
    participant M as Firestore memory
    participant R as RiskOfficer
    participant A as MarketAnalyst
    participant S as OptionsStrategist

    U->>O: "Run today's desk plan"
    O->>M: get_last_plan()
    O->>R: review the book
    R->>R: load_portfolio() → expiries, exposure, P/L
    R-->>O: risk summary
    loop names needing a decision
        O->>A: technical read
        A->>A: get_quote()
        O->>S: options-income sizing
        S->>S: get_quote() + get_expected_move()
    end
    O->>M: remember(theses)
    O->>O: synthesize ONE prioritized plan
    O->>M: save_daily_plan(plan)
    O-->>U: daily plan
```

## Design choices

- **Orchestrator–specialist over one mega-prompt.** Each specialist has a narrow
  instruction and only the tools it needs, which keeps tool-selection accurate
  and the reasoning auditable (ADK streams every tool call).
- **AgentTool, not sub-agent transfer.** The Orchestrator calls each specialist
  *as a tool* (ADK `AgentTool`) and keeps control for the whole run, so it can
  gather every specialist's output and finish by persisting one plan. A plain
  `sub_agents` transfer hands control away and never returns — which cannot
  complete a prescribed multi-step routine.
- **Deterministic data, agentic judgment.** Numbers (P/L, FIFO, FX, technicals)
  come from deterministic code; Gemini does prioritization and synthesis, not
  arithmetic. This is the split that makes the output trustworthy.
- **Read-only by construction.** No tool can place or modify an order; the
  guardrail is enforced by the tool surface, not just the prompt.
- **Memory as a first-class tool.** `save_daily_plan` / `get_last_plan` let the
  agent compare intent across days — the core of the "operator" framing.
- **Graceful degradation.** Firestore → local JSON fallback; every market tool
  returns an error dict instead of raising, so a flaky data feed never crashes a
  run.
```
