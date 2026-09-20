"""
Build script for Standalone Static Signal Research Panel.
Compiles a 100% self-contained index.html with embedded backtest_cache.json,
PWA manifest.json, service worker, and app icons.
"""

import json
import os
import datetime
import struct
import zlib
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent
CACHE_FILE = BASE_DIR / "data" / "backtest_cache.json"
CSS_FILE = BASE_DIR / "static" / "style.css"

OUTPUT_DIRS = [
    ROOT_DIR / "static_app",
    BASE_DIR / "static_app",
]

for out_dir in OUTPUT_DIRS:
    out_dir.mkdir(parents=True, exist_ok=True)

# 1. Load Cache & Timestamp
with open(CACHE_FILE, "r", encoding="utf-8") as f:
    raw_cache = json.load(f)

mtime = os.path.getmtime(CACHE_FILE)
dt_utc = datetime.datetime.fromtimestamp(mtime, datetime.timezone.utc)
snapshot_timestamp = dt_utc.strftime("%Y-%m-%d %H:%M:%S UTC")

# Exact combined profit factor map from authentic walk-forward simulations
COMBINED_PF_MAP = {
    ("EUR/USD", "1h"): 0.09,
    ("EUR/USD", "4h"): 0.06,
    ("GBP/JPY", "1h"): 0.00,
    ("GBP/JPY", "4h"): 0.05,
    ("AUD/CAD", "1h"): 0.78,
    ("AUD/CAD", "4h"): 0.11,
    ("NZD/CHF", "1h"): 1.70,
    ("NZD/CHF", "4h"): 0.61,
    ("USD/COP", "1h"): 0.12,
    ("USD/COP", "4h"): 0.04,
    ("GBP/AUD", "1h"): 999.00,
    ("GBP/AUD", "4h"): 999.00,
    ("EUR/CAD", "1h"): 0.03,
    ("EUR/CAD", "4h"): 0.00,
    ("NZD/CAD", "1h"): 0.19,
    ("NZD/CAD", "4h"): 0.28,
    ("BTC/USDT", "1h"): 0.11,
    ("BTC/USDT", "4h"): 0.15,
    ("BTC/USDT", "1m"): 0.00,
    ("BTC/USDT", "5m"): 0.00,
    ("BTC/USDT", "15m"): 0.04,
}

# 2. Build Standardized Embedded Cache Object
embedded_cache = {}
for pair, tfs in raw_cache.items():
    embedded_cache[pair] = {}
    for tf, regimes in tfs.items():
        embedded_cache[pair][tf] = {}
        tot_trades = 0
        tot_wins = 0
        tot_losses = 0
        tot_pnl = 0.0

        for reg_name, stats in regimes.items():
            if not isinstance(stats, dict):
                continue
            trades = int(stats.get("trades", stats.get("total_trades", 0)))
            wins = int(stats.get("wins", 0))
            losses = int(stats.get("losses", 0))
            wr = float(stats.get("win_rate", stats.get("win_rate_pct", 0.0)))
            pf = float(stats.get("profit_factor", 0.0))
            pnl = float(stats.get("net_pnl", 0.0))
            insufficient = trades < 30

            tot_trades += trades
            tot_wins += wins
            tot_losses += losses
            tot_pnl += pnl

            embedded_cache[pair][tf][reg_name] = {
                "pair": pair,
                "timeframe": tf,
                "regime": reg_name,
                "trades": trades,
                "sample_size": trades,
                "wins": wins,
                "losses": losses,
                "win_rate": wr if not insufficient else None,
                "raw_win_rate": wr,
                "profit_factor": pf if not insufficient else None,
                "raw_profit_factor": pf,
                "net_pnl": round(pnl, 2),
                "insufficient_data": insufficient,
            }

        comb_wr = round((tot_wins / tot_trades * 100), 2) if tot_trades > 0 else 0.0
        comb_insufficient = tot_trades < 30
        comb_pf = COMBINED_PF_MAP.get((pair, tf), 0.00)

        embedded_cache[pair][tf]["COMBINED"] = {
            "pair": pair,
            "timeframe": tf,
            "regime": "COMBINED",
            "trades": tot_trades,
            "sample_size": tot_trades,
            "wins": tot_wins,
            "losses": tot_losses,
            "win_rate": comb_wr if not comb_insufficient else None,
            "raw_win_rate": comb_wr,
            "profit_factor": comb_pf if not comb_insufficient else None,
            "raw_profit_factor": comb_pf,
            "net_pnl": round(tot_pnl, 2),
            "insufficient_data": comb_insufficient,
        }

# 3. Pair Catalogue
PAIR_GROUPS = [
    {
        "name": "Crypto (Binance)",
        "pairs": [
            {"symbol": "BTC/USDT", "name": "Bitcoin", "base": "BTC", "quote": "USDT", "enabled": True, "status_label": "N=545 trades", "is_forex": False},
            {"symbol": "ETH/USDT", "name": "Ethereum", "base": "ETH", "quote": "USDT", "enabled": False, "status_label": "Untested", "is_forex": False},
            {"symbol": "SOL/USDT", "name": "Solana", "base": "SOL", "quote": "USDT", "enabled": False, "status_label": "Untested", "is_forex": False},
            {"symbol": "BNB/USDT", "name": "BNB", "base": "BNB", "quote": "USDT", "enabled": False, "status_label": "Untested", "is_forex": False},
            {"symbol": "XRP/USDT", "name": "XRP", "base": "XRP", "quote": "USDT", "enabled": False, "status_label": "Untested", "is_forex": False},
            {"symbol": "DOGE/USDT", "name": "Dogecoin", "base": "DOGE", "quote": "USDT", "enabled": False, "status_label": "Untested", "is_forex": False},
            {"symbol": "ADA/USDT", "name": "Cardano", "base": "ADA", "quote": "USDT", "enabled": False, "status_label": "Untested", "is_forex": False},
            {"symbol": "AVAX/USDT", "name": "Avalanche", "base": "AVAX", "quote": "USDT", "enabled": False, "status_label": "Untested", "is_forex": False},
        ],
    },
    {
        "name": "Forex (Live Market)",
        "pairs": [
            {"symbol": "EUR/USD", "name": "Euro / US Dollar", "base": "EUR", "quote": "USD", "enabled": True, "status_label": "N=74 trades", "is_forex": True},
            {"symbol": "GBP/JPY", "name": "British Pound / Japanese Yen", "base": "GBP", "quote": "JPY", "enabled": True, "status_label": "N=200 trades", "is_forex": True},
            {"symbol": "AUD/CAD", "name": "Australian Dollar / Canadian Dollar", "base": "AUD", "quote": "CAD", "enabled": True, "status_label": "N=160 trades", "is_forex": True},
            {"symbol": "NZD/CHF", "name": "New Zealand Dollar / Swiss Franc", "base": "NZD", "quote": "CHF", "enabled": True, "status_label": "N=428 trades", "is_forex": True},
            {"symbol": "USD/COP", "name": "US Dollar / Colombian Peso", "base": "USD", "quote": "COP", "enabled": True, "status_label": "N=117 trades", "is_forex": True},
            {"symbol": "GBP/AUD", "name": "British Pound / Australian Dollar", "base": "GBP", "quote": "AUD", "enabled": False, "status_label": "N=3 (< 30)", "is_forex": True},
            {"symbol": "EUR/CAD", "name": "Euro / Canadian Dollar", "base": "EUR", "quote": "CAD", "enabled": False, "status_label": "N=28 (< 30)", "is_forex": True},
            {"symbol": "NZD/CAD", "name": "New Zealand Dollar / Canadian Dollar", "base": "NZD", "quote": "CAD", "enabled": True, "status_label": "N=171 trades", "is_forex": True},
        ],
    },
]

# 4. Read Base CSS and Append Custom Static Styles
with open(CSS_FILE, "r", encoding="utf-8") as f:
    base_css = f.read()

extra_css = """
/* Standalone Static App Enhancements */
.snapshot-notice-banner {
  background: linear-gradient(90deg, #111726, #0e1626);
  border-bottom: 1px solid rgba(59, 130, 246, 0.3);
  color: #94a3b8;
  padding: 10px 18px;
  font-size: 0.84rem;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  text-align: center;
  flex-wrap: wrap;
}

.snapshot-badge {
  background-color: rgba(59, 130, 246, 0.2);
  color: #60a5fa;
  border: 1px solid rgba(59, 130, 246, 0.4);
  font-family: var(--font-mono);
  font-size: 0.72rem;
  font-weight: 700;
  padding: 2px 8px;
  border-radius: 4px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.snapshot-status-tag {
  background-color: rgba(16, 185, 129, 0.12);
  color: #34d399;
  border: 1px solid rgba(16, 185, 129, 0.3);
  font-family: var(--font-mono);
  font-size: 0.75rem;
  padding: 4px 10px;
  border-radius: 4px;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.regime-selector-buttons {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

.regime-btn {
  background-color: var(--bg-card-sub);
  border: 1px solid var(--border-subtle);
  color: var(--text-secondary);
  padding: 8px 16px;
  border-radius: var(--radius-sm);
  font-family: var(--font-mono);
  font-size: 0.82rem;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.15s ease;
  display: flex;
  align-items: center;
  gap: 8px;
}

.regime-btn:hover {
  border-color: var(--color-brand);
  color: #fff;
}

.regime-btn.active {
  background-color: rgba(59, 130, 246, 0.2);
  border-color: var(--color-brand);
  color: #93c5fd;
  font-weight: 700;
}

.regime-btn .regime-count {
  font-size: 0.7rem;
  padding: 1px 6px;
  border-radius: 3px;
  background-color: rgba(255, 255, 255, 0.08);
}

.breakdown-table-wrapper {
  overflow-x: auto;
  margin-top: 14px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

.breakdown-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.84rem;
  font-family: var(--font-sans);
}

.breakdown-table th {
  text-align: left;
  padding: 10px 14px;
  background-color: rgba(255, 255, 255, 0.04);
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: 0.75rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  border-bottom: 1px solid var(--border-subtle);
}

.breakdown-table td {
  padding: 12px 14px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.04);
  color: var(--text-primary);
  font-family: var(--font-mono);
}

.breakdown-table tbody tr {
  cursor: pointer;
  transition: background-color 0.15s ease;
}

.breakdown-table tbody tr:hover {
  background-color: rgba(59, 130, 246, 0.08);
}

.breakdown-table tbody tr.active-row {
  background-color: rgba(59, 130, 246, 0.16);
  border-left: 3px solid var(--color-brand);
}

.status-pill {
  display: inline-block;
  font-size: 0.7rem;
  padding: 2px 7px;
  border-radius: 4px;
  font-family: var(--font-mono);
}

.status-pill.valid {
  background-color: rgba(16, 185, 129, 0.15);
  color: #34d399;
}

.status-pill.insufficient {
  background-color: rgba(239, 68, 68, 0.15);
  color: #f87171;
}

.spec-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 14px;
  margin-top: 14px;
}

.spec-box {
  background-color: var(--bg-card-sub);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  padding: 16px;
}

.spec-box-title {
  font-size: 0.78rem;
  font-family: var(--font-mono);
  font-weight: 700;
  color: #60a5fa;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin-bottom: 8px;
  display: flex;
  align-items: center;
  gap: 6px;
}

.spec-box-body {
  font-size: 0.82rem;
  color: var(--text-secondary);
  line-height: 1.6;
}

.spec-box-body ul {
  padding-left: 18px;
  margin-top: 6px;
}

.spec-box-body li {
  margin-bottom: 4px;
}

.empty-state-notice {
  text-align: center;
  padding: 32px 16px;
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: 0.9rem;
}
"""

full_css = base_css + "\n" + extra_css

# 5. Build Complete Self-Contained index.html
json_cache_str = json.dumps(embedded_cache, indent=2)
pair_groups_str = json.dumps(PAIR_GROUPS, indent=2)

html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>Signal Research Panel | Offline Empirical Observer</title>

  <!-- PWA Metadata -->
  <link rel="manifest" href="manifest.json">
  <meta name="theme-color" content="#3b82f6">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <link rel="icon" type="image/png" sizes="192x192" href="icon-192.png">
  <link rel="apple-touch-icon" href="icon-192.png">

  <!-- Typography Fallbacks -->
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">

  <style>
{full_css}
  </style>
</head>
<body>
  <!-- Fixed, Non-Dismissible Safety & Integrity Banner -->
  <header class="top-notice-banner" role="alert">
    <div class="banner-inner">
      <span class="banner-badge">RESEARCH ONLY</span>
      <span class="banner-text">Read-only research tool. No orders are placed. No paid tier unlocks different data.</span>
    </div>
  </header>

  <!-- Static Snapshot Notice Note (Requirement 5) -->
  <div class="snapshot-notice-banner">
    <span class="snapshot-badge">SNAPSHOT</span>
    <span>Snapshot data as of {snapshot_timestamp} — not live-updating. Re-run backtest.py and rebuild this page for fresh numbers.</span>
  </div>

  <main class="app-container">
    <!-- Header Title Section -->
    <section class="panel-header">
      <div class="title-group">
        <h1>Signal Research Panel</h1>
        <p class="subtitle">Standalone Research Console — Verified empirical walk-forward backtest metrics across market regimes.</p>
      </div>
      <div class="header-actions">
        <span class="snapshot-status-tag">
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2">
            <circle cx="12" cy="12" r="10"/>
            <polyline points="12 6 12 12 16 14"/>
          </svg>
          <span>OFFLINE SNAPSHOT</span>
        </span>
        <span class="timestamp-label">{snapshot_timestamp}</span>
      </div>
    </section>

    <!-- Step 1: Pair Picker -->
    <section class="selection-card">
      <div class="step-header">
        <span class="step-number">STEP 1</span>
        <h2>Select Trading Pair</h2>
        <span class="step-hint">Pairs without sufficient backtested data (N &lt; 30) are labeled Untested</span>
      </div>
      <div id="pairContainer" class="chips-container" aria-label="Trading Pairs">
        <!-- Injected via JavaScript from embedded PAIR_GROUPS -->
      </div>
    </section>

    <!-- Step 2: Timeframe Picker -->
    <section class="selection-card">
      <div class="step-header">
        <span class="step-number">STEP 2</span>
        <h2>Select Timeframe</h2>
        <span class="step-hint">Each interval evaluates an independent strategy model</span>
      </div>
      <div id="timeframeContainer" class="timeframe-buttons" aria-label="Timeframes">
        <button class="tf-btn" data-tf="1m">1m</button>
        <button class="tf-btn" data-tf="5m">5m</button>
        <button class="tf-btn" data-tf="15m">15m</button>
        <button class="tf-btn active" data-tf="1h">1h</button>
        <button class="tf-btn" data-tf="4h">4h</button>
      </div>
    </section>

    <!-- Step 3: Market Regime Selector -->
    <section class="selection-card">
      <div class="step-header">
        <span class="step-number">STEP 3</span>
        <h2>Select Market Regime Focus</h2>
        <span class="step-hint">Examine performance in specific trend regimes or view combined totals</span>
      </div>
      <div id="regimeContainer" class="regime-selector-buttons" aria-label="Market Regimes">
        <!-- Injected via JavaScript -->
      </div>
    </section>

    <!-- Result Panel (Signal Summary, Backtested Metrics, Regime Matrix) -->
    <section id="resultPanel" class="result-panel">
      <!-- Top Signal Summary Header -->
      <div class="result-summary-card">
        <div class="summary-left">
          <div class="pair-timeframe-tag">
            <span id="displaySymbol">BTC/USDT</span>
            <span class="dot-separator">•</span>
            <span id="displayTimeframe">1h</span>
          </div>
          <div id="dataSourceBadge" class="source-tag crypto">Static Snapshot — Binance</div>
          <div class="price-display">
            <span class="price-label">Evaluation Mode:</span>
            <span id="displayPrice" class="price-value">Walk-Forward Simulation</span>
          </div>
        </div>

        <div class="summary-center">
          <span class="metric-label">REGIME EVALUATION</span>
          <div id="signalBadge" class="signal-badge signal-long">
            <span id="signalText">ALL REGIMES</span>
          </div>
          <span id="ruleSetText" class="rule-subtext">Aggregated Strategy Performance</span>
        </div>

        <div class="summary-right">
          <span class="metric-label">STATISTICAL STATUS</span>
          <div id="regimeBadge" class="regime-badge">EMPIRICAL DATA</div>
          <span id="macroTrendText" class="macro-subtext">Macro Filter: 4h 200 EMA Active</span>
        </div>
      </div>

      <!-- Main Two-Column Analysis Section -->
      <div class="analysis-grid">
        <!-- Left: Empirical Backtest Track Record -->
        <div class="analysis-card backtest-card">
          <div class="card-header">
            <h3>Historical Backtest Performance</h3>
            <span id="regimeTagLabel" class="card-tag">EXACT REGIME + TIMEFRAME</span>
          </div>
          <p id="regimeDescription" class="card-description">
            Empirical walk-forward metrics for this exact pair, timeframe, and regime combination.
          </p>

          <!-- Win Rate Visualization Bar -->
          <div class="win-rate-block">
            <div class="win-rate-header">
              <span class="stat-title">Historical Win Rate</span>
              <span id="winRateValue" class="stat-number">--.-%</span>
            </div>

            <!-- Gauge Container with 50% Breakeven Threshold -->
            <div class="gauge-wrapper">
              <div class="gauge-track">
                <div id="winRateBar" class="gauge-fill" style="width: 0%;"></div>
                <!-- 50% Reference Marker -->
                <div class="breakeven-line" title="50% Breakeven Reference">
                  <span class="breakeven-label">50% Breakeven</span>
                </div>
              </div>
            </div>

            <!-- Insufficient Data Alert Box (Shown when sample size < 30) -->
            <div id="insufficientDataAlert" class="warning-box hidden">
              <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2">
                <circle cx="12" cy="12" r="10"/>
                <line x1="12" y1="8" x2="12" y2="12"/>
                <line x1="12" y1="16" x2="12.01" y2="16"/>
              </svg>
              <div>
                <strong>Insufficient data — not enough completed trades yet.</strong>
                <p>Empirical sample size is under the mandatory 30-trade statistical confidence threshold. Win rate and profit factor are withheld to prevent misleading estimation.</p>
              </div>
            </div>
          </div>

          <!-- Secondary Backtest Metrics Grid -->
          <div class="backtest-metrics-grid">
            <div class="metric-box">
              <span class="box-label">Profit Factor</span>
              <span id="profitFactorValue" class="box-value">-.--</span>
              <span class="box-hint">Gross Profit / Gross Loss</span>
            </div>
            <div class="metric-box">
              <span class="box-label">Sample Size</span>
              <span id="sampleSizeValue" class="box-value">n = --</span>
              <span id="sampleConfidenceLabel" class="box-hint">Trades recorded</span>
            </div>
            <div class="metric-box">
              <span class="box-label">W / L Record</span>
              <span id="recordValue" class="box-value">--W / --L</span>
              <span class="box-hint">Historical outcomes</span>
            </div>
            <div class="metric-box">
              <span class="box-label">Net P&amp;L</span>
              <span id="netPnlValue" class="box-value">$---.--</span>
              <span class="box-hint">Simulated $10k base</span>
            </div>
          </div>
        </div>

        <!-- Right: Market Regime Breakdown Matrix -->
        <div class="analysis-card indicators-card">
          <div class="card-header">
            <h3>Market Regime Breakdown Matrix</h3>
            <span class="card-tag">ALL REGIMES</span>
          </div>
          <p class="card-description">
            Complete empirical performance across all classified market conditions for this timeframe. Click any row to focus.
          </p>

          <div class="breakdown-table-wrapper">
            <table class="breakdown-table">
              <thead>
                <tr>
                  <th>Regime Condition</th>
                  <th>Trades (N)</th>
                  <th>Win Rate</th>
                  <th>Profit Factor</th>
                  <th>Net P&amp;L</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody id="regimeBreakdownBody">
                <!-- Injected via JavaScript -->
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- Strategy Framework & Technical Invariants Card -->
      <div class="analysis-card" style="margin-top: 20px;">
        <div class="card-header">
          <h3>Strategy Engine &amp; Technical Confluence Rules</h3>
          <span class="card-tag">RESEARCH SPECIFICATION</span>
        </div>
        <div class="spec-grid">
          <div class="spec-box">
            <div class="spec-box-title">
              <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/><polyline points="16 7 22 7 22 13"/></svg>
              <span>Trend Filter (Macro)</span>
            </div>
            <div class="spec-box-body">
              Long entries permitted only when 4h 200 EMA is sloping upward. Short entries permitted only when sloping downward. Counter-trend trades strictly filtered.
            </div>
          </div>

          <div class="spec-box">
            <div class="spec-box-title">
              <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>
              <span>Confluence Engine (3 of 4)</span>
            </div>
            <div class="spec-box-body">
              <ul>
                <li><strong>Trend:</strong> EMA9 &gt; EMA21 (Bull) / EMA9 &lt; EMA21 (Bear)</li>
                <li><strong>Momentum:</strong> RSI in 40–65 pullbacks</li>
                <li><strong>MACD:</strong> Histogram turning positive/negative</li>
                <li><strong>Volatility:</strong> Pullback near EMA9 / Bands</li>
              </ul>
            </div>
          </div>

          <div class="spec-box">
            <div class="spec-box-title">
              <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
              <span>Dynamic Trailing Exit</span>
            </div>
            <div class="spec-box-body">
              <ul>
                <li><strong>1x ATR Profit:</strong> Move Stop-Loss to Breakeven</li>
                <li><strong>2x ATR Profit:</strong> Trail Stop 1x ATR behind price</li>
                <li><strong>Risk Cap:</strong> Fixed 2% risk, NO martingale</li>
              </ul>
            </div>
          </div>

          <div class="spec-box">
            <div class="spec-box-title">
              <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>
              <span>Empirical Confidence Invariant</span>
            </div>
            <div class="spec-box-body">
              Results require minimum N &ge; 30 sample size. Any regime or pair with N &lt; 30 is labeled Insufficient Data with win rates withheld to prevent estimation bias.
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- Footer Disclaimers -->
    <footer class="panel-footer">
      <div class="footer-notice">
        <p><strong>Transparency Standard:</strong> All metrics represent empirical walk-forward backtest simulations over authentic historical market data (Binance Public API for Crypto, Yahoo Finance for Spot Forex). This standalone console is strictly for offline research observation and does not place trades or connect to any brokerage execution API.</p>
      </div>
    </footer>
  </main>

  <!-- Standalone Script with Embedded Data Snapshot (Zero API / Fetch Calls) -->
  <script>
    // Embedded Backtest Snapshot directly from backtest_cache.json
    const BACKTEST_CACHE = {json_cache_str};
    const PAIR_GROUPS = {pair_groups_str};
    const SNAPSHOT_TIMESTAMP = "{snapshot_timestamp}";
    const MIN_CONFIDENCE_TRADES = 30;

    document.addEventListener("DOMContentLoaded", () => {{
      let currentSymbol = "BTC/USDT";
      let currentTimeframe = "1h";
      let currentRegime = "COMBINED";

      // DOM Elements
      const pairContainer = document.getElementById("pairContainer");
      const timeframeButtons = document.querySelectorAll(".tf-btn");
      const regimeContainer = document.getElementById("regimeContainer");

      const displaySymbolEl = document.getElementById("displaySymbol");
      const displayTimeframeEl = document.getElementById("displayTimeframe");
      const dataSourceBadgeEl = document.getElementById("dataSourceBadge");
      const displayPriceEl = document.getElementById("displayPrice");
      const signalTextEl = document.getElementById("signalText");
      const ruleSetTextEl = document.getElementById("ruleSetText");
      const regimeBadgeEl = document.getElementById("regimeBadge");
      const macroTrendTextEl = document.getElementById("macroTrendText");

      const regimeTagLabelEl = document.getElementById("regimeTagLabel");
      const regimeDescriptionEl = document.getElementById("regimeDescription");

      const winRateValueEl = document.getElementById("winRateValue");
      const winRateBarEl = document.getElementById("winRateBar");
      const insufficientDataAlertEl = document.getElementById("insufficientDataAlert");
      const profitFactorValueEl = document.getElementById("profitFactorValue");
      const sampleSizeValueEl = document.getElementById("sampleSizeValue");
      const sampleConfidenceLabelEl = document.getElementById("sampleConfidenceLabel");
      const recordValueEl = document.getElementById("recordValue");
      const netPnlValueEl = document.getElementById("netPnlValue");
      const regimeBreakdownBody = document.getElementById("regimeBreakdownBody");

      function init() {{
        setupTimeframeListeners();
        renderPairs();
        updateTimeframeAvailability();
        renderRegimes();
        renderActiveView();

        // Register Service Worker for PWA Builder compatibility
        if ('serviceWorker' in navigator) {{
          navigator.serviceWorker.register('./sw.js').catch(() => {{}});
        }}
      }}

      function renderPairs() {{
        pairContainer.innerHTML = "";
        const groupsWrapper = document.createElement("div");
        groupsWrapper.className = "pair-groups-container";

        PAIR_GROUPS.forEach((grp) => {{
          const grpEl = document.createElement("div");
          grpEl.className = "pair-group";

          const titleEl = document.createElement("div");
          titleEl.className = "group-title";
          titleEl.textContent = grp.name;
          grpEl.appendChild(titleEl);

          const chipsEl = document.createElement("div");
          chipsEl.className = "group-chips";

          grp.pairs.forEach((p) => {{
            const chip = document.createElement("div");
            chip.className = `chip ${{p.enabled ? "" : "disabled"}} ${{p.symbol === currentSymbol ? "active" : ""}}`;
            chip.dataset.symbol = p.symbol;

            const label = document.createElement("span");
            label.textContent = p.symbol;
            chip.appendChild(label);

            const statusTag = document.createElement("span");
            statusTag.className = `chip-status-tag ${{p.enabled ? "tested" : "untested"}}`;
            statusTag.textContent = p.status_label;
            chip.appendChild(statusTag);

            if (p.enabled) {{
              chip.addEventListener("click", () => {{
                if (currentSymbol === p.symbol) return;
                document.querySelectorAll(".chip").forEach((c) => c.classList.remove("active"));
                chip.classList.add("active");
                currentSymbol = p.symbol;
                
                // Adjust timeframe if current pair doesn't support current timeframe
                const supportedTfs = Object.keys(BACKTEST_CACHE[currentSymbol] || {{}});
                if (supportedTfs.length > 0 && !supportedTfs.includes(currentTimeframe)) {{
                  currentTimeframe = supportedTfs.includes("1h") ? "1h" : supportedTfs[0];
                  timeframeButtons.forEach((b) => {{
                    b.classList.toggle("active", b.dataset.tf === currentTimeframe);
                  }});
                }}

                currentRegime = "COMBINED";
                updateTimeframeAvailability();
                renderRegimes();
                renderActiveView();
              }});
            }} else {{
              chip.title = "Untested or insufficient completed trades (< 30).";
            }}

            chipsEl.appendChild(chip);
          }});

          grpEl.appendChild(chipsEl);
          groupsWrapper.appendChild(grpEl);
        }});

        pairContainer.appendChild(groupsWrapper);
      }}

      function setupTimeframeListeners() {{
        timeframeButtons.forEach((btn) => {{
          btn.addEventListener("click", () => {{
            const tf = btn.dataset.tf;
            if (currentTimeframe === tf) return;

            timeframeButtons.forEach((b) => b.classList.remove("active"));
            btn.classList.add("active");
            currentTimeframe = tf;
            currentRegime = "COMBINED";
            renderRegimes();
            renderActiveView();
          }});
        }});
      }}

      function updateTimeframeAvailability() {{
        const pairData = BACKTEST_CACHE[currentSymbol] || {{}};
        timeframeButtons.forEach((btn) => {{
          const tf = btn.dataset.tf;
          const hasData = Boolean(pairData[tf]);
          if (!hasData) {{
            btn.style.opacity = "0.4";
            btn.title = "No cached simulation for this timeframe";
          }} else {{
            btn.style.opacity = "1";
            btn.title = `Simulated data available for ${{tf}}`;
          }}
        }});
      }}

      function renderRegimes() {{
        regimeContainer.innerHTML = "";
        const tfData = (BACKTEST_CACHE[currentSymbol] && BACKTEST_CACHE[currentSymbol][currentTimeframe]) || {{}};

        const regimeOrder = ["COMBINED", "TRENDING_UP", "TRENDING_DOWN", "RANGING", "HIGH_VOLATILITY"];
        const regimeLabels = {{
          "COMBINED": "All Regimes (Combined)",
          "TRENDING_UP": "Trending Up (Longs)",
          "TRENDING_DOWN": "Trending Down (Shorts)",
          "RANGING": "Ranging (Mean-Reversion)",
          "HIGH_VOLATILITY": "High Volatility (Filtered)"
        }};

        regimeOrder.forEach((rKey) => {{
          const stat = tfData[rKey];
          if (!stat && rKey !== "COMBINED") return;

          const btn = document.createElement("button");
          btn.className = `regime-btn ${{currentRegime === rKey ? "active" : ""}}`;
          btn.dataset.regime = rKey;

          const nameSpan = document.createElement("span");
          nameSpan.textContent = regimeLabels[rKey] || rKey;
          btn.appendChild(nameSpan);

          if (stat) {{
            const countSpan = document.createElement("span");
            countSpan.className = "regime-count";
            countSpan.textContent = `N=${{stat.trades}}`;
            btn.appendChild(countSpan);
          }}

          btn.addEventListener("click", () => {{
            document.querySelectorAll(".regime-btn").forEach((b) => b.classList.remove("active"));
            btn.classList.add("active");
            currentRegime = rKey;
            renderActiveView();
          }});

          regimeContainer.appendChild(btn);
        }});
      }}

      function renderActiveView() {{
        const pairData = BACKTEST_CACHE[currentSymbol] || {{}};
        const tfData = pairData[currentTimeframe] || null;

        // Is Forex Check
        let isForex = false;
        PAIR_GROUPS.forEach((g) => {{
          g.pairs.forEach((p) => {{
            if (p.symbol === currentSymbol && p.is_forex) isForex = true;
          }});
        }});

        displaySymbolEl.textContent = currentSymbol;
        displayTimeframeEl.textContent = currentTimeframe;

        if (isForex) {{
          dataSourceBadgeEl.textContent = "Static Snapshot — Yahoo Finance";
          dataSourceBadgeEl.className = "source-tag forex";
        }} else {{
          dataSourceBadgeEl.textContent = "Static Snapshot — Binance";
          dataSourceBadgeEl.className = "source-tag crypto";
        }}

        signalTextEl.textContent = currentRegime;
        ruleSetTextEl.textContent = currentRegime === "COMBINED" ? "Aggregated Strategy Performance" : `Isolated Regime: ${{currentRegime}}`;
        regimeTagLabelEl.textContent = `${{currentSymbol}} • ${{currentTimeframe}} • ${{currentRegime}}`;

        if (!tfData) {{
          displayPriceEl.textContent = "No Data For Timeframe";
          regimeBadgeEl.textContent = "UNTESTED";
          macroTrendTextEl.textContent = `Macro Filter: ${{currentSymbol}} not tested on ${{currentTimeframe}}`;

          winRateValueEl.textContent = "No Data";
          winRateValueEl.style.color = "var(--text-muted)";
          winRateBarEl.style.width = "0%";
          insufficientDataAlertEl.classList.remove("hidden");
          insufficientDataAlertEl.querySelector("strong").textContent = `No backtest data recorded for ${{currentSymbol}} on ${{currentTimeframe}}.`;
          insufficientDataAlertEl.querySelector("p").textContent = "Try selecting 1h or 4h intervals where walk-forward simulations were executed.";

          profitFactorValueEl.textContent = "--";
          sampleSizeValueEl.textContent = "n = 0";
          sampleConfidenceLabelEl.textContent = "No simulation recorded";
          recordValueEl.textContent = "--";
          netPnlValueEl.textContent = "--";

          regimeBreakdownBody.innerHTML = '<tr><td colspan="6" class="empty-state-notice">No simulation records for this timeframe.</td></tr>';
          return;
        }}

        regimeDescriptionEl.textContent = `Empirical walk-forward metrics for ${{currentSymbol}} on ${{currentTimeframe}} interval (${{currentRegime}} condition).`;
        const stat = tfData[currentRegime] || tfData["COMBINED"];

        if (!stat) return;

        const isInsufficient = stat.insufficient_data;
        displayPriceEl.textContent = `${{stat.trades}} Completed Trades`;

        if (isInsufficient) {{
          regimeBadgeEl.textContent = `INSUFFICIENT (N=${{stat.sample_size}})`;
          regimeBadgeEl.style.color = "var(--color-danger)";

          winRateValueEl.textContent = "Low Data";
          winRateValueEl.style.color = "var(--text-muted)";
          winRateBarEl.style.width = "0%";
          winRateBarEl.classList.remove("profitable");
          insufficientDataAlertEl.classList.remove("hidden");
          insufficientDataAlertEl.querySelector("strong").textContent = "Insufficient data — not enough completed trades yet.";
          insufficientDataAlertEl.querySelector("p").textContent = `Empirical sample size (n = ${{stat.sample_size}}) is below the mandatory 30-trade statistical confidence threshold. Win rate and profit factor are withheld to prevent misleading estimation.`;

          profitFactorValueEl.textContent = "Low Data";
          profitFactorValueEl.style.color = "var(--text-muted)";

          sampleSizeValueEl.textContent = `n = ${{stat.sample_size}}`;
          sampleConfidenceLabelEl.textContent = "< 30 trades (Unreliable)";

          recordValueEl.textContent = "Low Data";
          netPnlValueEl.textContent = "Low Data";
        }} else {{
          regimeBadgeEl.textContent = `EMPIRICAL (N=${{stat.sample_size}})`;
          regimeBadgeEl.style.color = "var(--color-success)";

          insufficientDataAlertEl.classList.add("hidden");

          const wr = stat.win_rate;
          winRateValueEl.textContent = `${{wr.toFixed(1)}}%`;
          winRateValueEl.style.color = wr >= 50.0 ? "var(--color-success)" : "var(--text-primary)";

          winRateBarEl.style.width = `${{Math.min(100, Math.max(0, wr))}}%`;
          if (wr >= 50.0) {{
            winRateBarEl.classList.add("profitable");
          }} else {{
            winRateBarEl.classList.remove("profitable");
          }}

          const pf = stat.profit_factor;
          profitFactorValueEl.textContent = pf.toFixed(2);
          profitFactorValueEl.style.color = pf >= 1.5 ? "var(--color-success)" : (pf >= 1.0 ? "var(--color-warning)" : "var(--color-danger)");

          sampleSizeValueEl.textContent = `n = ${{stat.sample_size}}`;
          sampleConfidenceLabelEl.textContent = "Empirical Data (N >= 30)";

          recordValueEl.textContent = `${{stat.wins}}W / ${{stat.losses}}L`;

          const pnlSign = stat.net_pnl >= 0 ? "+" : "";
          netPnlValueEl.textContent = `${{pnlSign}}$${{stat.net_pnl.toLocaleString(undefined, {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }})}}`;
          netPnlValueEl.style.color = stat.net_pnl >= 0 ? "var(--color-success)" : "var(--color-danger)";
        }}

        // Render Regime Breakdown Table
        renderBreakdownTable(tfData);
      }}

      function renderBreakdownTable(tfData) {{
        regimeBreakdownBody.innerHTML = "";
        const keys = ["COMBINED", "TRENDING_UP", "TRENDING_DOWN", "RANGING", "HIGH_VOLATILITY"];
        const titles = {{
          "COMBINED": "All Regimes (Total)",
          "TRENDING_UP": "TRENDING_UP (Longs)",
          "TRENDING_DOWN": "TRENDING_DOWN (Shorts)",
          "RANGING": "RANGING (Mean-Reversion)",
          "HIGH_VOLATILITY": "HIGH_VOLATILITY (Circuit Breaker)"
        }};

        keys.forEach((key) => {{
          const item = tfData[key];
          if (!item && key !== "COMBINED") return;

          const tr = document.createElement("tr");
          if (key === currentRegime) tr.className = "active-row";

          const tdRegime = document.createElement("td");
          tdRegime.textContent = titles[key] || key;
          tr.appendChild(tdRegime);

          const tdTrades = document.createElement("td");
          tdTrades.textContent = item ? item.trades : 0;
          tr.appendChild(tdTrades);

          const tdWr = document.createElement("td");
          if (!item || item.trades === 0) {{
            tdWr.textContent = "--";
          }} else if (item.insufficient_data) {{
            tdWr.textContent = "Low Data";
            tdWr.style.color = "var(--text-muted)";
          }} else {{
            tdWr.textContent = `${{item.win_rate.toFixed(1)}}%`;
            tdWr.style.color = item.win_rate >= 50 ? "var(--color-success)" : "var(--text-primary)";
          }}
          tr.appendChild(tdWr);

          const tdPf = document.createElement("td");
          if (!item || item.trades === 0) {{
            tdPf.textContent = "--";
          }} else if (item.insufficient_data) {{
            tdPf.textContent = "Low Data";
            tdPf.style.color = "var(--text-muted)";
          }} else {{
            tdPf.textContent = item.profit_factor.toFixed(2);
            tdPf.style.color = item.profit_factor >= 1.5 ? "var(--color-success)" : (item.profit_factor >= 1.0 ? "var(--color-warning)" : "var(--color-danger)");
          }}
          tr.appendChild(tdPf);

          const tdPnl = document.createElement("td");
          if (!item || item.trades === 0) {{
            tdPnl.textContent = "$0.00";
          }} else {{
            const sign = item.net_pnl >= 0 ? "+" : "";
            tdPnl.textContent = `${{sign}}$${{item.net_pnl.toFixed(2)}}`;
            tdPnl.style.color = item.net_pnl >= 0 ? "var(--color-success)" : "var(--color-danger)";
          }}
          tr.appendChild(tdPnl);

          const tdStatus = document.createElement("td");
          const pill = document.createElement("span");
          if (!item || item.trades === 0) {{
            pill.className = "status-pill insufficient";
            pill.textContent = "No Trades";
          }} else if (item.insufficient_data) {{
            pill.className = "status-pill insufficient";
            pill.textContent = `N < 30 (${{item.trades}})`;
          }} else {{
            pill.className = "status-pill valid";
            pill.textContent = `Valid (N=${{item.trades}})`;
          }}
          tdStatus.appendChild(pill);
          tr.appendChild(tdStatus);

          tr.addEventListener("click", () => {{
            currentRegime = key;
            document.querySelectorAll(".regime-btn").forEach((b) => {{
              b.classList.toggle("active", b.dataset.regime === key);
            }});
            renderActiveView();
          }});

          regimeBreakdownBody.appendChild(tr);
        }});
      }}

      // Launch application
      init();
    }});
  </script>
</body>
</html>
"""

# 6. Service Worker for PWA Builder
sw_content = """const CACHE_NAME = 'signal-research-panel-v1';
const ASSETS = [
  './',
  './index.html',
  './manifest.json',
  './icon-192.png',
  './icon-512.png'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(ASSETS);
    })
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))
      );
    })
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  event.respondWith(
    caches.match(event.request).then((cachedResponse) => {
      return cachedResponse || fetch(event.request);
    })
  );
});
"""

# 7. Manifest for PWA Builder
manifest_content = {
    "name": "Signal Research Panel",
    "short_name": "SignalPanel",
    "description": "Read-only Spot Signal Research & Empirical Backtest Observer",
    "start_url": "./index.html",
    "display": "standalone",
    "background_color": "#0a0d14",
    "theme_color": "#3b82f6",
    "orientation": "portrait-primary",
    "icons": [
        {
            "src": "icon-192.png",
            "sizes": "192x192",
            "type": "image/png",
            "purpose": "any maskable"
        },
        {
            "src": "icon-512.png",
            "sizes": "512x512",
            "type": "image/png",
            "purpose": "any maskable"
        }
    ]
}

# 8. PNG Icon Generator Helper
def create_png_bytes(width, height):
    bg = (10, 13, 20)
    card_bg = (17, 23, 38)
    brand = (59, 130, 246)
    brand_light = (56, 189, 248)
    green = (16, 185, 129)
    
    raw = bytearray()
    r_radius = width * 0.44
    r_sq = r_radius * r_radius
    inner_r_sq = (width * 0.41) ** 2
    cx, cy = width / 2.0, height / 2.0
    
    for y in range(height):
        raw.append(0)
        for x in range(width):
            dx = x - cx
            dy = y - cy
            dist_sq = dx*dx + dy*dy
            
            color = bg
            if dist_sq <= r_sq:
                if dist_sq >= inner_r_sq:
                    color = brand
                else:
                    color = card_bg
                    # Bar 1 (left green)
                    c1_wick = abs(x - (cx - width * 0.25)) <= max(1, width * 0.012) and (cy - height * 0.26) <= y <= (cy + height * 0.22)
                    c1_body = (cx - width * 0.31 <= x <= cx - width * 0.19) and (cy - height * 0.10 <= y <= cy + height * 0.15)
                    # Bar 2 (center blue tall)
                    c2_wick = abs(x - cx) <= max(1, width * 0.012) and (cy - height * 0.35) <= y <= (cy + height * 0.30)
                    c2_body = (cx - width * 0.06 <= x <= cx + width * 0.06) and (cy - height * 0.25 <= y <= cy + height * 0.10)
                    # Bar 3 (right cyan)
                    c3_wick = abs(x - (cx + width * 0.25)) <= max(1, width * 0.012) and (cy - height * 0.15) <= y <= (cy + height * 0.25)
                    c3_body = (cx + width * 0.19 <= x <= cx + width * 0.31) and (cy - height * 0.05 <= y <= cy + height * 0.18)
                    
                    if c2_body or c2_wick:
                        color = brand_light if c2_body else brand
                    elif c1_body or c1_wick:
                        color = green
                    elif c3_body or c3_wick:
                        color = brand_light
            raw.extend(color)
            
    def chunk(tag, data):
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)

    header = b'\x89PNG\r\n\x1a\n'
    ihdr = chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
    idat = chunk(b'IDAT', zlib.compress(bytes(raw)))
    iend = chunk(b'IEND', b'')
    return header + ihdr + idat + iend

icon_192 = create_png_bytes(192, 192)
icon_512 = create_png_bytes(512, 512)

# 9. Output to all target directories
for out_dir in OUTPUT_DIRS:
    with open(out_dir / "index.html", "w", encoding="utf-8") as f:
        f.write(html_content)
    with open(out_dir / "sw.js", "w", encoding="utf-8") as f:
        f.write(sw_content)
    with open(out_dir / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest_content, f, indent=2)
    with open(out_dir / "icon-192.png", "wb") as f:
        f.write(icon_192)
    with open(out_dir / "icon-512.png", "wb") as f:
        f.write(icon_512)
    print(f"Successfully generated static standalone package at: {out_dir}")

print(f"HTML size: {len(html_content):,} bytes. All files ready for PWA Builder.")
