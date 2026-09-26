# SIGNAL-ONLY FOREX SYSTEM: IMPLEMENTATION REPORT
**Timestamp**: 2026-09-26 17:00:00 UTC (22:30:00 IST)  
**System State**: `SIGNAL_ONLY (MANUAL EXECUTION ONLY)`  
**Broker Trading**: Strictly `DISABLED`  
**Target Universe**: 7 Pairs (`EUR/USD`, `GBP/USD`, `USD/JPY`, `USD/CHF`, `AUD/USD`, `USD/CAD`, `NZD/USD`)  

---

## 1. OBJECTIVE & ARCHITECTURAL SEPARATION

The Spot Forex Signal-Only System provides institutional-grade, real-time trade signals for **manual execution by human traders**.

### Strict Operational Constraints:
- **Zero Automated Trading**: The codebase contains zero order placement functions, zero buy/sell execution logic, zero broker order endpoints, and zero trade automation.
- **Zero Broker Accounts / Keys**: No broker API credentials, private keys, or passwords are stored, requested, or utilized.
- **Strictly Read-Only Market Ingestion**: Public real-time data feeds (e.g. Yahoo Finance / ECN data) are used exclusively for read-only market price observation.
- **Render Live Trading Disabled**: The Render deployment configuration (`Procfile`) hosts only the signal research web panel (`app.py`), which has zero execution routes.

### Architectural Decoupling:
```
+-----------------------------------------------------------------------------------+
|                            SIGNAL-ONLY ARCHITECTURE                               |
+-----------------------------------------------------------------------------------+
|  1. Public Market Ingestion (Yahoo Finance / ECN Feeds)                           |
|       ↓ (Read-Only 1M/5M/15M/1H/4H Data)                                          |
|  2. Multi-Timeframe Candle Aggregator (Confirmed Closed Candles Only)             |
|       ↓                                                                           |
|  3. Causal Technical Indicators (EMA, RSI, MACD, ADX, ATR, Bollinger)             |
|       ↓                                                                           |
|  4. Frozen Phase 7 Mod C Signal Engine (short_tf_engine.py)                        |
|       - 1H SIDEWAYS Exclusion                                                     |
|       - 4H TRENDING_UP Overextension Gate (<= 0.80 ATR)                           |
|       - Spread Gate (Spread Cost <= 0.25 * ATR)                                   |
|       ↓                                                                           |
|  5. Candidate C Exit Architecture (exit_engine.py Reference)                      |
|       - Structural SL, TP1 (+1.0R), TP2 (+2.0R), 1R Risk Metric                  |
|       ↓                                                                           |
|  6. Signal Formatter & Notification Layer (forex_signal_engine.py)                |
|       - UTC Calculation -> IST Display Conversion (UTC + 05:30)                   |
|       - Duplicate Alert Prevention (Unique Signal ID, State Persistence)          |
|       - Health & Safety Monitoring (SAFE / UNSAFE Status)                         |
|       ↓                                                                           |
|  7. User Display & Delivery (CLI Console, CSV Log, Flask /api/forex/signals)       |
|       ↓                                                                           |
|  8. 🧑 MANUAL TRADER (Places trade manually on their own broker terminal)        |
+-----------------------------------------------------------------------------------+
```

---

## 2. VALIDATED STRATEGY SPECIFICATION (PHASE 7 MOD C)

The signal engine strictly adheres to the frozen Phase 7 Mod C specification with zero parameter tuning:

### 2.1 Rule 1: 1H SIDEWAYS Exclusion
- If the 1H context is classified as `SIDEWAYS` (flat moving averages, compressed ATR ratio):
  $$\text{Signal} = \text{HOLD / NO SIGNAL}, \quad \text{Rule Set} = \text{"1H\_SIDEWAYS\_EXCLUSION\_HOLD"}$$
- Entries are completely suppressed during horizontal chop.

### 2.2 Rule 2: 4H TRENDING_UP Overextension Filter
- During 4H `TRENDING_UP` macro regimes:
  $$\text{Distance from 5M EMA50} \le 0.80\times\text{ATR}$$
  If distance exceeds $0.80\text{ ATR}$, the setup is rejected to avoid late-stage exhaustion traps.
- In all other regimes (e.g. `RANGING`, `TRENDING_DOWN`), the existing validated threshold of $\le 1.40\times\text{ATR}$ is preserved.

### 2.3 Rule 3: Candidate C Exit Engine (Risk & Target Bounds)
- Derived from `exit_engine.py` (`CandidateC_Partial_TP_Trailing`):
  - **Initial Stop Loss (SL)**: Structural swing extreme ($[0.8\text{ ATR}, 2.5\text{ ATR}]$).
  - **Take Profit 1 (TP1)**: Calculated at $+1.0\text{R}$ (50% partial TP recommendation).
  - **Breakeven Stop**: At $+1.0\text{R}$, remainder stop moves to Entry Price $\pm$ Spread Buffer.
  - **Take Profit 2 (TP2)**: Calculated at $+2.0\text{R}$ (final runner target).
  - **1R Risk Value**: Explicitly presented in pips and price distance.

### 2.4 Rule 4: Spread Gate Protection
- Spread cost must not exceed 25% of the 5M ATR:
  $$\text{Spread Cost} \le 0.25 \times \text{ATR}_{14}$$
  If spread exceeds this threshold, the setup is rejected (`SPREAD_EXCESSIVE_HOLD`).

---

## 3. TIME HANDLING: UTC BACKEND $\rightarrow$ IST DISPLAY

### 3.1 Internal Calculation Standard
- **100% UTC**: All internal timestamps, candle indices, session classifications, indicator windows, and state files are computed and stored strictly in **UTC**.
- UTC and IST are never mixed in internal logic.

### 3.2 User-Facing Display Standard
- **IST (Indian Standard Time)**: Converted via $\text{IST} = \text{UTC} + 05:30$.
- Every displayed signal explicitly shows the label **`IST`**.
- Example: `2026-09-26 12:00:00 UTC` $\longrightarrow$ `2026-09-26 17:30:00 IST`.

---

## 4. SIGNAL OUTPUT & FORMATTED DISPLAY CARD

When a setup satisfies 100% of gating rules, the engine renders an intuitive, clutter-free signal card designed for instant execution by a manual trader:

### Example BUY Signal Card:
```text
━━━━━━━━━━━━━━━━━━
🟢 BUY SIGNAL
EUR/USD
━━━━━━━━━━━━━━━━━━

Time: 2026-09-26 17:30:00 IST (IST)
Entry: 1.10500
SL:    1.10300
TP1:   1.10700 (+1R)
TP2:   1.10900 (+2R)
Risk:  20.0 pips (0.00200)

4H Regime: RANGING
1H Trend:  UP
5M Setup:  VALID
Spread:    1.2 pips
Status:    VALID

Signal ID: EURUSD-20260926-1730
━━━━━━━━━━━━━━━━━━
```

### Example NO SIGNAL Display:
```text
━━━━━━━━━━━━━━━━━━
⚪ NO SIGNAL
EUR/USD
━━━━━━━━━━━━━━━━━━

Time: 2026-09-26 17:30:00 IST (IST)
Status:    NO SIGNAL
Reason:    1H_SIDEWAYS_EXCLUSION_HOLD

4H Regime: TRENDING_UP
1H Trend:  SIDEWAYS
Spread:    1.1 pips
Gate:      PASS
━━━━━━━━━━━━━━━━━━
```

---

## 5. CAUSALITY, NO-LOOKAHEAD & DUPLICATE PREVENTION

1. **Closed Candles Only**: Signal evaluation requires completed, finalized candles. Intrabar ticks never generate signals.
2. **Temporal Independence**: Deleting or appending future bars does not alter prior signal generation.
3. **Deterministic Signal ID**: Each signal is tagged with `{PAIR}-{YYYYMMDD}-{HHMM}` based on the closing timestamp of the triggering 5M bar.
4. **Duplicate Suppression**: Once a Signal ID is emitted, it is added to `seen_signal_ids` and persisted to `data/signals_live_state.json`. Any subsequent evaluations on the same candle are suppressed as `DUPLICATE_SIGNAL_ALREADY_ISSUED`.
5. **Restart Resilience**: If the service restarts, the state file is reloaded immediately, preventing duplicate alerts from being re-sent.

---

## 6. HEALTH & SAFETY MONITORING

The engine continuously computes its operational health state:
- **`data_connection_status`**: `CONNECTED`, `DISCONNECTED_OR_STALE`, or `NO_DATA`.
- **`system_status`**:
  - **`SAFE`**: Feed active, data age $\le 15\text{ minutes}$, normal spreads.
  - **`UNSAFE`**: Feed stale ($> 15\text{ minutes}$ old), market closed, or feeds disconnected.
- When `system_status == UNSAFE`, signal generation is automatically blocked, and signals revert to `NO SIGNAL (STALE_MARKET_DATA)`.

---

## 7. PRODUCTION FILE GOVERNANCE AUDIT

| File | Status | Modifications |
| :--- | :--- | :--- |
| `short_tf_engine.py` | **Preserved** | Validated Mod C logic active |
| `exit_engine.py` | **Preserved (100% Frozen)** | 0 lines modified |
| `regime_detector.py` | **Preserved (100% Frozen)** | 0 lines modified |
| `forex_data_feed.py` | **Preserved** | 0 lines modified |
| `historical_data_feed.py` | **Preserved** | 0 lines modified |
| `forex_signal_engine.py` | **Created** | Modular signal-only orchestrator |
| `run_signal_service.py` | **Created** | CLI monitor and shadow runner |
| `app.py` | **Updated** | Added `/api/forex/signals` and `/api/forex/health` endpoints |
| `tests/test_signal_only_system.py` | **Created** | 15-point automated verification suite |
