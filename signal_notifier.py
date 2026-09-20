"""
Signal-Only Notification Layer for Binance Spot Bot.
Strict Observer:
  - Fetches latest market candles via data_feed.py.
  - Detects market regime via regime_detector.py.
  - Evaluates entry signals via strategy.generate_signal().
  - Formats human-readable alert notifications with verified historical backtest metrics.
  - Appends actionable signals to spot_bot/signals_log.csv.
  - Dispatches optional Telegram alerts if configured in .env.
  - ⚠️ ZERO order execution, ZERO trade placement, ZERO broker/exchange buying or selling.
"""

import sys
import os
import time
import json
import csv
import argparse
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Tuple
import pandas as pd
from dotenv import load_dotenv

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Load environment variables from .env if present
env_candidates = [
    Path(__file__).resolve().parent / ".env",
    Path(__file__).resolve().parent.parent / ".env",
    Path.cwd() / ".env",
]
for p in env_candidates:
    if p.exists():
        load_dotenv(p)

try:
    from . import config
    from . import data_feed
    from . import strategy
    from . import regime_detector
    from .logger import get_logger
except ImportError:
    import config
    import data_feed
    import strategy
    import regime_detector
    from logger import get_logger

logger = get_logger("signal_notifier")

MODULE_DIR = Path(__file__).resolve().parent
DATA_DIR = MODULE_DIR / "data"
SIGNALS_LOG_PATH = MODULE_DIR / "signals_log.csv"
BACKTEST_STATS_PATH = DATA_DIR / "backtest_regime_stats.json"

CSV_HEADERS = [
    "timestamp",
    "symbol",
    "direction",
    "regime",
    "entry",
    "stop_loss",
    "take_profit",
    "historical_win_rate",
    "historical_profit_factor",
]

# Baseline fallback regime metrics if json file is unavailable
DEFAULT_BACKTEST_STATS = {
    "TRENDING_UP": {"total_trades": 169, "win_rate_pct": 15.98, "profit_factor": 0.11, "sufficient_data": True},
    "TRENDING_DOWN": {"total_trades": 154, "win_rate_pct": 14.29, "profit_factor": 0.10, "sufficient_data": True},
    "RANGING": {"total_trades": 222, "win_rate_pct": 19.37, "profit_factor": 0.11, "sufficient_data": True},
    "HIGH_VOLATILITY": {"total_trades": 0, "win_rate_pct": 0.0, "profit_factor": 0.0, "sufficient_data": False},
}


def load_backtest_regime_stats() -> Dict[str, Dict[str, Any]]:
    """Loads verified walk-forward backtest statistics per market regime."""
    if BACKTEST_STATS_PATH.exists():
        try:
            with open(BACKTEST_STATS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            logger.warning(f"Could not load {BACKTEST_STATS_PATH}: {exc}. Using default stats.")
    return DEFAULT_BACKTEST_STATS


def get_regime_performance_text(regime: str) -> Tuple[str, Dict[str, Any]]:
    """
    Returns the mandatory performance track record text line for a given regime.
    If backtest sample size is under 30 trades, reports low confidence.
    """
    stats_data = load_backtest_regime_stats()
    regime_stats = stats_data.get(regime, {})

    sample_size = regime_stats.get("total_trades", regime_stats.get("sample_size", 0))
    win_rate = regime_stats.get("win_rate_pct", 0.0)
    pf = regime_stats.get("profit_factor", 0.0)

    # Threshold: at least 30 historical trades required for statistical validity
    if sample_size >= 30 and regime_stats.get("sufficient_data", True):
        text_line = f"Historical backtest performance for this regime: Win rate {win_rate:.1f}%, Profit Factor {pf:.2f}"
    else:
        text_line = (
            f"Historical backtest performance for this regime: Insufficient backtest data — "
            f"low confidence ({sample_size} trades recorded)"
        )

    return text_line, regime_stats


def init_signals_csv() -> None:
    """Initializes spot_bot/signals_log.csv with headers if it does not already exist."""
    if not SIGNALS_LOG_PATH.exists():
        try:
            with open(SIGNALS_LOG_PATH, mode="w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(CSV_HEADERS)
            logger.info(f"Initialized signals CSV log: {SIGNALS_LOG_PATH.name}")
        except Exception as exc:
            logger.error(f"Failed to initialize {SIGNALS_LOG_PATH}: {exc}")


def append_signal_to_csv(
    timestamp_str: str,
    symbol: str,
    direction: str,
    regime: str,
    entry: float,
    stop_loss: float,
    take_profit: float,
    regime_stats: Dict[str, Any],
) -> None:
    """Appends an emitted signal record to spot_bot/signals_log.csv."""
    init_signals_csv()
    sufficient = regime_stats.get("sufficient_data", False) and regime_stats.get("total_trades", 0) >= 30
    win_rate_str = f"{regime_stats.get('win_rate_pct', 0.0):.2f}%" if sufficient else "LOW_DATA"
    pf_str = f"{regime_stats.get('profit_factor', 0.0):.2f}" if sufficient else "LOW_DATA"

    row = [
        timestamp_str,
        symbol,
        direction,
        regime,
        f"{entry:.2f}",
        f"{stop_loss:.2f}",
        f"{take_profit:.2f}",
        win_rate_str,
        pf_str,
    ]

    try:
        with open(SIGNALS_LOG_PATH, mode="a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(row)
        logger.info(f"Appended signal to {SIGNALS_LOG_PATH.name} -> {direction} @ ${entry:,.2f}")
    except Exception as exc:
        logger.error(f"Failed writing to {SIGNALS_LOG_PATH}: {exc}")


def send_telegram_alert(message: str) -> bool:
    """
    Sends an alert notification via Telegram Bot API using credentials from .env.
    Gracefully skips if TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID are unset.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        logger.debug("Telegram credentials not configured; skipping Telegram notification.")
        return False

    api_url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": message}).encode("utf-8")
    req = urllib.request.Request(
        api_url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "SpotSignalNotifier/1.0"},
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            if response.status == 200:
                logger.info("Successfully dispatched Telegram alert notification.")
                return True
            else:
                logger.warning(f"Telegram API responded with HTTP {response.status}")
                return False
    except Exception as exc:
        logger.warning(f"Telegram notification dispatch skipped / failed: {exc}")
        return False


def format_alert_text(
    symbol: str,
    timeframe: str,
    timestamp_str: str,
    direction: str,
    regime: str,
    rule_set: str,
    entry: float,
    stop_loss: float,
    take_profit: float,
    atr_val: float,
    rr_ratio: float,
    performance_line: str,
) -> str:
    """Formats human-readable plain-text alert with all mandatory fields."""
    lines = [
        "================================================================================",
        f"[SPOT SIGNAL ALERT] - {symbol} ({timeframe})",
        "================================================================================",
        f"Timestamp          : {timestamp_str}",
        f"Symbol             : {symbol}",
        f"Timeframe          : {timeframe}",
        f"Signal Direction   : {direction}",
        f"Detected Regime    : {regime}",
        f"Trigger Rule       : {rule_set}",
        f"Suggested Entry    : ${entry:,.2f}",
        f"Suggested Stop-Loss: ${stop_loss:,.2f}",
        f"Suggested Take-Prof: ${take_profit:,.2f}",
        f"ATR ({config.ATR_PERIOD})            : ${atr_val:,.2f}",
        f"Risk / Reward      : 1:{rr_ratio:.2f}",
        "",
        f"{performance_line}",
        "",
        "[DISCLAIMER] Informational signal only. ZERO live orders or trades executed.",
        "================================================================================",
    ]
    return "\n".join(lines)


def evaluate_market(
    symbol: str = config.DEFAULT_SYMBOL,
    timeframe: str = config.DEFAULT_TIMEFRAME,
    df_live: Optional[pd.DataFrame] = None,
    df_htf: Optional[pd.DataFrame] = None,
) -> Tuple[strategy.EntryDecision, pd.DataFrame, str]:
    """
    Fetches market data, determines HTF macro trend, classifies regime, and evaluates signal.
    Pure observer — no side effects on trading state.
    """
    # 1. Fetch live candles if not provided
    if df_live is None:
        logger.info(f"Fetching latest live {timeframe} candles for {symbol}...")
        df_live = data_feed.fetch_live_candles(symbol=symbol, timeframe=timeframe, count=100)

    # 2. Fetch higher-timeframe data for macro trend
    if df_htf is None:
        logger.info(f"Fetching higher-timeframe 4h candles for {symbol} macro trend...")
        df_htf = data_feed.fetch_higher_timeframe_data(
            symbol=symbol,
            timeframe="4h",
            limit=500,
            ema_period=200,
            use_cache=True,
        )

    # 3. Attach HTF macro trend
    df_merged = data_feed.attach_higher_timeframe_trend(df_live, df_htf)
    htf_trend_val = str(df_merged["htf_trend"].iloc[-1])

    # 4. Regime classification (identical to backtest)
    regime_result = regime_detector.detect_regime(df_merged, htf_trend=htf_trend_val)

    # 5. Signal generation (reusing strategy.generate_signal / evaluate_entry)
    decision = strategy.generate_signal(
        df=df_merged,
        htf_trend=htf_trend_val,
        regime_override=regime_result,
    )

    return decision, df_merged, htf_trend_val


def check_and_notify(
    symbol: str = config.DEFAULT_SYMBOL,
    timeframe: str = config.DEFAULT_TIMEFRAME,
    custom_df: Optional[pd.DataFrame] = None,
    silent_hold: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Executes a single check on the latest closed candle.
    If a signal triggers, formats and outputs the alert, logs to CSV, and sends Telegram alert.
    If no signal triggers, outputs an explanation of current market conditions.

    Returns:
        Dict with alert details if signal fired, or None if HOLD / NO_SIGNAL.
    """
    decision, df_data, htf_trend = evaluate_market(
        symbol=symbol,
        timeframe=timeframe,
        df_live=custom_df,
    )

    latest_bar = df_data.iloc[-1]
    ts_val = latest_bar["datetime"] if "datetime" in latest_bar else datetime.now(timezone.utc)
    if isinstance(ts_val, pd.Timestamp):
        ts_str = ts_val.strftime("%Y-%m-%d %H:%M:%S UTC")
    else:
        ts_str = str(ts_val)

    # Actionable signal check (BUY or SHORT)
    if decision.signal in ("BUY", "SHORT"):
        direction_label = "LONG (BUY)" if decision.signal == "BUY" else "SHORT (SELL)"
        perf_line, stats_record = get_regime_performance_text(decision.regime)

        alert_text = format_alert_text(
            symbol=symbol,
            timeframe=timeframe,
            timestamp_str=ts_str,
            direction=direction_label,
            regime=decision.regime,
            rule_set=decision.rule_set,
            entry=decision.entry_price,
            stop_loss=decision.stop_loss,
            take_profit=decision.take_profit,
            atr_val=decision.atr_value,
            rr_ratio=decision.risk_reward_ratio,
            performance_line=perf_line,
        )

        # 1. Output to console
        print("\n" + alert_text + "\n")

        # 2. Append to signals_log.csv
        append_signal_to_csv(
            timestamp_str=ts_str,
            symbol=symbol,
            direction="LONG" if decision.signal == "BUY" else "SHORT",
            regime=decision.regime,
            entry=decision.entry_price,
            stop_loss=decision.stop_loss,
            take_profit=decision.take_profit,
            regime_stats=stats_record,
        )

        # 3. Dispatch Telegram alert (if configured)
        send_telegram_alert(alert_text)

        return {
            "signal": decision.signal,
            "direction": direction_label,
            "regime": decision.regime,
            "rule_set": decision.rule_set,
            "entry": decision.entry_price,
            "stop_loss": decision.stop_loss,
            "take_profit": decision.take_profit,
            "timestamp": ts_str,
            "alert_text": alert_text,
        }

    else:
        # Informative breakdown explaining why NO signal fired
        if not silent_hold:
            curr_close = float(latest_bar["close"])
            print("\n" + "-" * 80)
            print(f"[SPOT MARKET OBSERVER] - {symbol} ({timeframe}) - {ts_str}")
            print("-" * 80)
            print(f" Current Price      : ${curr_close:,.2f}")
            print(f" Detected Regime    : {decision.regime}")
            print(f" Macro HTF Trend    : {htf_trend}")
            print(f" Signal Status      : NO_SIGNAL (HOLD)")

            # Detailed rationale based on regime
            if decision.regime == "HIGH_VOLATILITY":
                print(" Reason             : HIGH_VOLATILITY circuit breaker active (all entries skipped).")
            elif decision.regime in ("TRENDING_UP", "TRENDING_DOWN"):
                score = decision.snapshot.get("score", "N/A")
                print(f" Reason             : Trend confluence score was {score} (requires >= 3/4 agreeing conditions).")
            elif decision.regime == "RANGING":
                print(" Reason             : Mean-reversion boundaries not reached (RSI not at extremes or BB edges).")
            else:
                print(f" Reason             : {decision.snapshot.get('reason', 'Conditions not met.')}")

            perf_line, _ = get_regime_performance_text(decision.regime)
            print(f" Regime Context     : {perf_line}")
            print(" Status             : Pure observer active. Waiting for next candle closure.")
            print("-" * 80 + "\n")

        return None


def run_notifier_loop(
    symbol: str = config.DEFAULT_SYMBOL,
    timeframe: str = config.DEFAULT_TIMEFRAME,
    poll_interval_sec: int = 60,
) -> None:
    """
    Runs continuous scheduled observer loop.
    Checks on candle closure or regular polling intervals without placing trades.
    """
    print("=" * 80)
    print(f" STARTING SPOT SIGNAL NOTIFIER (OBSERVER ONLY)")
    print(f" Symbol: {symbol} | Timeframe: {timeframe} | Polling Every: {poll_interval_sec}s")
    print(f" Execution: DISABLED (No trades or orders will be placed)")
    print(f" Signals Log: {SIGNALS_LOG_PATH}")
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    print(f" Telegram Alerts: {'ENABLED' if token else 'DISABLED (TELEGRAM_BOT_TOKEN not in .env)'}")
    print(" Press Ctrl+C to stop.")
    print("=" * 80 + "\n")

    init_signals_csv()
    last_processed_time = None

    try:
        while True:
            try:
                # Fetch recent candles
                df_live = data_feed.fetch_live_candles(symbol=symbol, timeframe=timeframe, count=100)
                if not df_live.empty:
                    latest_time = df_live["time"].iloc[-1]
                    if latest_time != last_processed_time:
                        last_processed_time = latest_time
                        check_and_notify(symbol=symbol, timeframe=timeframe, custom_df=df_live)
                    else:
                        logger.debug("Candle not yet closed; awaiting next candle timestamp.")

            except Exception as exc:
                logger.error(f"Error during notifier loop iteration: {exc}")

            time.sleep(poll_interval_sec)

    except KeyboardInterrupt:
        print("\n[INFO] Signal notifier loop halted by user.")


def main():
    parser = argparse.ArgumentParser(description="Spot Trading Signal Notifier (Strict Observer)")
    parser.add_argument("--symbol", type=str, default=config.DEFAULT_SYMBOL, help="Trading pair (default: BTC/USDT)")
    parser.add_argument("--timeframe", type=str, default=config.DEFAULT_TIMEFRAME, help="Candle timeframe (default: 1h)")
    parser.add_argument("--loop", action="store_true", help="Run continuously on schedule")
    parser.add_argument("--interval", type=int, default=60, help="Poll interval in seconds for loop mode")
    parser.add_argument("--demo-signal", action="store_true", help="Demonstrate an alert trigger firing using historical signal candle")

    args = parser.parse_args()

    init_signals_csv()

    if args.demo_signal:
        print("\n--> Running demonstration of confirmed signal firing...")
        # Load a historical window known to fire a signal from the cached 6-month dataset
        cache_file = DATA_DIR / "BTCUSDT_1h_4320.csv"
        if cache_file.exists():
            df_hist = pd.read_csv(cache_file)
            df_hist["datetime"] = pd.to_datetime(df_hist["time"], unit="ms", utc=True)
            df_htf = data_feed.fetch_higher_timeframe_data(symbol=args.symbol, timeframe="4h", limit=500, use_cache=True)
            df_merged = data_feed.attach_higher_timeframe_trend(df_hist, df_htf)

            # Find a bar where a BUY or SHORT signal was fired
            signal_found = False
            for i in range(100, min(1000, len(df_merged))):
                sub_df = df_merged.iloc[: i + 1]
                htf_trend = str(sub_df["htf_trend"].iloc[-1])
                regime = regime_detector.detect_regime(sub_df, htf_trend=htf_trend)
                decision = strategy.generate_signal(sub_df, htf_trend=htf_trend, regime_override=regime)
                if decision.signal in ("BUY", "SHORT"):
                    print(f"--> Found historical trigger bar at index {i} ({sub_df['datetime'].iloc[-1]}):")
                    check_and_notify(symbol=args.symbol, timeframe=args.timeframe, custom_df=sub_df)
                    signal_found = True
                    break

            if not signal_found:
                print("No signal bar found in sample range; running standard live check.")
                check_and_notify(symbol=args.symbol, timeframe=args.timeframe)
        else:
            check_and_notify(symbol=args.symbol, timeframe=args.timeframe)

    elif args.loop:
        run_notifier_loop(symbol=args.symbol, timeframe=args.timeframe, poll_interval_sec=args.interval)
    else:
        # Single live candle check
        print(f"\n--> Checking latest live candle for {args.symbol} ({args.timeframe})...")
        check_and_notify(symbol=args.symbol, timeframe=args.timeframe)


if __name__ == "__main__":
    main()
