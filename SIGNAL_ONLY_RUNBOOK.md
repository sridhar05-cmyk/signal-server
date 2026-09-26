# SIGNAL-ONLY FOREX SYSTEM: OPERATIONAL RUNBOOK
**System State**: `SIGNAL_ONLY (MANUAL TRADING ONLY)`  
**Broker Order Placement**: Strictly `DISABLED`  
**Target Universe**: 7 Pairs (`EUR/USD`, `GBP/USD`, `USD/JPY`, `USD/CHF`, `AUD/USD`, `USD/CAD`, `NZD/USD`)  

---

## 1. SYSTEM OVERVIEW

This system is an institutional **Signal-Only** market observer. It processes live multi-timeframe market data, evaluates the frozen Phase 7 Mod C strategy, and outputs clean signal cards for **manual trade execution by human traders**.

> [!WARNING]
> **NO AUTO-TRADING**: This software CANNOT place trades on your broker. It does not contain any broker login or trading API integration. You must execute all recommended trades manually on your preferred broker or trading platform.

---

## 2. HOW TO START THE SERVICE

The system can be operated in multiple modes depending on your operating requirements:

### Mode A: Real-Time Live Monitor (Continuous Daemon)
Scans all 7 pairs every 60 seconds against real live market feeds and prints signals to the console:
```powershell
python run_signal_service.py --interval 60
```

### Mode B: Single-Scan Snapshot (One-Shot)
Performs a single evaluation pass across all 7 pairs, prints the status/cards, and exits immediately:
```powershell
python run_signal_service.py --once
```

### Mode C: Historical / Shadow Replay Mode
Replays the most recent $N$ confirmed 5-minute candles from local verified historical datasets to inspect signal output during weekend market closures:
```powershell
python run_signal_service.py --replay --bars 60
```

### Mode D: Web Dashboard & REST API
Starts the local Flask web panel and REST API server:
```powershell
python app.py
```
- Web Dashboard: `http://localhost:5000/`
- Live Signals API: `http://localhost:5000/api/forex/signals`
- System Health API: `http://localhost:5000/api/forex/health`

---

## 3. HOW TO STOP THE SERVICE

- **CLI Monitor**: Press `Ctrl + C` in the terminal window. The service will gracefully save state and terminate.
- **Background / Daemon Process**: If running as a background service or task, terminate via Task Manager or shell command:
  ```powershell
  # Windows PowerShell
  Stop-Process -Name "python" -Force
  ```

---

## 4. HOW TO ENABLE / DISABLE SIGNAL MODE

- **Always Active**: Signal mode is hardcoded as the only operational mode of the system.
- **Broker Execution is Impossible**: There is no flag, argument, or configuration setting that can enable automated broker trading.
- **Pausing Alerts**: To pause signal generation, simply stop the process or close the terminal window.

---

## 5. WHERE SIGNALS APPEAR

Signals are delivered simultaneously through four distinct channels:

### 1. Console Standard Output (Stdout)
Whenever a valid BUY or SELL setup triggers, a formatted signal card is printed directly to the console:
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

### 2. Live CSV Log
Every valid signal is appended to:
[`spot_bot/data/signals_live_log.csv`](file:///i:/quotex%20ai/quotex_bot/spot_bot/data/signals_live_log.csv)  
Fields: `signal_id`, `timestamp_utc`, `timestamp_ist`, `pair`, `direction`, `entry_price`, `stop_loss`, `tp1`, `tp2`, `risk_1r_pips`, `regime_4h`, `trend_1h`, `spread_pips`, `rule_set`.

### 3. Persistent State File
Current active signals and duplicate tracking IDs are stored in:
[`spot_bot/data/signals_live_state.json`](file:///i:/quotex%20ai/quotex_bot/spot_bot/data/signals_live_state.json)

### 4. REST API Endpoint
Query `GET /api/forex/signals` to receive a JSON object containing current signal cards and metadata for external webhook or GUI consumption.

---

## 6. HOW TIMESTAMPS ARE HANDLED

- **Internal Calculations (Backend)**: Strictly **UTC**. All moving averages, candle aggregation boundaries, and timestamps are indexed in UTC.
- **User-Facing Display**: Converted to **IST (Indian Standard Time)** via $\text{IST} = \text{UTC} + 05:30$.
- **Timezone Clarification**: Every signal card and CSV row displays the explicit label `IST` to prevent any confusion.

---

## 7. HOW TO INTERPRET SIGNALS FOR MANUAL EXECUTION

When you see a signal card:

1. **🟢 BUY SIGNAL**:
   - Open your broker terminal.
   - Select the specified currency pair.
   - Check current broker price against the **Entry Price**. (Execute if current price is within 1–2 pips of Entry).
   - Set your **Stop Loss (SL)** at the indicated SL price.
   - Set your **Take Profit 1 (TP1)** at the $+1.0\text{R}$ price.
   - *Candidate C Management*: When price hits TP1, manually close **50% of your position**, and immediately move the Stop Loss on the remaining 50% to your **Entry Price (Breakeven)**. Allow the remainder to trail toward **TP2 (+2.0R)**.

2. **🔴 SELL SIGNAL**:
   - Open your broker terminal.
   - Check current broker price against the **Entry Price**.
   - Set your **Stop Loss (SL)** at the indicated SL price above market.
   - Set your **Take Profit 1 (TP1)** at the $+1.0\text{R}$ price below market.
   - When price reaches TP1, close 50% and move the remainder stop to Breakeven.

3. **⚪ NO SIGNAL**:
   - Do not trade. Market conditions are either in horizontal chop (`1H_SIDEWAYS_EXCLUSION_HOLD`), overextended, or experiencing excessive spreads.

---

## 8. TROUBLESHOOTING & HEALTH MONITORING

### Problem: System Displays `SYSTEM STATUS: UNSAFE | Feed: DISCONNECTED_OR_STALE`
- **Cause 1 (Weekend Market Closure)**: FX markets close on Friday at 21:00 UTC and reopen on Sunday at 21:00 UTC. During weekends, quotes are stale and the engine correctly protects you by refusing to trade.
  - *Action*: Normal behavior. The system will automatically return to `SAFE` when markets reopen on Sunday evening.
- **Cause 2 (Internet Disconnection)**: Your network cannot reach Yahoo Finance.
  - *Action*: Verify your internet connection. Run `python run_signal_service.py --once` to test.

### Problem: Signal Disappeared or Shows `DUPLICATE_SIGNAL_ALREADY_ISSUED`
- **Explanation**: The engine debounces signals so that the same 5-minute candle does not alert you multiple times. Once a signal is printed, subsequent scans in the same candle window will suppress duplicates.

---

## 9. HOW TO VERIFY BROKER EXECUTION IS IMPOSSIBLE

You can independently audit the codebase to confirm that trade execution is physically impossible:

1. **Check Dependencies**:
   Open [`requirements.txt`](file:///i:/quotex%20ai/quotex_bot/spot_bot/requirements.txt). Notice that MetaTrader (MetaTrader5), ccxt, binance-connector, or broker SDKs are completely absent.
2. **Search for Execution Commands**:
   Run a search across `spot_bot/` for `order_send`, `place_order`, `create_order`, `buy()`, `sell()`. There are zero broker API calls.
3. **Environment Audit**:
   Open `.env` (or your environment variables). There are zero broker account logins, API keys, or secret keys configured.
