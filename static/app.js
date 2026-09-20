/**
 * Signal Research Panel - Frontend Application Logic.
 * Strict Read-Only Client. Zero execution pathways.
 */

document.addEventListener("DOMContentLoaded", () => {
  let currentSymbol = "BTC/USDT";
  let currentTimeframe = "1h";
  let isFetching = false;

  // DOM Elements
  const pairContainer = document.getElementById("pairContainer");
  const timeframeButtons = document.querySelectorAll(".tf-btn");
  const refreshBtn = document.getElementById("refreshBtn");
  const lastUpdatedEl = document.getElementById("lastUpdated");

  // Summary Elements
  const displaySymbolEl = document.getElementById("displaySymbol");
  const displayTimeframeEl = document.getElementById("displayTimeframe");
  const displayPriceEl = document.getElementById("displayPrice");
  const signalBadgeEl = document.getElementById("signalBadge");
  const signalTextEl = document.getElementById("signalText");
  const ruleSetTextEl = document.getElementById("ruleSetText");
  const regimeBadgeEl = document.getElementById("regimeBadge");
  const macroTrendTextEl = document.getElementById("macroTrendText");

  // Backtest Elements
  const winRateValueEl = document.getElementById("winRateValue");
  const winRateBarEl = document.getElementById("winRateBar");
  const insufficientDataAlertEl = document.getElementById("insufficientDataAlert");
  const profitFactorValueEl = document.getElementById("profitFactorValue");
  const sampleSizeValueEl = document.getElementById("sampleSizeValue");
  const sampleConfidenceLabelEl = document.getElementById("sampleConfidenceLabel");
  const recordValueEl = document.getElementById("recordValue");
  const netPnlValueEl = document.getElementById("netPnlValue");

  // Indicator Elements
  const valEma9El = document.getElementById("valEma9");
  const valEma21El = document.getElementById("valEma21");
  const valEmaCrossStatusEl = document.getElementById("valEmaCrossStatus");
  const valRsiEl = document.getElementById("valRsi");
  const valRsiStatusEl = document.getElementById("valRsiStatus");
  const valMacdEl = document.getElementById("valMacd");
  const valMacdStatusEl = document.getElementById("valMacdStatus");
  const valBbPosEl = document.getElementById("valBbPos");
  const valBbStatusEl = document.getElementById("valBbStatus");
  const valAtrEl = document.getElementById("valAtr");
  const valRiskRewardEl = document.getElementById("valRiskReward");

  // Suggested Levels
  const levelEntryEl = document.getElementById("levelEntry");
  const levelSlEl = document.getElementById("levelSl");
  const levelTpEl = document.getElementById("levelTp");

  /**
   * Initializes the application by fetching supported pairs from /api/pairs.
   */
  async function init() {
    setupTimeframeListeners();
    setupRefreshListener();
    await loadPairs();
    await fetchSignalData(currentSymbol, currentTimeframe);

    // Auto-refresh poll every 30 seconds for live closed-bar updates
    setInterval(() => {
      fetchSignalData(currentSymbol, currentTimeframe, true);
    }, 30000);
  }

  /**
   * Helper to create an individual trading pair chip.
   */
  function createPairChip(p) {
    const chip = document.createElement("div");
    chip.className = `chip ${p.enabled ? "" : "disabled"} ${p.symbol === currentSymbol ? "active" : ""}`;
    chip.dataset.symbol = p.symbol;

    const label = document.createElement("span");
    label.textContent = p.symbol;
    chip.appendChild(label);

    const statusTag = document.createElement("span");
    statusTag.className = `chip-status-tag ${p.enabled ? "tested" : "untested"}`;
    statusTag.textContent = p.status_label;
    chip.appendChild(statusTag);

    if (p.enabled) {
      chip.addEventListener("click", () => {
        if (currentSymbol === p.symbol) return;
        document.querySelectorAll(".chip").forEach((c) => c.classList.remove("active"));
        chip.classList.add("active");
        currentSymbol = p.symbol;
        fetchSignalData(currentSymbol, currentTimeframe);
      });
    } else {
      chip.title = "Insufficient backtested trades (< 30) or untested. Run backtest.py to evaluate.";
    }

    return chip;
  }

  /**
   * Fetches pair catalogue from /api/pairs.
   */
  async function loadPairs() {
    try {
      const response = await fetch("/api/pairs");
      const data = await response.json();

      if (!data.success) {
        pairContainer.innerHTML = '<div class="chip disabled">Failed to load pairs</div>';
        return;
      }

      pairContainer.innerHTML = "";

      if (data.groups && data.groups.length > 0) {
        const groupsWrapper = document.createElement("div");
        groupsWrapper.className = "pair-groups-container";

        data.groups.forEach((grp) => {
          const grpEl = document.createElement("div");
          grpEl.className = "pair-group";

          const titleEl = document.createElement("div");
          titleEl.className = "group-title";
          titleEl.textContent = grp.name;
          grpEl.appendChild(titleEl);

          const chipsEl = document.createElement("div");
          chipsEl.className = "group-chips";

          grp.pairs.forEach((p) => {
            chipsEl.appendChild(createPairChip(p));
          });

          grpEl.appendChild(chipsEl);
          groupsWrapper.appendChild(grpEl);
        });

        pairContainer.appendChild(groupsWrapper);
      } else if (data.pairs) {
        data.pairs.forEach((p) => {
          pairContainer.appendChild(createPairChip(p));
        });
      }
    } catch (err) {
      console.error("Error loading pairs:", err);
      pairContainer.innerHTML = '<div class="chip disabled">Error loading pair registry</div>';
    }
  }

  /**
   * Configures timeframe selection buttons.
   */
  function setupTimeframeListeners() {
    timeframeButtons.forEach((btn) => {
      btn.addEventListener("click", () => {
        const tf = btn.dataset.tf;
        if (currentTimeframe === tf) return;

        timeframeButtons.forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        currentTimeframe = tf;
        fetchSignalData(currentSymbol, currentTimeframe);
      });
    });
  }

  /**
   * Configures manual refresh button.
   */
  function setupRefreshListener() {
    refreshBtn.addEventListener("click", () => {
      fetchSignalData(currentSymbol, currentTimeframe);
    });
  }

  /**
   * Fetches live signal and backtest data for selected pair + timeframe.
   */
  async function fetchSignalData(symbol, timeframe, isBackground = false) {
    if (isFetching) return;
    isFetching = true;

    if (!isBackground) {
      refreshBtn.style.opacity = "0.5";
      refreshBtn.querySelector("span").textContent = "Loading...";
    }

    try {
      const url = `/api/signal?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}`;
      const resp = await fetch(url);
      const data = await resp.json();

      if (!data.success) {
        alert(`Failed fetching signal: ${data.error || "Unknown error"}`);
        return;
      }

      renderSignalData(data);
    } catch (err) {
      console.error("Network error fetching signal:", err);
    } finally {
      isFetching = false;
      refreshBtn.style.opacity = "1";
      refreshBtn.querySelector("span").textContent = "Refresh";
    }
  }

  /**
   * Formats numeric price strings for Crypto vs Forex.
   */
  function formatPriceStr(val, isForex) {
    if (val === null || val === undefined) return "--";
    const num = Number(val);
    if (isNaN(num)) return String(val);

    if (isForex) {
      if (num < 5) return num.toFixed(5);
      if (num < 50) return num.toFixed(4);
      if (num < 500) return num.toFixed(3);
      return num.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }
    return `$${num.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  }

  /**
   * Updates all UI elements with API response.
   */
  function renderSignalData(data) {
    const isForex = Boolean(data.is_forex);

    // Header & Summary
    displaySymbolEl.textContent = data.symbol;
    displayTimeframeEl.textContent = data.timeframe;
    displayPriceEl.textContent = formatPriceStr(data.indicators.price, isForex);
    lastUpdatedEl.textContent = `Updated: ${data.timestamp}`;

    // Data Source Tag
    const dataSourceBadgeEl = document.getElementById("dataSourceBadge");
    if (dataSourceBadgeEl && data.data_source) {
      dataSourceBadgeEl.textContent = data.data_source;
      dataSourceBadgeEl.className = `source-tag ${isForex ? "forex" : "crypto"}`;
    }

    // Signal Badge
    signalTextEl.textContent = data.signal;
    signalBadgeEl.className = "signal-badge";

    if (data.signal === "LONG") {
      signalBadgeEl.classList.add("signal-long");
      signalTextEl.textContent = "LONG (BUY)";
    } else if (data.signal === "SHORT") {
      signalBadgeEl.classList.add("signal-short");
      signalTextEl.textContent = "SHORT (SELL)";
    } else {
      signalBadgeEl.classList.add("signal-hold");
      signalTextEl.textContent = "NO_SIGNAL";
    }

    ruleSetTextEl.textContent = `Trigger: ${data.rule_set}`;
    regimeBadgeEl.textContent = data.regime;
    macroTrendTextEl.textContent = `Macro Trend: ${data.macro_htf_trend} (4h 200 EMA)`;

    // =========================================================================
    // Empirical Backtest Section (Strict Confidence Enforcement)
    // =========================================================================
    const bt = data.backtest;

    if (bt.insufficient_data) {
      // Below 30 trades: Enforce honest "Insufficient data" notification
      winRateValueEl.textContent = "Low Data";
      winRateValueEl.style.color = "var(--text-muted)";
      winRateBarEl.style.width = "0%";
      winRateBarEl.classList.remove("profitable");
      insufficientDataAlertEl.classList.remove("hidden");

      profitFactorValueEl.textContent = "Low Data";
      profitFactorValueEl.style.color = "var(--text-muted)";

      sampleSizeValueEl.textContent = `n = ${bt.sample_size}`;
      sampleConfidenceLabelEl.textContent = "< 30 trades (Unreliable)";

      recordValueEl.textContent = "Low Data";
      netPnlValueEl.textContent = "Low Data";
    } else {
      // 30+ trades: Display authentic empirical metrics
      insufficientDataAlertEl.classList.add("hidden");

      const winRate = bt.win_rate;
      winRateValueEl.textContent = `${winRate.toFixed(1)}%`;
      winRateValueEl.style.color = winRate >= 50.0 ? "var(--color-success)" : "var(--text-primary)";

      winRateBarEl.style.width = `${Math.min(100, Math.max(0, winRate))}%`;
      if (winRate >= 50.0) {
        winRateBarEl.classList.add("profitable");
      } else {
        winRateBarEl.classList.remove("profitable");
      }

      profitFactorValueEl.textContent = bt.profit_factor.toFixed(2);
      profitFactorValueEl.style.color = bt.profit_factor >= 1.5 ? "var(--color-success)" : (bt.profit_factor >= 1.0 ? "var(--color-warning)" : "var(--color-danger)");

      sampleSizeValueEl.textContent = `n = ${bt.sample_size}`;
      sampleConfidenceLabelEl.textContent = bt.confidence_label;

      recordValueEl.textContent = `${bt.wins}W / ${bt.losses}L`;

      const pnlSign = bt.net_pnl >= 0 ? "+" : "";
      netPnlValueEl.textContent = `${pnlSign}$${bt.net_pnl.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
      netPnlValueEl.style.color = bt.net_pnl >= 0 ? "var(--color-success)" : "var(--color-danger)";
    }

    // =========================================================================
    // Live Indicator Snapshot
    // =========================================================================
    const ind = data.indicators;
    valEma9El.textContent = formatPriceStr(ind.ema9, isForex);
    valEma21El.textContent = formatPriceStr(ind.ema21, isForex);

    if (ind.ema9 > ind.ema21) {
      valEmaCrossStatusEl.textContent = "Bullish Stack (EMA9 > EMA21)";
      valEmaCrossStatusEl.style.color = "var(--color-success)";
    } else {
      valEmaCrossStatusEl.textContent = "Bearish Stack (EMA9 < EMA21)";
      valEmaCrossStatusEl.style.color = "var(--color-danger)";
    }

    valRsiEl.textContent = ind.rsi.toFixed(1);
    if (ind.rsi >= 70.0) {
      valRsiStatusEl.textContent = "Overbought (> 70)";
      valRsiStatusEl.style.color = "var(--color-danger)";
    } else if (ind.rsi <= 30.0) {
      valRsiStatusEl.textContent = "Oversold (< 30)";
      valRsiStatusEl.style.color = "var(--color-success)";
    } else {
      valRsiStatusEl.textContent = "Neutral Zone (30-70)";
      valRsiStatusEl.style.color = "var(--text-secondary)";
    }

    valMacdEl.textContent = (ind.macd_hist >= 0 ? "+" : "") + ind.macd_hist.toFixed(isForex ? 5 : 3);
    valMacdEl.style.color = ind.macd_hist >= 0 ? "var(--color-success)" : "var(--color-danger)";
    valMacdStatusEl.textContent = ind.macd_hist >= 0 ? "Positive Momentum" : "Negative Momentum";

    valBbPosEl.textContent = ind.bb_position.split(" ")[0];
    valBbStatusEl.textContent = ind.bb_position;

    valAtrEl.textContent = formatPriceStr(ind.atr, isForex);
    valRiskRewardEl.textContent = `1:${data.suggested_levels.risk_reward_ratio.toFixed(2)}`;

    // Suggested Levels (Informational Only)
    levelEntryEl.textContent = formatPriceStr(data.suggested_levels.entry, isForex);
    levelSlEl.textContent = formatPriceStr(data.suggested_levels.stop_loss, isForex);
    levelTpEl.textContent = formatPriceStr(data.suggested_levels.take_profit, isForex);
  }

  // Start the application
  init();
});
