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
    from . import forex_data_feed
    from . import forex_utils
    from . import forex_sessions
    from . import strategy
    from . import indicators
    from . import regime_detector
    from . import backtest
    from .logger import get_logger
except ImportError:
    import config
    import data_feed
    import forex_data_feed
    import forex_utils
    import forex_sessions
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
DEFAULT_SYMBOL: str = "EUR/USD"
MIN_TRADES_CONFIDENCE: int = 30

CSV_HEADERS = [
    "timestamp",
    "symbol",
    "timeframe",
    "session",
    "signal",
    "confidence_score",
    "regime",
    "rule_set",
    "price",
    "ema9",
    "ema21",
    "rsi",
    "macd_hist",
    "bb_position",
    "suggested_sl",
    "suggested_tp",
    "sl_pips",
    "tp_pips",
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
      1. Fetches recent candles (Forex via Yahoo Finance or Crypto via Binance).
      2. Merges 4h HTF macro trend without look-ahead bias.
      3. Classifies market regime via regime_detector.py.
      4. Evaluates signal via strategy.generate_signal().
      5. Gathers indicator snapshot with pair-aware pip calculations.
      6. Retrieves empirical backtest metrics for that timeframe + regime.
    """
    if cache is None:
        cache = load_backtest_cache()

    std_symbol = forex_utils.clean_forex_symbol(symbol)
    is_forex = forex_data_feed.is_forex_symbol(std_symbol)

    logger.info(f"Scanning {std_symbol} on {timeframe}...")

    # Fetch live recent candles (100 candles sufficient for all indicators)
    if is_forex:
        df_live = forex_data_feed.fetch_forex_candles(pair=std_symbol, timeframe=timeframe, count=100)
        if df_live is None or df_live.empty or len(df_live) < 25:
            logger.warning(f"Insufficient live candle data returned for {std_symbol} ({timeframe}). Skipping...")
            return None
        df_merged = forex_data_feed.attach_higher_timeframe_trend(df_live, df_htf)
    else:
        df_live = data_feed.fetch_live_candles(symbol=std_symbol, timeframe=timeframe, count=100)
        if df_live is None or df_live.empty or len(df_live) < 25:
            logger.warning(f"Insufficient live candle data returned for {std_symbol} ({timeframe}). Skipping...")
            return None
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
        pair=std_symbol,
        timeframe=timeframe,
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
        symbol=std_symbol,
        timeframe=timeframe,
        regime=regime_name,
        cache=cache,
    )

    latest_bar = df_merged.iloc[-1]
    ts_val = latest_bar["datetime"] if "datetime" in latest_bar else datetime.now(timezone.utc)
    ts_str = ts_val.strftime("%Y-%m-%d %H:%M:%S UTC") if isinstance(ts_val, pd.Timestamp) else str(ts_val)

    # Session classification
    if is_forex:
        dt = ts_val.to_pydatetime() if isinstance(ts_val, pd.Timestamp) else (ts_val if isinstance(ts_val, datetime) else datetime.now(timezone.utc))
        session_label = forex_sessions.classify_session(dt)
    else:
        session_label = "24/7 Market"

    # Confidence score
    conf_obj = decision.confidence
    if conf_obj:
        conf_score_str = f"{conf_obj.score}/100 ({conf_obj.tier})"
        conf_factors = conf_obj.factors
    else:
        conf_score_str = "N/A"
        conf_factors = []

    # Pip calculations for SL / TP
    pip_size = forex_utils.get_pip_size(std_symbol) if is_forex else 1.0
    entry_p = decision.entry_price if decision.entry_price > 0 else indicators_snap["price"]
    sl_p = decision.stop_loss
    tp_p = decision.take_profit

    if is_forex and pip_size > 0:
        sl_pips = round(abs(entry_p - sl_p) / pip_size, 1) if sl_p > 0 else 0.0
        tp_pips = round(abs(tp_p - entry_p) / pip_size, 1) if tp_p > 0 else 0.0
    else:
        sl_pips = round(abs(entry_p - sl_p), 2) if sl_p > 0 else 0.0
        tp_pips = round(abs(tp_p - entry_p), 2) if tp_p > 0 else 0.0

    return {
        "timestamp": ts_str,
        "symbol": std_symbol,
        "is_forex": is_forex,
        "timeframe": timeframe,
        "session": session_label,
        "signal": signal_label,
        "confidence_score": conf_score_str,
        "confidence_factors": conf_factors,
        "regime": regime_name,
        "rule_set": decision.rule_set,
        "price": indicators_snap["price"],
        "ema9": indicators_snap["ema9"],
        "ema21": indicators_snap["ema21"],
        "rsi": indicators_snap["rsi"],
        "macd_hist": indicators_snap["macd_hist"],
        "bb_pos": indicators_snap["bb_pos"],
        "suggested_entry": entry_p,
        "suggested_sl": sl_p,
        "suggested_tp": tp_p,
        "sl_pips": sl_pips,
        "tp_pips": tp_pips,
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
    """Appends snapshot rows for all scanned timeframes to multi_tf_signals.csv with pair-aware precision."""
    init_signals_csv()
    rows = []
    for r in results:
        is_fx = r.get("is_forex", False)
        p_fmt = (lambda v: forex_utils.format_price(v, r["symbol"])) if is_fx else (lambda v: f"{v:.2f}")

        rows.append([
            r["timestamp"],
            r["symbol"],
            r["timeframe"],
            r.get("session", "N/A"),
            r["signal"],
            r.get("confidence_score", "N/A"),
            r["regime"],
            r["rule_set"],
            p_fmt(r["price"]),
            p_fmt(r["ema9"]),
            p_fmt(r["ema21"]),
            f"{r['rsi']:.2f}",
            f"{r['macd_hist']:+.5f}" if is_fx else f"{r['macd_hist']:+.4f}",
            r["bb_pos"],
            p_fmt(r["suggested_sl"]) if r.get("suggested_sl") else "0",
            p_fmt(r["suggested_tp"]) if r.get("suggested_tp") else "0",
            f"{r.get('sl_pips', 0.0):.1f}",
            f"{r.get('tp_pips', 0.0):.1f}",
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
    std_symbol = forex_utils.clean_forex_symbol(symbol)
    is_forex = forex_data_feed.is_forex_symbol(std_symbol)

    print("\n" + "=" * 145)
    print(f"               MULTI-TIMEFRAME FOREX/SPOT SIGNAL DASHBOARD - {std_symbol}")
    print(f"               Scan Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 145)

    # 1. Fetch 4h HTF candles once for macro trend
    logger.info(f"Fetching 4h macro trend data for {std_symbol}...")
    if is_forex:
        df_htf = forex_data_feed.fetch_higher_timeframe_forex_data(
            pair=std_symbol,
            timeframe="4h",
            limit=500,
            ema_period=200,
            use_cache=True,
        )
    else:
        df_htf = data_feed.fetch_higher_timeframe_data(
            symbol=std_symbol,
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
                symbol=std_symbol,
                timeframe=tf,
                df_htf=df_htf,
                cache=cache,
            )
            if res is not None:
                results.append(res)
        except Exception as exc:
            logger.error(f"Failed scanning timeframe {tf}: {exc}")

    # 4. Print clean console table
    col_tf = 8
    col_sess = 14
    col_sig = 10
    col_conf = 18
    col_reg = 16
    col_wr = 28
    col_pf = 24

    header = (
        f"{'TF':<{col_tf}} | "
        f"{'Session':<{col_sess}} | "
        f"{'Signal':<{col_sig}} | "
        f"{'Confidence':<{col_conf}} | "
        f"{'Regime':<{col_reg}} | "
        f"{'Backtested Win Rate':<{col_wr}} | "
        f"{'Profit Factor':<{col_pf}}"
    )
    separator = (
        f"{'-'*col_tf}-+-"
        f"{'-'*col_sess}-+-"
        f"{'-'*col_sig}-+-"
        f"{'-'*col_conf}-+-"
        f"{'-'*col_reg}-+-"
        f"{'-'*col_wr}-+-"
        f"{'-'*col_pf}"
    )

    print(header)
    print(separator)
    for r in results:
        row = (
            f"{r['timeframe']:<{col_tf}} | "
            f"{r.get('session', 'N/A'):<{col_sess}} | "
            f"{r['signal']:<{col_sig}} | "
            f"{r.get('confidence_score', 'N/A'):<{col_conf}} | "
            f"{r['regime']:<{col_reg}} | "
            f"{r['win_rate']:<{col_wr}} | "
            f"{r['profit_factor']:<{col_pf}}"
        )
        print(row)
    print("=" * 145)

    # 5. Print indicator & suggested levels snapshot
    print("\nINDICATOR & LEVEL SNAPSHOT BY TIMEFRAME:")
    for r in results:
        is_fx = r.get("is_forex", False)
        p_fmt = (lambda v: forex_utils.format_price(v, r["symbol"])) if is_fx else (lambda v: f"${v:,.2f}")
        prefix = "" if is_fx else "$"

        sl_info = f"SL: {p_fmt(r['suggested_sl'])} (-{r.get('sl_pips', 0.0):.1f}p)" if r.get('suggested_sl') else "SL: N/A"
        tp_info = f"TP: {p_fmt(r['suggested_tp'])} (+{r.get('tp_pips', 0.0):.1f}p)" if r.get('suggested_tp') else "TP: N/A"

        print(
            f"  [{r['timeframe']:>3}] Price: {p_fmt(r['price'])} | "
            f"EMA9: {p_fmt(r['ema9'])} | "
            f"EMA21: {p_fmt(r['ema21'])} | "
            f"RSI: {r['rsi']:>5.1f} | "
            f"MACD Hist: {r['macd_hist']:>+7.4f} | "
            f"{sl_info} | {tp_info}"
        )
    print("-" * 145)
    print("[DISCLAIMER] Read-only multi-timeframe observer. Zero orders or executions placed.")
    print("=" * 145 + "\n")

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
