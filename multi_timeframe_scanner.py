"""
Multi-Timeframe Signal Dashboard for Binance Spot Bot.
Strict Observer:
  - Scans across 1m, 5m, 15m, 1h, and 4h timeframes independently.
  - Detects market regime via regime_detector.py.
  - Evaluates entry signal via strategy.generate_signal().
  - Computes complete indicator snapshot (EMA9, EMA21, RSI, MACD Histogram, Bollinger Position).
  - Retrieves empirical backtest track record (Win Rate & Profit Factor) per timeframe + regime.
  - Enforces mandatory confidence fallback (< 30 trades -> "Insufficient data — low confidence").
  - Prints clean multi-timeframe dashboard table to console.
  - Appends timestamped snapshot rows to spot_bot/multi_tf_signals.csv.
  - ⚠️ ZERO order execution, ZERO trade placement, ZERO broker/exchange buying or selling.
"""

import sys
import os
import time
import json
import csv
import argparse
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Tuple
import pandas as pd
from dotenv import load_dotenv

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Load environment variables
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
    from . import indicators
    from . import regime_detector
    from . import backtest
    from .logger import get_logger
except ImportError:
    import config
    import data_feed
    import strategy
    import indicators
    import regime_detector
    import backtest
    from logger import get_logger

logger = get_logger("multi_tf_scanner")

MODULE_DIR = Path(__file__).resolve().parent
DATA_DIR = MODULE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE_PATH = DATA_DIR / "backtest_cache.json"
SIGNALS_CSV_PATH = MODULE_DIR / "multi_tf_signals.csv"

SUPPORTED_TIMEFRAMES: List[str] = ["1m", "5m", "15m", "1h", "4h"]
DEFAULT_SYMBOL: str = "BTC/USDT"
MIN_TRADES_CONFIDENCE: int = 30

CSV_HEADERS = [
    "timestamp",
    "symbol",
    "timeframe",
    "signal",
    "regime",
    "rule_set",
    "price",
    "ema9",
    "ema21",
    "rsi",
    "macd_hist",
    "bb_position",
    "backtested_win_rate",
    "backtested_profit_factor",
    "confidence",
]


def load_backtest_cache() -> Dict[str, Any]:
    """Loads cached backtest results across timeframes and regimes."""
    if CACHE_FILE_PATH.exists():
        try:
            with open(CACHE_FILE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            logger.warning(f"Could not read {CACHE_FILE_PATH.name}: {exc}. Using empty cache.")
    return {}


def save_backtest_cache(cache: Dict[str, Any]) -> None:
    """Saves updated backtest cache to disk."""
    try:
        with open(CACHE_FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
        logger.info(f"Updated backtest cache saved: {CACHE_FILE_PATH.name}")
    except Exception as exc:
        logger.error(f"Failed to write {CACHE_FILE_PATH.name}: {exc}")


def get_or_compute_backtest_stats(
    symbol: str,
    timeframe: str,
    regime: str,
    cache: Dict[str, Any],
) -> Tuple[str, str, str, Dict[str, Any]]:
    """
    Retrieves backtest win rate and profit factor for a given timeframe + regime.
    If not in cache, runs backtest.py once for that timeframe, records results to cache, and persists.
    Enforces the mandatory minimum 30-trade confidence threshold.
    """
    sym_cache = cache.get(symbol, cache) if symbol in cache else cache
    if timeframe not in sym_cache and timeframe not in cache:
        logger.info(f"Backtest stats for {timeframe} not found in cache. Running backtest simulation...")
        candle_count = 4320 if timeframe == "1h" else (1500 if timeframe == "4h" else 1000)
        try:
            bt_res = backtest.run_spot_backtest(
                symbol=symbol,
                timeframe=timeframe,
                candle_count=candle_count,
            )
            if symbol in cache:
                cache[symbol][timeframe] = bt_res.get("regime_stats", {})
            else:
                cache[timeframe] = bt_res.get("regime_stats", {})
            save_backtest_cache(cache)
        except Exception as exc:
            logger.error(f"Error running backtest for {timeframe}: {exc}")
            if symbol in cache:
                cache[symbol][timeframe] = {}
            else:
                cache[timeframe] = {}

    sym_cache = cache.get(symbol, cache) if symbol in cache else cache
    tf_cache = sym_cache.get(timeframe, {})
    regime_data = tf_cache.get(regime, {})
    trades_count = regime_data.get("trades", regime_data.get("total_trades", 0))

    if trades_count >= MIN_TRADES_CONFIDENCE:
        raw_wr = regime_data.get("win_rate", regime_data.get("win_rate_pct", 0.0))
        raw_pf = regime_data.get("profit_factor", 0.0)
        wr_str = f"{raw_wr:.2f}%"
        pf_str = f"{raw_pf:.2f}"
        confidence_str = f"Sufficient (N={trades_count})"
    else:
        # Strict requirement: mark "Insufficient data — low confidence" instead of numbers
        wr_str = "Insufficient data — low confidence"
        pf_str = "Insufficient data — low confidence"
        confidence_str = "Insufficient data — low confidence"

    return wr_str, pf_str, confidence_str, regime_data


def compute_indicator_snapshot(df: pd.DataFrame) -> Dict[str, Any]:
    """Computes pure indicator snapshot on latest candles."""
    curr_close = float(df["close"].iloc[-1])
    ema_9_val = float(indicators.ema(df, 9).iloc[-1])
    ema_21_val = float(indicators.ema(df, 21).iloc[-1])
    rsi_val = float(indicators.rsi(df, 14).iloc[-1])
    _, _, macd_hist = indicators.macd(df, 12, 26, 9)
    macd_hist_val = float(macd_hist.iloc[-1])
    bb_up, bb_mid, bb_low = indicators.bollinger_bands(df, 20, 2.0)
    bb_up_val = float(bb_up.iloc[-1])
    bb_mid_val = float(bb_mid.iloc[-1])
    bb_low_val = float(bb_low.iloc[-1])

    # Position relative to Bollinger Bands
    band_width = bb_up_val - bb_low_val
    if band_width > 0:
        pct_b = (curr_close - bb_low_val) / band_width
        pct_str = f"{pct_b * 100:.1f}%"
        if curr_close >= bb_up_val:
            bb_pos = f"Above Upper ({pct_str})"
        elif curr_close <= bb_low_val:
            bb_pos = f"Below Lower ({pct_str})"
        elif curr_close >= bb_mid_val:
            bb_pos = f"Upper Band ({pct_str})"
        else:
            bb_pos = f"Lower Band ({pct_str})"
    else:
        bb_pos = "Flat"

    return {
        "price": curr_close,
        "ema9": ema_9_val,
        "ema21": ema_21_val,
        "rsi": rsi_val,
        "macd_hist": macd_hist_val,
        "bb_up": bb_up_val,
        "bb_mid": bb_mid_val,
        "bb_low": bb_low_val,
        "bb_pos": bb_pos,
    }


def scan_single_timeframe(
    symbol: str,
    timeframe: str,
    df_htf: Optional[pd.DataFrame] = None,
    cache: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Scans a single timeframe independently:
      1. Fetches recent candles.
      2. Merges 4h HTF macro trend without look-ahead bias.
      3. Classifies market regime via regime_detector.py.
      4. Evaluates signal via strategy.generate_signal().
      5. Gathers indicator snapshot.
      6. Retrieves empirical backtest metrics for that timeframe + regime.
    """
    if cache is None:
        cache = load_backtest_cache()

    logger.info(f"Scanning {symbol} on {timeframe}...")
    # Fetch live recent candles (100 candles sufficient for all indicators)
    df_live = data_feed.fetch_live_candles(symbol=symbol, timeframe=timeframe, count=100)

    # Attach HTF macro trend (4h 200 EMA slope)
    df_merged = data_feed.attach_higher_timeframe_trend(df_live, df_htf)
    htf_trend_val = str(df_merged["htf_trend"].iloc[-1])

    # Detect regime
    regime_res = regime_detector.detect_regime(df_merged, htf_trend=htf_trend_val)
    regime_name = regime_res.regime

    # Generate signal using identical strategy logic
    decision = strategy.generate_signal(
        df=df_merged,
        htf_trend=htf_trend_val,
        regime_override=regime_res,
    )

    # Signal mapping
    if decision.signal == "BUY":
        signal_label = "LONG"
    elif decision.signal == "SHORT":
        signal_label = "SHORT"
    else:
        signal_label = "NO_SIGNAL"

    # Compute indicator snapshot
    indicators_snap = compute_indicator_snapshot(df_merged)

    # Pull backtest track record for this exact timeframe + regime
    wr_str, pf_str, conf_str, raw_stat = get_or_compute_backtest_stats(
        symbol=symbol,
        timeframe=timeframe,
        regime=regime_name,
        cache=cache,
    )

    latest_bar = df_merged.iloc[-1]
    ts_val = latest_bar["datetime"] if "datetime" in latest_bar else datetime.now(timezone.utc)
    ts_str = ts_val.strftime("%Y-%m-%d %H:%M:%S UTC") if isinstance(ts_val, pd.Timestamp) else str(ts_val)

    return {
        "timestamp": ts_str,
        "symbol": symbol,
        "timeframe": timeframe,
        "signal": signal_label,
        "regime": regime_name,
        "rule_set": decision.rule_set,
        "price": indicators_snap["price"],
        "ema9": indicators_snap["ema9"],
        "ema21": indicators_snap["ema21"],
        "rsi": indicators_snap["rsi"],
        "macd_hist": indicators_snap["macd_hist"],
        "bb_pos": indicators_snap["bb_pos"],
        "win_rate": wr_str,
        "profit_factor": pf_str,
        "confidence": conf_str,
    }


def init_signals_csv() -> None:
    """Ensures spot_bot/multi_tf_signals.csv has headers."""
    if not SIGNALS_CSV_PATH.exists():
        try:
            with open(SIGNALS_CSV_PATH, mode="w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(CSV_HEADERS)
            logger.info(f"Initialized CSV log: {SIGNALS_CSV_PATH.name}")
        except Exception as exc:
            logger.error(f"Failed to initialize {SIGNALS_CSV_PATH}: {exc}")


def append_scan_results_to_csv(results: List[Dict[str, Any]]) -> None:
    """Appends snapshot rows for all scanned timeframes to multi_tf_signals.csv."""
    init_signals_csv()
    rows = []
    for r in results:
        rows.append([
            r["timestamp"],
            r["symbol"],
            r["timeframe"],
            r["signal"],
            r["regime"],
            r["rule_set"],
            f"{r['price']:.2f}",
            f"{r['ema9']:.2f}",
            f"{r['ema21']:.2f}",
            f"{r['rsi']:.2f}",
            f"{r['macd_hist']:+.4f}",
            r["bb_pos"],
            r["win_rate"],
            r["profit_factor"],
            r["confidence"],
        ])

    try:
        with open(SIGNALS_CSV_PATH, mode="a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerows(rows)
        logger.info(f"Appended {len(rows)} multi-timeframe records to {SIGNALS_CSV_PATH.name}")
    except Exception as exc:
        logger.error(f"Failed appending to {SIGNALS_CSV_PATH}: {exc}")


def run_multi_timeframe_scan(
    symbol: str = DEFAULT_SYMBOL,
    timeframes: List[str] = SUPPORTED_TIMEFRAMES,
) -> List[Dict[str, Any]]:
    """
    Executes scan across all configured timeframes, prints table, and appends to CSV.
    """
    print("\n" + "=" * 135)
    print(f"               MULTI-TIMEFRAME SPOT SIGNAL DASHBOARD - {symbol}")
    print(f"               Scan Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 135)

    # 1. Fetch 4h HTF candles once for macro trend
    logger.info(f"Fetching 4h macro trend data for {symbol}...")
    df_htf = data_feed.fetch_higher_timeframe_data(
        symbol=symbol,
        timeframe="4h",
        limit=500,
        ema_period=200,
        use_cache=True,
    )

    # 2. Load shared backtest stats cache
    cache = load_backtest_cache()

    # 3. Scan each timeframe independently
    results: List[Dict[str, Any]] = []
    for tf in timeframes:
        try:
            res = scan_single_timeframe(
                symbol=symbol,
                timeframe=tf,
                df_htf=df_htf,
                cache=cache,
            )
            results.append(res)
        except Exception as exc:
            logger.error(f"Failed scanning timeframe {tf}: {exc}")

    # 4. Print clean console table
    col_tf = 10
    col_sig = 12
    col_reg = 16
    col_wr = 36
    col_pf = 36
    col_conf = 36

    header = (
        f"{'Timeframe':<{col_tf}} | "
        f"{'Signal':<{col_sig}} | "
        f"{'Regime':<{col_reg}} | "
        f"{'Backtested Win Rate':<{col_wr}} | "
        f"{'Backtested Profit Factor':<{col_pf}} | "
        f"{'Confidence':<{col_conf}}"
    )
    separator = (
        f"{'-'*col_tf}-+-"
        f"{'-'*col_sig}-+-"
        f"{'-'*col_reg}-+-"
        f"{'-'*col_wr}-+-"
        f"{'-'*col_pf}-+-"
        f"{'-'*col_conf}"
    )

    print(header)
    print(separator)
    for r in results:
        row = (
            f"{r['timeframe']:<{col_tf}} | "
            f"{r['signal']:<{col_sig}} | "
            f"{r['regime']:<{col_reg}} | "
            f"{r['win_rate']:<{col_wr}} | "
            f"{r['profit_factor']:<{col_pf}} | "
            f"{r['confidence']:<{col_conf}}"
        )
        print(row)
    print("=" * 135)

    # 5. Print indicator snapshot
    print("\nINDICATOR SNAPSHOT BY TIMEFRAME:")
    for r in results:
        print(
            f"  [{r['timeframe']:>3}] Price: ${r['price']:>10,.2f} | "
            f"EMA9: ${r['ema9']:>10,.2f} | "
            f"EMA21: ${r['ema21']:>10,.2f} | "
            f"RSI: {r['rsi']:>5.1f} | "
            f"MACD Hist: {r['macd_hist']:>+8.3f} | "
            f"BB Pos: {r['bb_pos']}"
        )
    print("-" * 135)
    print("[DISCLAIMER] Read-only multi-timeframe observer. Zero orders or executions placed.")
    print("=" * 135 + "\n")

    # 6. Save results to CSV
    append_scan_results_to_csv(results)

    return results


def main():
    parser = argparse.ArgumentParser(description="Multi-Timeframe Signal Dashboard (Strict Observer)")
    parser.add_argument("--symbol", type=str, default=DEFAULT_SYMBOL, help="Trading pair symbol (default: BTC/USDT)")
    parser.add_argument("--loop", action="store_true", help="Continuously refresh the dashboard on interval")
    parser.add_argument("--interval", type=int, default=60, help="Refresh interval in seconds for loop mode")

    args = parser.parse_args()

    if args.loop:
        print(f"Starting Multi-Timeframe Scanner loop for {args.symbol} every {args.interval}s. Press Ctrl+C to stop.")
        try:
            while True:
                run_multi_timeframe_scan(symbol=args.symbol)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n[INFO] Multi-timeframe scanner stopped by user.")
    else:
        run_multi_timeframe_scan(symbol=args.symbol)


if __name__ == "__main__":
    main()
